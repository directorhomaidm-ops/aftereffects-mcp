#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2"]
# ///
"""aftereffects-mcp — MCP server that drives Adobe After Effects through ExtendScript.

Run (After Effects must be open, macOS):
    uv run aftereffects_mcp.py

Register with Claude Code:
    claude mcp add aftereffects -- uv run /path/to/aftereffects_mcp.py

Each tool runs one command of jsx/aemcp.jsx inside After Effects: the script goes through AppleScript's DoScript
(`osascript`) and its JSON result comes back through a result file, or on stdout when the file is missing (that
needs Settings > Scripting & Expressions > "Allow Scripts to Write Files and Access Network").
Set AE_APP if your After Effects is not "Adobe After Effects 2026". Logs one JSON object per line to stderr; set
AE_MCP_LOG_LEVEL (default INFO) to change verbosity.
"""

import functools
import inspect
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from array import array
import zlib
from time import monotonic, sleep  # export_frame has a parameter named time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

LIB = Path(__file__).resolve().parent / "jsx" / "aemcp.jsx"
DEFAULT_TIMEOUT = 60

mcp = MCPServer(
    "aftereffects",
    instructions="Controls the running Adobe After Effects: project items, compositions, layers (text, solid, shape, "
    "null, adjustment, camera, light, footage), properties, keyframes with easing, expressions, effects, masks, "
    "vector shapes, text animation presets, precomposing, layer order and switches, track mattes, markers, motion "
    "paths and easing curves, point tracking, cameras, extruded 3D and parallax, import, the render queue and frame "
    "export. Start with status and list_items, then comp_info to see a comp's layers. "
    "Times are in seconds. Layers are addressed by 1-based index or name, comps by name or id; with no comp the "
    "active comp is used.",
)

log = logging.getLogger("aftereffects_mcp")


class _JsonFormatter(logging.Formatter):
    """One JSON object per line. Structured fields come from `extra={"fields": {...}}`."""

    def format(self, record):
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def _setup_logging():
    # stdout carries the MCP stdio protocol, so logs must go to stderr.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    log.addHandler(handler)
    level = os.environ.get("AE_MCP_LOG_LEVEL", "INFO").upper()
    log.setLevel(level if isinstance(logging.getLevelName(level), int) else logging.INFO)
    log.propagate = False


def _tool(fn):
    """Register `fn` as an MCP tool and log each call with its arguments, duration and outcome."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        fields = {"tool": fn.__name__, "args": dict(sig.bind(*args, **kwargs).arguments)}
        start = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except ToolError as e:
            fields.update(outcome="error", error=str(e), duration_ms=round((time.perf_counter() - start) * 1000, 1))
            log.warning("tool call failed", extra={"fields": fields})
            raise
        except Exception:
            fields.update(outcome="crash", duration_ms=round((time.perf_counter() - start) * 1000, 1))
            log.exception("tool call crashed", extra={"fields": fields})
            raise
        fields.update(outcome="ok", duration_ms=round((time.perf_counter() - start) * 1000, 1))
        log.info("tool call", extra={"fields": fields})
        return result

    return mcp.tool()(wrapper)


# --- transport ---


def _applescript_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript(script, timeout):
    """Run ExtendScript in After Effects through AppleScript's DoScript; return the script's string result.
    The script is read from a UTF-8 file inside After Effects (so any text survives), and also writes its result
    to a file, used when DoScript hands nothing back."""
    if sys.platform != "darwin":
        raise ToolError("aftereffects-mcp drives After Effects through AppleScript: macOS only for now")
    app = os.environ.get("AE_APP", "Adobe After Effects 2026")
    tmp = tempfile.mkdtemp(prefix="aemcp_")
    src, out = os.path.join(tmp, "call.jsx"), os.path.join(tmp, "result.json")
    with open(src, "w", encoding="utf-8") as f:
        f.write(script.replace("__AEMCP_RESULT_FILE__", json.dumps(out)))
    loader = (f'var __f = new File({json.dumps(src)}); __f.encoding = "UTF-8"; __f.open("r"); '
              'var __s = __f.read(); __f.close(); eval(__s);')
    cmd = ["osascript", "-e", f"with timeout of {int(timeout)} seconds",
           "-e", f"tell application {_applescript_string(app)} to DoScript {_applescript_string(loader)}",
           "-e", "end timeout"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        if r.returncode:
            err = r.stderr.strip()
            if "-1728" in err or "-2700" in err or "Can’t get application" in err or "Can't get application" in err:
                raise ToolError(f"{app} not found: set AE_APP to your After Effects' name ({err})")
            if "-1712" in err:
                raise ToolError(f"After Effects did not answer within {timeout}s (a dialog open, or a long render?)")
            raise ToolError(f"osascript failed: {err or r.returncode}")
        # The result file wins: AE 2026's DoScript prints "0" on stdout whatever the script returns.
        if os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                return f.read().strip()
        return r.stdout.strip()
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"After Effects did not answer within {timeout}s") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


RUNNER = _osascript  # tests swap in a Node host running the same library against a fake After Effects


def _call(command, timeout=DEFAULT_TIMEOUT, keep_null=(), **args):
    """Run one aemcp command in After Effects and return its result (raises ToolError with its error). None
    arguments are left out, except those in keep_null, sent as null."""
    args = {k: v for k, v in args.items() if v is not None or k in keep_null}
    lib = LIB.read_text(encoding="utf-8")
    script = (f"{lib}\nvar __aemcp_result = aemcp.run({json.dumps(command)}, {json.dumps(args)});\n"
              "try { var __o = new File(__AEMCP_RESULT_FILE__); __o.encoding = \"UTF-8\"; __o.open(\"w\"); "
              "__o.write(__aemcp_result); __o.close(); } catch (__e) {}\n__aemcp_result;")
    raw = RUNNER(script, timeout)
    if not raw:
        raise ToolError("After Effects returned nothing: allow Settings > Scripting & Expressions > "
                        "\"Allow Scripts to Write Files and Access Network\", then retry")
    try:
        reply = json.loads(raw)
    except json.JSONDecodeError:
        raise ToolError(f"unexpected reply from After Effects: {raw[:300]}") from None
    if not isinstance(reply, dict):
        # DoScript's own "0" with no result file: the script never ran, usually because a dialog is open
        raise ToolError("After Effects did not run the script: close any open dialog in After Effects, then retry")
    if "error" in reply:
        raise ToolError(reply["error"])
    return reply["ok"]


def _path(p):
    return os.path.abspath(os.path.expanduser(p))


def _color(name, v, n=3):
    if v is None:
        return None
    if len(v) != n or not all(0 <= float(x) <= 1 for x in v):
        raise ToolError(f"{name} needs {n} values 0-1")
    return [float(x) for x in v]


# --- project and comps ---


@_tool
def status() -> dict:
    """After Effects version, the open project's file, its item count and the active comp."""
    return _call("status")


@_tool
def list_items() -> list[dict]:
    """Every project item: id, name, type (comp, footage, folder), folder, and for comps and footage size,
    duration (s) and frame rate; footage also its file."""
    return _call("list_items")


@_tool
def create_comp(name: str, width: int = 1920, height: int = 1080, duration: float = 10.0,
                frame_rate: float = 30.0, pixel_aspect: float = 1.0, bg_color: list[float] | None = None) -> dict:
    """Create a composition and open it in the viewer (it becomes the active comp). duration in seconds; bg_color
    [r, g, b] 0-1. Vertical video: width=1080, height=1920."""
    if not (4 <= width <= 30000 and 4 <= height <= 30000):
        raise ToolError("width and height must be 4-30000 pixels")
    if not 0 < duration <= 10800:
        raise ToolError("duration must be above 0 and at most 3 hours")
    if not 1 <= frame_rate <= 999:
        raise ToolError("frame_rate must be 1-999")
    return _call("create_comp", name=name, width=width, height=height, duration=duration, frameRate=frame_rate,
                 pixelAspect=pixel_aspect, bgColor=_color("bg_color", bg_color))


@_tool
def comp_info(comp: int | str | None = None) -> dict:
    """A comp's size, duration, frame rate and layers (index, name, type, in/out/start in seconds, parent,
    effects, source). comp: name or id; default the active comp."""
    return _call("comp_info", comp=comp)


# --- layers ---

LAYER_KINDS = ("text", "solid", "shape", "null", "adjustment", "camera", "light", "item")


@_tool
def add_layer(kind: str, comp: int | str | None = None, name: str | None = None, text: str | None = None,
              color: list[float] | None = None, item: int | str | None = None) -> dict:
    """Add a layer on top of a comp: kind text (with `text`), solid (with `color` [r, g, b] 0-1), shape, null,
    adjustment, camera, light, or item (a project item by name or id: footage or another comp)."""
    if kind not in LAYER_KINDS:
        raise ToolError(f"kind must be one of: {', '.join(LAYER_KINDS)}")
    if kind == "item" and item is None:
        raise ToolError("kind='item' needs item (a project item name or id, see list_items)")
    return _call("add_layer", comp=comp, kind=kind, name=name, text=text, color=_color("color", color), item=item)


@_tool
def delete_layer(layer: int | str, comp: int | str | None = None) -> dict:
    """Delete a layer (1-based index or name)."""
    return _call("delete_layer", comp=comp, layer=layer)


@_tool
def set_layer(layer: int | str, comp: int | str | None = None, name: str | None = None,
              start_time: float | None = None, in_point: float | None = None, out_point: float | None = None,
              enabled: bool | None = None, parent: int | str | None = None, unparent: bool = False) -> dict:
    """Change a layer's name, timing (start_time moves it, in/out_point trim it, in seconds), visibility, or parent
    (another layer's index or name; unparent=True clears it)."""
    if unparent and parent is not None:
        raise ToolError("give parent or unparent, not both")
    args = dict(comp=comp, layer=layer, name=name, startTime=start_time, inPoint=in_point, outPoint=out_point,
                enabled=enabled, parent=parent)
    return _call("set_layer", keep_null=("parent",) if unparent else (), **args)


@_tool
def set_text(layer: int | str, text: str | None = None, font: str | None = None, size: float | None = None,
             color: list[float] | None = None, justification: str | None = None,
             comp: int | str | None = None) -> dict:
    """Edit a text layer: text (any language), font (PostScript name, e.g. "Arial-BoldMT"), size in pixels, fill
    color [r, g, b] 0-1, justification left, center or right."""
    if justification is not None and justification not in ("left", "center", "right"):
        raise ToolError("justification must be left, center or right")
    value = {k: v for k, v in (("text", text), ("font", font), ("fontSize", size),
                               ("fillColor", _color("color", color)), ("justification", justification))
             if v is not None}
    if not value:
        raise ToolError("nothing to change")
    return _call("set_property", comp=comp, layer=layer, property="text", value=value)


# --- properties, keyframes, expressions, effects ---


@_tool
def get_property(layer: int | str, property: str | list[str], comp: int | str | None = None) -> dict:
    """Read a layer property: value, keyframes (time, value) and expression. property: anchor, position, scale,
    rotation, opacity, text, or a path of names / match names, e.g. ["Effects", "Gaussian Blur", "Blurriness"] or
    ["ADBE Transform Group", "ADBE Position"]."""
    return _call("get_property", comp=comp, layer=layer, property=property)


@_tool
def set_property(layer: int | str, property: str | list[str], value: Any, comp: int | str | None = None) -> dict:
    """Set a static property value: numbers or arrays ([x, y] position, [w, h] scale in %, colors [r, g, b(, a)]
    0-1). Refused on animated properties: use set_keyframes."""
    return _call("set_property", comp=comp, layer=layer, property=property, value=value)


EASES = ("linear", "ease", "ease_in", "ease_out", "hold")


@_tool
def set_keyframes(layer: int | str, property: str | list[str], keys: list[dict], clear: bool = False,
                  comp: int | str | None = None) -> dict:
    """Animate a property: keys [{"time": s, "value": v, "ease": "ease"}], ease linear, ease (both sides, the
    default), ease_in (slow into the key), ease_out (slow out of it) or hold; "influence" (0.1-100, default 33.33)
    sets how strong the easing is. clear=True removes existing keyframes first."""
    if not keys:
        raise ToolError("no keys")
    for k in keys:
        if not isinstance(k, dict) or "time" not in k or "value" not in k:
            raise ToolError(f"each key needs time and value: {k!r}")
        if k.get("ease", "ease") not in EASES:
            raise ToolError(f"ease must be one of: {', '.join(EASES)}")
        if "influence" in k and not 0.1 <= float(k["influence"]) <= 100:
            raise ToolError("influence must be 0.1-100")
    return _call("set_keyframes", comp=comp, layer=layer, property=property, keys=keys, clear=clear or None)


@_tool
def set_expression(layer: int | str, property: str | list[str], expression: str,
                   comp: int | str | None = None) -> dict:
    """Put an expression on a property (e.g. "wiggle(2, 30)" on position); "" removes it. Returns After Effects'
    expression error when it does not evaluate."""
    return _call("set_expression", comp=comp, layer=layer, property=property, expression=expression)


@_tool
def add_effect(layer: int | str, effect: str, settings: dict | None = None, name: str | None = None,
               comp: int | str | None = None) -> dict:
    """Apply an effect by display or match name (e.g. "Gaussian Blur" or "ADBE Gaussian Blur 2"; see list_effects)
    and set its parameters by name, e.g. {"Blurriness": 20}."""
    return _call("add_effect", comp=comp, layer=layer, effect=effect, settings=settings or {}, name=name)


@_tool
def list_effects(query: str | None = None) -> list[dict]:
    """Installed effects (display name, match name, category), filtered by `query` when given."""
    return _call("list_effects", query=query)


# --- import, render, save ---


@_tool
def import_file(path: str, sequence: bool = False, folder: int | str | None = None) -> dict:
    """Import footage, audio, an image, or (sequence=True, give the first file) an image sequence into the project,
    optionally into a folder item."""
    return _call("import_file", path=_path(path), sequence=sequence or None, folder=folder)


@_tool
def add_to_render_queue(output: str, comp: int | str | None = None, template: str | None = None) -> dict:
    """Queue a comp for rendering to `output` (the extension follows the output module template, e.g. an H.264 or
    ProRes template name from After Effects' Output Module Templates)."""
    return _call("add_to_render_queue", comp=comp, output=_path(output), template=template)


@_tool
def render(timeout: int = 1800) -> dict:
    """Render everything queued. After Effects is busy until it finishes; timeout is in seconds."""
    return _call("render", timeout=timeout)


@_tool
def export_frame(path: str, time: float = 0.0, comp: int | str | None = None) -> dict:
    """Save one frame of a comp as a PNG file (time in seconds)."""
    out = _call("export_frame", comp=comp, path=_path(path), time=time)
    _wait_png(out["path"])
    return out


def _wait_png(path):
    """saveFrameToPng returns at once and fills the file later: wait for the PNG's IEND chunk, return its bytes."""
    deadline = monotonic() + DEFAULT_TIMEOUT
    while monotonic() < deadline:
        try:
            data = Path(path).read_bytes()
            if data[-8:-4] == b"IEND":
                return data
        except OSError:
            pass
        sleep(0.02)
    raise ToolError(f"After Effects did not finish writing {path} within {DEFAULT_TIMEOUT}s")


@_tool
def save_project(path: str | None = None) -> dict:
    """Save the project (to `path`, an .aep, when given or never saved)."""
    return _call("save_project", path=_path(path) if path else None)


# --- masks, shapes, text animation ---

MASK_MODES = ("none", "add", "subtract", "intersect", "lighten", "darken", "difference")


def _rect(name, r):
    if r is None or len(r) != 4 or float(r[2]) <= 0 or float(r[3]) <= 0:
        raise ToolError(f"{name} needs [x, y, width, height] with a positive width and height")
    return [float(v) for v in r]


@_tool
def add_mask(layer: int | str, shape: str = "rect", rect: list[float] | None = None,
             points: list[list[float]] | None = None, mode: str = "add", feather: float | None = None,
             expansion: float | None = None, opacity: float | None = None, inverted: bool = False,
             name: str | None = None, comp: int | str | None = None) -> dict:
    """Add a mask to a layer: shape rect or ellipse inside rect [x, y, width, height] (layer pixels, from the
    layer's top left), or path through points [[x, y], ...] (at least 3, closed). mode none, add, subtract,
    intersect, lighten, darken or difference; feather and expansion in pixels, opacity 0-100, inverted flips it.
    Animate its shape or feather afterwards with set_keyframes on ["Masks", "<mask name>", "Mask Feather"]."""
    if mode not in MASK_MODES:
        raise ToolError(f"mode must be one of: {', '.join(MASK_MODES)}")
    if shape in ("rect", "ellipse"):
        rect = _rect("rect", rect)
    elif shape == "path":
        if not points or len(points) < 3 or any(len(pt) != 2 for pt in points):
            raise ToolError("path needs at least 3 points [x, y]")
    else:
        raise ToolError("shape must be rect, ellipse or path")
    if feather is not None and feather < 0:
        raise ToolError("feather must be 0 or more")
    if opacity is not None and not 0 <= opacity <= 100:
        raise ToolError("opacity must be 0-100")
    return _call("add_mask", comp=comp, layer=layer, shape=shape, rect=rect, points=points, mode=mode,
                 feather=feather, expansion=expansion, opacity=opacity, inverted=inverted or None, name=name)


SHAPES = ("rect", "ellipse", "star", "polygon")


@_tool
def add_shape(shape: str, size: list[float] = [200, 200], fill: list[float] | None = None,
              stroke: list[float] | None = None, stroke_width: float = 4, roundness: float = 0, points: int = 5,
              position: list[float] | None = None, layer: int | str | None = None, layer_name: str | None = None,
              name: str | None = None, comp: int | str | None = None) -> dict:
    """Draw a vector shape: rect (with corner roundness), ellipse, star or polygon (`points` corners; size[0] is the
    diameter), sized [width, height] in pixels, with fill and/or stroke colors [r, g, b] 0-1 (white fill when
    neither is given) and stroke_width. It goes into a new shape layer (named layer_name), or into an existing shape
    layer when `layer` is given, as its own group (`name`); position offsets it from the layer's anchor."""
    if shape not in SHAPES:
        raise ToolError(f"shape must be one of: {', '.join(SHAPES)}")
    if len(size) != 2 or min(size) <= 0:
        raise ToolError("size needs [width, height] above 0")
    if shape in ("star", "polygon") and not 3 <= points <= 100:
        raise ToolError("points must be 3-100")
    if stroke_width <= 0 or roundness < 0:
        raise ToolError("stroke_width must be above 0 and roundness 0 or more")
    if fill is None and stroke is None:
        fill = [1.0, 1.0, 1.0]
    rgba = lambda n, v: _color(n, v) + [1.0] if v is not None else None  # noqa: E731 - color props take alpha
    return _call("add_shape", comp=comp, layer=layer, layerName=layer_name, name=name, shape=shape,
                 size=[float(v) for v in size], fill=rgba("fill", fill), stroke=rgba("stroke", stroke),
                 strokeWidth=stroke_width, roundness=roundness or None, points=points, position=position)


TEXT_PRESETS = ("fade_in", "typewriter", "slide_up", "scale_in", "blur_in")


@_tool
def animate_text(layer: int | str, preset: str = "fade_in", duration: float = 1.0, start: float | None = None,
                 comp: int | str | None = None) -> dict:
    """Reveal a text layer character by character with a text animator: fade_in, typewriter (each letter pops in),
    slide_up, scale_in or blur_in, over `duration` seconds from `start` (default the layer's in point). Adds an
    animator named "aemcp <preset>"; calling again stacks another."""
    if preset not in TEXT_PRESETS:
        raise ToolError(f"preset must be one of: {', '.join(TEXT_PRESETS)}")
    if duration <= 0:
        raise ToolError("duration must be above 0")
    return _call("animate_text", comp=comp, layer=layer, preset=preset, duration=duration, start=start)


# --- layer structure and switches ---


@_tool
def precompose(layers: list[int | str], name: str, move_attributes: bool = True,
               comp: int | str | None = None) -> dict:
    """Precompose layers (indexes or names) into a new comp `name`, replaced in this comp by one layer.
    move_attributes=False keeps effects and transforms on the outer layer (one layer only)."""
    if not layers:
        raise ToolError("no layers")
    if not move_attributes and len(layers) > 1:
        raise ToolError("move_attributes=False works on one layer only")
    return _call("precompose", comp=comp, layers=layers, name=name, moveAttributes=move_attributes)


@_tool
def duplicate_layer(layer: int | str, name: str | None = None, comp: int | str | None = None) -> dict:
    """Duplicate a layer with its keyframes, effects and masks; the copy goes right above it."""
    return _call("duplicate_layer", comp=comp, layer=layer, name=name)


@_tool
def move_layer(layer: int | str, to: str, other: int | str | None = None, comp: int | str | None = None) -> dict:
    """Reorder a layer: to top, bottom, before (above) or after (below) the `other` layer."""
    if to not in ("top", "bottom", "before", "after"):
        raise ToolError("to must be top, bottom, before or after")
    if to in ("before", "after") and other is None:
        raise ToolError(f"to={to} needs the other layer")
    return _call("move_layer", comp=comp, layer=layer, to=to, other=other)


BLENDING = ("normal", "dissolve", "darken", "multiply", "color_burn", "linear_burn", "lighten", "screen",
            "color_dodge", "linear_dodge", "add", "overlay", "soft_light", "hard_light", "difference", "exclusion",
            "hue", "saturation", "color", "luminosity")
MATTES = ("alpha", "alpha_inverted", "luma", "luma_inverted", "none")


@_tool
def set_switches(layer: int | str, blending: str | None = None, three_d: bool | None = None,
                 motion_blur: bool | None = None, shy: bool | None = None, solo: bool | None = None,
                 locked: bool | None = None, matte: str | None = None, matte_layer: int | str | None = None,
                 comp: int | str | None = None) -> dict:
    """Layer switches: blending mode (normal, screen, multiply, overlay, add, soft_light...), 3D, motion blur, shy,
    solo, locked, and a track matte: matte alpha, alpha_inverted, luma or luma_inverted from matte_layer (After
    Effects 2023+), or none to remove it. Locking is applied last."""
    if blending is not None and blending not in BLENDING:
        raise ToolError(f"blending must be one of: {', '.join(BLENDING)}")
    if matte is not None and matte not in MATTES:
        raise ToolError(f"matte must be one of: {', '.join(MATTES)}")
    if matte not in (None, "none") and matte_layer is None:
        raise ToolError("a track matte needs matte_layer")
    return _call("set_switches", comp=comp, layer=layer, blending=blending, threeD=three_d, motionBlur=motion_blur,
                 shy=shy, solo=solo, locked=locked, matte=matte, matteLayer=matte_layer)


@_tool
def add_marker(time: float, comment: str = "", duration: float = 0.0, layer: int | str | None = None,
               comp: int | str | None = None) -> dict:
    """Add a marker at `time` (seconds) with a comment and optional duration: on the comp, or on a layer."""
    if time < 0 or duration < 0:
        raise ToolError("time and duration must be 0 or more")
    return _call("add_marker", comp=comp, layer=layer, time=time, comment=comment, duration=duration or None)


@_tool
def set_comp(comp: int | str | None = None, name: str | None = None, width: int | None = None,
             height: int | None = None, duration: float | None = None, frame_rate: float | None = None,
             bg_color: list[float] | None = None, work_area: list[float] | None = None,
             motion_blur: bool | None = None, shutter_angle: float | None = None) -> dict:
    """Change a comp's name, size, duration, frame rate, background color, work area [start, end] in seconds
    (what renders when the render settings use the work area), or motion blur: motion_blur switches it on for the
    comp (layers also need theirs, set_switches), shutter_angle 0-720 degrees sets how long the blur streaks
    (180 = film look, 360 = one whole frame)."""
    if width is not None and not 4 <= width <= 30000 or height is not None and not 4 <= height <= 30000:
        raise ToolError("width and height must be 4-30000 pixels")
    if duration is not None and not 0 < duration <= 10800:
        raise ToolError("duration must be above 0 and at most 3 hours")
    if frame_rate is not None and not 1 <= frame_rate <= 999:
        raise ToolError("frame_rate must be 1-999")
    if work_area is not None and len(work_area) != 2:
        raise ToolError("work_area needs [start, end]")
    if shutter_angle is not None and not 0 <= shutter_angle <= 720:
        raise ToolError("shutter_angle must be 0-720 degrees")
    return _call("set_comp", comp=comp, name=name, width=width, height=height, duration=duration,
                 frameRate=frame_rate, bgColor=_color("bg_color", bg_color), workArea=work_area,
                 motionBlur=motion_blur, shutterAngle=shutter_angle)


@_tool
def render_templates() -> dict:
    """The output module and render settings template names installed in After Effects, for
    add_to_render_queue(template=...). Needs at least one comp in the project."""
    return _call("render_templates")


# --- expression presets, timing, placement ---

BOUNCE = """// inertial bounce after each keyframe
var amp = {amp}, freq = {freq}, decay = {decay};
var n = 0;
if (numKeys > 0) {{ n = nearestKey(time).index; if (key(n).time > time) n--; }}
var t = n === 0 ? 0 : time - key(n).time;
if (n > 0 && t < 1) {{
  var v = velocityAtTime(key(n).time - thisComp.frameDuration / 10);
  value + v * amp * Math.sin(freq * t * 2 * Math.PI) / Math.exp(decay * t);
}} else {{ value; }}"""

EXPRESSION_PRESETS = {
    "wiggle": ("wiggle({freq}, {amp})", {"freq": 2, "amp": 30}),
    "loop_cycle": ('loopOut("cycle")', {}),
    "loop_pingpong": ('loopOut("pingpong")', {}),
    "loop_continue": ('loopOut("continue")', {}),
    "bounce": (BOUNCE, {"amp": 0.05, "freq": 4, "decay": 8}),
    "spin": ("value + time * {speed}", {"speed": 90}),
    "blink": ("Math.floor(time * {freq} * 2) % 2 === 0 ? value : 0", {"freq": 2}),
}


@_tool
def expression_preset(layer: int | str, property: str | list[str], preset: str, params: dict | None = None,
                      comp: int | str | None = None) -> dict:
    """Put a ready-made expression on a property: wiggle (freq per second, amp in the property's units), loop_cycle,
    loop_pingpong or loop_continue (repeat the keyframes after the last one), bounce (inertial overshoot after each
    keyframe: amp, freq, decay), spin (rotation: speed in degrees per second), blink (opacity: freq per second).
    params overrides the defaults, e.g. {"freq": 3, "amp": 50}."""
    if preset not in EXPRESSION_PRESETS:
        raise ToolError(f"preset must be one of: {', '.join(EXPRESSION_PRESETS)}")
    template, defaults = EXPRESSION_PRESETS[preset]
    unknown = set(params or {}) - set(defaults)
    if unknown:
        raise ToolError(f"{preset} takes {', '.join(defaults) or 'no params'} (got {', '.join(sorted(unknown))})")
    values = {**defaults, **(params or {})}
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values.values()):
        raise ToolError("params must be numbers")
    out = _call("set_expression", comp=comp, layer=layer, property=property, expression=template.format(**values))
    return {**out, "preset": preset, "params": values}


@_tool
def property_tree(layer: int | str, depth: int = 3, comp: int | str | None = None) -> dict:
    """A layer's property hierarchy: names and match names of every group and property down to `depth`, with
    values and keyframe counts, to find the path for get_property / set_property / set_keyframes."""
    if not 1 <= depth <= 8:
        raise ToolError("depth must be 1-8")
    return _call("property_tree", comp=comp, layer=layer, depth=depth)


@_tool
def sequence_layers(layers: list[int | str], start: float = 0.0, overlap: float = 0.0,
                    comp: int | str | None = None) -> dict:
    """Place layers one after another in the given order from `start` (seconds): each begins where the previous
    ends, minus `overlap` seconds (negative for a gap). Durations are kept."""
    if not layers:
        raise ToolError("no layers")
    if start < 0:
        raise ToolError("start must be 0 or more")
    return _call("sequence_layers", comp=comp, layers=layers, start=start, overlap=overlap)


@_tool
def time_remap(layer: int | str, keys: list[dict], smooth: bool = True, comp: int | str | None = None) -> dict:
    """Retime footage or a precomp: keys [{"time": comp seconds, "source": source seconds}] replace its time
    remapping, e.g. slow motion ({0: 0}, {4: 2}), speed ramps, or a freeze frame ("hold": true holds a key).
    smooth eases between keys."""
    if not keys:
        raise ToolError("no keys")
    for k in keys:
        if not isinstance(k, dict) or "time" not in k or "source" not in k:
            raise ToolError(f"each key needs time and source: {k!r}")
        if k["source"] < 0:
            raise ToolError("source times must be 0 or more")
    return _call("time_remap", comp=comp, layer=layer, keys=keys, smooth=smooth)


FIT_MODES = ("fill", "fit", "width", "height", "stretch")


@_tool
def fit_to_comp(layer: int | str, mode: str = "fill", comp: int | str | None = None) -> dict:
    """Scale a layer to the comp and center it: fill (cover the frame, cropping), fit (whole layer visible), width,
    height, or stretch (both axes). The anchor point moves to the layer's center."""
    if mode not in FIT_MODES:
        raise ToolError(f"mode must be one of: {', '.join(FIT_MODES)}")
    return _call("fit_to_comp", comp=comp, layer=layer, mode=mode)


@_tool
def center_anchor(layer: int | str, comp: int | str | None = None) -> dict:
    """Move a layer's anchor point to the center of what it draws (text, shapes, footage) without moving it on
    screen, so scale and rotation happen around its middle. Do it before animating anchor or position."""
    return _call("center_anchor", comp=comp, layer=layer)


@_tool
def null_control(layers: list[int | str], name: str = "Control", comp: int | str | None = None) -> dict:
    """Add a null at the layers' average position and parent them to it, so one null moves, scales and rotates them
    together."""
    if not layers:
        raise ToolError("no layers")
    return _call("null_control", comp=comp, layers=layers, name=name)


# --- presets, footage, project organization ---


@_tool
def apply_preset(layer: int | str, path: str, comp: int | str | None = None) -> dict:
    """Apply an animation preset (.ffx, e.g. from After Effects' Presets folder) to a layer."""
    path = _path(path)
    if not path.lower().endswith(".ffx"):
        raise ToolError("an animation preset is an .ffx file")
    return _call("apply_preset", comp=comp, layer=layer, path=path)


@_tool
def replace_footage(item: int | str, path: str, sequence: bool = False) -> dict:
    """Swap a footage item's file (every layer using it follows); sequence=True for an image sequence (first
    file)."""
    return _call("replace_footage", item=item, path=_path(path), sequence=sequence or None)


@_tool
def create_folder(name: str, parent: int | str | None = None) -> dict:
    """Create a project folder, optionally inside another folder."""
    return _call("create_folder", name=name, parent=parent)


@_tool
def move_items(items: list[int | str], folder: int | str) -> dict:
    """Move project items (names or ids) into a folder."""
    if not items:
        raise ToolError("no items")
    return _call("move_items", items=items, folder=folder)


@_tool
def clean_project(remove_unused: bool = True, consolidate: bool = True) -> dict:
    """Tidy the project: consolidate merges duplicate footage items of the same file, remove_unused deletes footage
    no comp uses. Returns the counts and the item total before and after."""
    if not (remove_unused or consolidate):
        raise ToolError("nothing to do")
    return _call("clean_project", removeUnused=remove_unused or None, consolidate=consolidate or None)


# --- camera, light, audio ---

CAMERA_MOVES = ("push_in", "pull_out", "pan_left", "pan_right", "crane_up", "crane_down", "orbit")


@_tool
def camera_move(move: str, amount: float | None = None, duration: float = 3.0, start: float = 0.0,
                ease: bool = True, camera: int | str | None = None, comp: int | str | None = None) -> dict:
    """Animate a 3D camera (a new one unless `camera` names one): push_in / pull_out along its line of sight,
    pan_left / pan_right / crane_up / crane_down (camera and point of interest together), each by `amount` pixels
    (default 400), or orbit around the point of interest by `amount` degrees (default 90) through a 3D null rig.
    Over `duration` seconds from `start`, eased unless ease=False. Layers need their 3D switch on (set_switches)."""
    if move not in CAMERA_MOVES:
        raise ToolError(f"move must be one of: {', '.join(CAMERA_MOVES)}")
    if duration <= 0 or start < 0:
        raise ToolError("duration must be above 0 and start 0 or more")
    amount = amount if amount is not None else (90.0 if move == "orbit" else 400.0)
    if amount <= 0:
        raise ToolError("amount must be above 0 (the move sets the direction)")
    return _call("camera_move", comp=comp, camera=camera, move=move, amount=amount, duration=duration, start=start,
                 ease=ease)


LIGHT_TYPES = ("parallel", "spot", "point", "ambient")


@_tool
def set_light(layer: int | str, type: str | None = None, intensity: float | None = None,
              color: list[float] | None = None, cone_angle: float | None = None, cone_feather: float | None = None,
              shadows: bool | None = None, comp: int | str | None = None) -> dict:
    """Set up a light layer: type parallel, spot, point or ambient; intensity in % (100 = normal); color [r, g, b]
    0-1; cone_angle (degrees) and cone_feather (%) for spots; shadows makes it cast shadows (layers also need their
    shadow switches)."""
    if type is not None and type not in LIGHT_TYPES:
        raise ToolError(f"type must be one of: {', '.join(LIGHT_TYPES)}")
    if cone_angle is not None and not 0 < cone_angle <= 180:
        raise ToolError("cone_angle must be above 0 and at most 180 degrees")
    if cone_feather is not None and not 0 <= cone_feather <= 100:
        raise ToolError("cone_feather must be 0-100")
    rgba = _color("color", color) + [1.0] if color is not None else None
    return _call("set_light", comp=comp, layer=layer, type=type, intensity=intensity, color=rgba,
                 coneAngle=cone_angle, coneFeather=cone_feather, shadows=shadows)


@_tool
def audio_fade(layer: int | str, fade_in: float = 0.0, fade_out: float = 0.0, level_db: float = 0.0,
               comp: int | str | None = None) -> dict:
    """Audio level and fades of a layer with sound: fade_in / fade_out in seconds (from and to -48 dB), level_db the
    level in between (0 = unchanged, -6 = half as loud). Replaces the layer's audio level keyframes."""
    if fade_in < 0 or fade_out < 0:
        raise ToolError("fades must be 0 or more seconds")
    if not -48 < level_db <= 24:
        raise ToolError("level_db must be above -48 and at most 24")
    return _call("audio_fade", comp=comp, layer=layer, fadeIn=fade_in, fadeOut=fade_out, level=level_db)


@_tool
def audio_react(audio: int | str, layer: int | str, property: str | list[str] = "scale", amount: float = 1.0,
                name: str = "Audio Amplitude", comp: int | str | None = None) -> dict:
    """Make a property move with the music: runs Animation > Keyframe Assistant > Convert Audio to Keyframes on the
    `audio` layer (a new "Audio Amplitude" layer, renamed to `name`) and adds an expression to the target property
    that adds amplitude x amount to it (x and y for scale and position). E.g. amount 2 on scale pumps it with each
    beat."""
    if amount == 0:
        raise ToolError("amount must not be 0")
    return _call("audio_react", comp=comp, audio=audio, layer=layer, property=property, amount=amount, name=name)


# --- captions ---

_SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def _srt_seconds(stamp):
    m = _SRT_TIME.fullmatch(stamp.strip())
    if not m:
        raise ToolError(f"not an SRT time: {stamp!r}")
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _parse_srt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip().lstrip("﻿")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        timing = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing is None:
            continue
        start, end = (_srt_seconds(part.split()[0]) for part in lines[timing].split("-->")[:2])
        # After Effects breaks text lines on \r
        body = "\r".join(re.sub(r"</?[a-zA-Z][^>]*>", "", ln).strip() for ln in lines[timing + 1:])
        if body and end > start:
            cues.append({"start": start, "end": end, "text": body})
    return cues


@_tool
def captions_from_srt(path: str, comp: int | str | None = None, size: float = 60, font: str | None = None,
                      color: list[float] = [1, 1, 1], y: float = 0.85, prefix: str = "Caption",
                      offset: float = 0.0) -> dict:
    """Turn an .srt subtitle file into text layers, one per cue, timed to it (plus `offset` seconds), centered at
    `y` (0 top, 1 bottom) with the given size, font (PostScript name) and color. Any language, right-to-left
    included. Tags like <i> are dropped; line breaks are kept."""
    path = _path(path)
    try:
        with open(path, encoding="utf-8-sig") as f:
            cues = _parse_srt(f.read())
    except OSError as e:
        raise ToolError(f"cannot read {path}: {e}") from e
    if not cues:
        raise ToolError(f"no subtitles found in {path}")
    if not 0 <= y <= 1:
        raise ToolError("y must be 0-1")
    for c in cues:
        c["start"], c["end"] = round(c["start"] + offset, 3), round(c["end"] + offset, 3)
    if cues[0]["start"] < 0:
        raise ToolError("the offset moves the first subtitle before 0")
    return _call("add_captions", comp=comp, cues=cues, size=size, font=font, color=_color("color", color), y=y,
                 prefix=prefix)


# --- background rendering (aerender) ---

_JOBS = {}


def _aerender():
    exe = os.environ.get("AE_RENDER")
    if not exe:
        app = os.environ.get("AE_APP", "Adobe After Effects 2026")
        exe = f"/Applications/{app}/aerender"
    if not os.path.exists(exe):
        raise ToolError(f"aerender not found at {exe}: set AE_RENDER to its path")
    return exe


@_tool
def render_background() -> dict:
    """Render the queued items in a separate aerender process so After Effects stays free: the project is saved
    first (aerender reads the saved file). Follow it with render_background_status."""
    exe = _aerender()
    project = _call("save_project")["project"]
    log_path = os.path.join(tempfile.mkdtemp(prefix="aemcp_render_"), "aerender.log")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([exe, "-project", project], stdout=log, stderr=subprocess.STDOUT)
    _JOBS[proc.pid] = (proc, log, log_path)
    return {"job": proc.pid, "project": project, "log": log_path}


@_tool
def render_background_status(job: int) -> dict:
    """State of a render_background job: running, done or failed, with the end of aerender's log."""
    if job not in _JOBS:
        raise ToolError(f"unknown render job {job} (jobs live as long as this server)")
    proc, log, log_path = _JOBS[job]
    code = proc.poll()
    if code is not None and not log.closed:
        log.close()
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    state = "running" if code is None else ("done" if code == 0 else "failed")
    if code == 0 and any("error" in ln.lower() for ln in lines):
        state = "failed"  # aerender can exit 0 after a failed render; its log says so
    return {"job": job, "state": state, "exit_code": code, "log_tail": lines[-15:]}


# --- motion ---


@_tool
def motion_path(layer: int | str, points: list[list[float]], duration: float = 2.0, start: float = 0.0,
                times: list[float] | None = None, smooth: bool = True, ease: bool = True, constant_speed: bool = False,
                auto_orient: bool = False, comp: int | str | None = None) -> dict:
    """Move a layer along a path through points [[x, y] or [x, y, z], ...] (comp pixels), replacing its position
    keyframes: spread over `duration` seconds from `start`, or at the given `times`. smooth curves the path through the
    points (False: straight lines); the layer keeps moving through inner points, easing only at the ends (ease=False:
    none). constant_speed evens the speed over the whole path (roving keyframes); auto_orient turns the layer to
    face along the path, e.g. a car or a paper plane."""
    if len(points) < 2 or any(len(pt) not in (2, 3) for pt in points):
        raise ToolError("points needs at least 2 points [x, y] or [x, y, z]")
    if times is None:
        if duration <= 0 or start < 0:
            raise ToolError("duration must be above 0 and start 0 or more")
        times = [start + duration * i / (len(points) - 1) for i in range(len(points))]
    elif len(times) != len(points) or any(b <= a for a, b in zip(times, times[1:])) or times[0] < 0:
        raise ToolError("times needs one increasing time (0 or more) per point")
    return _call("motion_path", comp=comp, layer=layer, points=points, times=times, smooth=smooth, ease=ease,
                 constantSpeed=constant_speed, autoOrient=auto_orient)


# CSS cubic-bezier control points; the named ones follow easings.net
CURVES = {
    "ease": (0.25, 0.1, 0.25, 1), "ease_in": (0.42, 0, 1, 1), "ease_out": (0, 0, 0.58, 1),
    "ease_in_out": (0.42, 0, 0.58, 1),
    "ease_in_sine": (0.12, 0, 0.39, 0), "ease_out_sine": (0.61, 1, 0.88, 1), "ease_in_out_sine": (0.37, 0, 0.63, 1),
    "ease_in_quad": (0.11, 0, 0.5, 0), "ease_out_quad": (0.5, 1, 0.89, 1), "ease_in_out_quad": (0.45, 0, 0.55, 1),
    "ease_in_cubic": (0.32, 0, 0.67, 0), "ease_out_cubic": (0.33, 1, 0.68, 1),
    "ease_in_out_cubic": (0.65, 0, 0.35, 1),
    "ease_in_quart": (0.5, 0, 0.75, 0), "ease_out_quart": (0.25, 1, 0.5, 1), "ease_in_out_quart": (0.76, 0, 0.24, 1),
    "ease_in_quint": (0.64, 0, 0.78, 0), "ease_out_quint": (0.22, 1, 0.36, 1),
    "ease_in_out_quint": (0.83, 0, 0.17, 1),
    "ease_in_expo": (0.7, 0, 0.84, 0), "ease_out_expo": (0.16, 1, 0.3, 1), "ease_in_out_expo": (0.87, 0, 0.13, 1),
    "ease_in_circ": (0.55, 0, 1, 0.45), "ease_out_circ": (0, 0.55, 0.45, 1), "ease_in_out_circ": (0.85, 0, 0.15, 1),
    "ease_in_back": (0.36, 0, 0.66, -0.56), "ease_out_back": (0.34, 1.56, 0.64, 1),
    "ease_in_out_back": (0.68, -0.6, 0.32, 1.6),
}


@_tool
def bezier_ease(layer: int | str, property: str | list[str], curve: str | list[float] = "ease_in_out_cubic",
                keys: list[int] | None = None, comp: int | str | None = None) -> dict:
    """Shape the speed between existing keyframes with a named easing curve (ease, ease_in, ease_out, ease_in_out,
    and ease_in / ease_out / ease_in_out _sine, _quad, _cubic, _quart, _quint, _expo, _circ, _back) or a CSS
    cubic-bezier [x1, y1, x2, y2], as in a motion designer's graph editor. Every segment gets the curve, or only
    those between keys [first, last] (1-based). _back curves overshoot: not possible on position (use
    expression_preset bounce)."""
    if isinstance(curve, str):
        if curve not in CURVES:
            raise ToolError(f"curve must be a cubic-bezier [x1, y1, x2, y2] or one of: {', '.join(CURVES)}")
        pts = CURVES[curve]
    else:
        if len(curve) != 4 or not (0 <= curve[0] <= 1 and 0 <= curve[2] <= 1):
            raise ToolError("a cubic-bezier needs [x1, y1, x2, y2] with x1 and x2 0-1")
        pts = tuple(float(v) for v in curve)
    if keys is not None and len(keys) != 2:
        raise ToolError("keys needs [first, last]")
    # After Effects' influence is 0.1-100 %: x1 and 1 - x2 can not be 0
    x1, y1, x2, y2 = pts
    pts = [max(x1, 0.001), y1, min(x2, 0.999), y2]
    out = _call("bezier_ease", comp=comp, layer=layer, property=property, curve=pts, keys=keys)
    return {**out, "curve": curve}


# --- tracking ---


def _png_gray(data):
    """Decode an 8-bit RGB or RGBA PNG (what saveFrameToPng writes) to (width, rows of gray 0-255)."""
    pos, idat, w = 8, [], None
    while pos < len(data):
        n = int.from_bytes(data[pos:pos + 4], "big")
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype = body[0:4], body[4:8], body[8], body[9]
            w, h = int.from_bytes(w, "big"), int.from_bytes(h, "big")
            if depth != 8 or ctype not in (2, 6) or body[12]:
                raise ToolError("unexpected PNG from After Effects (want 8-bit RGB or RGBA, not interlaced)")
            bpp = 3 if ctype == 2 else 4
        elif kind == b"IDAT":
            idat.append(body)
        pos += 12 + n
    raw, stride, prev, rows = zlib.decompress(b"".join(idat)), w * bpp, bytearray(w * bpp), []
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            line = bytearray((a + b) & 255 for a, b in zip(line, prev))
        elif f == 3:
            for i in range(stride):
                line[i] = (line[i] + ((line[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
        elif f == 4:
            for i in range(stride):
                a, b, c = (line[i - bpp] if i >= bpp else 0), prev[i], (prev[i - bpp] if i >= bpp else 0)
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        rows.append([0.299 * line[i] + 0.587 * line[i + 1] + 0.114 * line[i + 2] for i in range(0, stride, bpp)])
    return w, rows


def _patch(img, x, y, size, step=1):
    """size x size pixels from (x, y), every `step`-th pixel averaged over step x step blocks, flattened."""
    if step == 1:
        return [v for row in img[y:y + size] for v in row[x:x + size]]
    out = []
    for by in range(y, y + size - step + 1, step):
        for bx in range(x, x + size - step + 1, step):
            out.append(sum(v for row in img[by:by + step] for v in row[bx:bx + step]) / (step * step))
    return out


def _ncc(a, b):
    """Normalized cross-correlation of two equal-length patches: 1 identical, 0 unrelated (brightness-proof)."""
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    num = va = vb = 0.0
    for x, y in zip(a, b):
        x, y = x - ma, y - mb
        num += x * y
        va += x * x
        vb += y * y
    return num / (va * vb) ** 0.5 if va and vb else 0.0


def _match(img, tmpl, feature, radius):
    """Find the template (feature x feature, flattened) in a window image of side feature + 2 radius: coarse search
    at quarter resolution, then full resolution around the best, then a parabola fit for sub-pixel. Returns the
    offset (dx, dy) of the template's top left from the window's centered position, and the correlation."""
    step = 4
    small = _patch([tmpl[i * feature:(i + 1) * feature] for i in range(feature)], 0, 0, feature, step)
    best, bx, by = -2.0, 0, 0
    for dy in range(-radius, radius + 1, step):
        for dx in range(-radius, radius + 1, step):
            s = _ncc(small, _patch(img, radius + dx, radius + dy, feature, step))
            if s > best:
                best, bx, by = s, dx, dy
    scores = {}
    for dy in range(max(-radius, by - step), min(radius, by + step) + 1):
        for dx in range(max(-radius, bx - step), min(radius, bx + step) + 1):
            scores[dx, dy] = _ncc(tmpl, _patch(img, radius + dx, radius + dy, feature))
    (bx, by), best = max(scores.items(), key=lambda kv: kv[1])

    def vertex(a, b, c):  # peak of the parabola through three scores at -1, 0, 1
        d = a - 2 * b + c
        return max(-0.5, min(0.5, (a - c) / (2 * d))) if d < 0 else 0.0

    sx = vertex(scores[bx - 1, by], best, scores[bx + 1, by]) if (bx - 1, by) in scores and (bx + 1, by) in scores \
        else 0.0
    sy = vertex(scores[bx, by - 1], best, scores[bx, by + 1]) if (bx, by - 1) in scores and (bx, by + 1) in scores \
        else 0.0
    return bx + sx, by + sy, best


@_tool
def track_point(layer: int | str, point: list[float], start: float | None = None, end: float | None = None,
                feature: int = 31, search: int = 40, tracker: str = "aemcp track", name: str = "Track Point 1",
                comp: int | str | None = None) -> dict:
    """Track a feature through footage: `point` [x, y] in the layer's own pixels (on an untransformed full-frame
    layer, comp pixels) marks a high-contrast spot (a corner, a mark, an eye), followed frame by frame from `start`
    to `end` (seconds, default the layer's in and out points). `feature` is the square it matches (pixels, odd),
    `search` how far it may move between frames. The track is written as an After Effects tracker (`tracker` /
    `name`) on the layer, visible and editable in the Tracker panel; attach_to_track makes other layers follow it.
    Returns per-frame confidence (1 = exact match): below about 0.6 the feature was lost or hidden. It renders
    each frame, about 0.2 s a frame."""
    if len(point) != 2:
        raise ToolError("point needs [x, y]")
    if not (9 <= feature <= 101 and feature % 2 == 1):
        raise ToolError("feature must be an odd number 9-101")
    if not 4 <= search <= 200:
        raise ToolError("search must be 4-200 pixels")
    size = feature + 2 * search
    win = _call("track_window", comp=comp, layer=layer, create=True, size=size)
    if win["timeRemap"]:
        _call("track_window", window=win["window"], remove=True)
        raise ToolError("time remapped layers cannot be tracked here: precompose the layer first")
    frame = win["frame"]
    t = win["inPoint"] if start is None else start
    t_end = win["outPoint"] - frame if end is None else end
    if t_end <= t:
        _call("track_window", window=win["window"], remove=True)
        raise ToolError("end must come after start")
    tmp = tempfile.mkdtemp(prefix="aemcp_track_")
    times, points, confidence = [], [], []
    x, y, vx, vy, tmpl = float(point[0]), float(point[1]), 0.0, 0.0, None
    fx, fy = x - round(x), y - round(y)  # the sub-pixel part of the start point, kept on every frame
    try:
        while t <= t_end + frame / 2:
            # the window is centered where the feature should be: last position plus last motion
            ox, oy = round(x + vx) - size // 2, round(y + vy) - size // 2
            path = os.path.join(tmp, f"{len(times)}.png")
            _call("track_window", window=win["window"], origin=[ox, oy], time=t, path=path)
            _, img = _png_gray(_wait_png(path))
            if tmpl is None:
                # ponytail: fixed first-frame template, drift-free but blind to big scale or rotation changes
                tmpl = _patch(img, search, search, feature)
                nx, ny, score = float(point[0]), float(point[1]), 1.0
            else:
                dx, dy, score = _match(img, tmpl, feature, search)
                nx, ny = ox + search + dx + feature // 2 + fx, oy + search + dy + feature // 2 + fy
                vx, vy = nx - x, ny - y
            x, y = nx, ny
            times.append(round(t, 6))
            points.append([round(x, 3), round(y, 3)])
            confidence.append(round(max(score, 0.0), 4))
            os.remove(path)
            t += frame
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _call("track_window", window=win["window"], remove=True)
    out = _call("write_track", comp=comp, layer=layer, tracker=tracker, point=name, times=times, points=points,
                confidence=confidence, feature=feature, search=size)
    lost = [times[i] for i, c in enumerate(confidence) if c < 0.6]
    return {**out, "from": times[0], "to": times[-1], "start": points[0], "end": points[-1],
            "min_confidence": min(confidence), "lost_frames": len(lost), "first_lost": lost[0] if lost else None}


@_tool
def attach_to_track(layer: int | str, tracked: int | str, tracker: int | str | None = None,
                    point: int | str | None = None, keep_offset: bool = False, comp: int | str | None = None) -> dict:
    """Make a layer follow a track point of another layer (from track_point, or tracked in After Effects' Tracker
    panel): a live expression on its position, so the text, graphic or null sticks to the tracked feature. tracker
    and point by name or index (default the newest tracker, its first point). keep_offset keeps the layer where it
    is and moves it with the track, instead of snapping it onto the point. The position must not be keyframed."""
    return _call("attach_to_track", comp=comp, layer=layer, tracked=tracked, tracker=tracker, point=point,
                 keepOffset=keep_offset or None)


# --- 3D ---


@_tool
def set_camera(layer: int | str, focal_length: float | None = None, depth_of_field: bool | None = None,
               focus_distance: float | None = None, focus_on: int | str | None = None, f_stop: float | None = None,
               blur_level: float | None = None, comp: int | str | None = None) -> dict:
    """Set up a camera's lens: focal_length in mm (a 36 mm film back: 15 wide, 50 normal, 200 telephoto),
    depth_of_field on or off, focus by focus_distance (pixels from the camera) or focus_on a layer (autofocus: an
    expression keeps that layer sharp as things move), f_stop for how shallow the focus is (1.4 very blurry, 16 all
    sharp), blur_level in %."""
    if focal_length is not None and not 1 <= focal_length <= 2000:
        raise ToolError("focal_length must be 1-2000 mm")
    if focus_distance is not None and focus_on is not None:
        raise ToolError("give focus_distance or focus_on, not both")
    if focus_distance is not None and focus_distance <= 0:
        raise ToolError("focus_distance must be above 0")
    if f_stop is not None and not 0.5 <= f_stop <= 64:
        raise ToolError("f_stop must be 0.5-64")
    if blur_level is not None and not 0 <= blur_level <= 1000:
        raise ToolError("blur_level must be 0-1000 %")
    return _call("set_camera", comp=comp, layer=layer, focalLength=focal_length, depthOfField=depth_of_field,
                 focusDistance=focus_distance, focusOn=focus_on, fStop=f_stop, blurLevel=blur_level)


BEVELS = {"none": 1, "angular": 2, "concave": 3, "convex": 4}


@_tool
def set_3d_layer(layer: int | str, extrusion: float | None = None, bevel: float | None = None,
                 bevel_style: str = "angular", casts_shadows: bool | None = None,
                 accepts_shadows: bool | None = None, accepts_lights: bool | None = None,
                 specular: float | None = None, shininess: float | None = None, metal: float | None = None,
                 reflection: float | None = None, comp: int | str | None = None) -> dict:
    """Make a layer 3D and shape it: extrusion depth in pixels and a bevel (depth in pixels; bevel_style angular,
    concave, convex or none) turn flat text and shapes into solid 3D objects (the comp switches to the Advanced 3D
    renderer); material: casts_shadows, accepts_shadows, accepts_lights, specular and shininess (%, how glossy),
    metal (%, how much highlights take the layer's color), reflection (%). Light it with lights (set_light)."""
    if bevel_style not in BEVELS:
        raise ToolError(f"bevel_style must be one of: {', '.join(BEVELS)}")
    if extrusion is not None and extrusion < 0 or bevel is not None and bevel < 0:
        raise ToolError("extrusion and bevel must be 0 or more pixels")
    for n, v in (("specular", specular), ("shininess", shininess), ("metal", metal), ("reflection", reflection)):
        if v is not None and not 0 <= v <= 100:
            raise ToolError(f"{n} must be 0-100 %")
    return _call("set_3d_layer", comp=comp, layer=layer, extrusion=extrusion, bevel=bevel,
                 bevelStyle=BEVELS[bevel_style], castsShadows=casts_shadows, acceptsShadows=accepts_shadows,
                 acceptsLights=accepts_lights, specular=specular, shininess=shininess, metal=metal,
                 reflection=reflection)


@_tool
def depth_stack(layers: list[int | str], spacing: float = 500, compensate: bool = True,
                camera: int | str | None = None, comp: int | str | None = None) -> dict:
    """2.5D parallax from flat layers: turns them 3D and spreads them back in depth, `spacing` pixels apart, in the
    given order (first nearest, e.g. foreground, subject, sky), scaled up (compensate) so each still looks the same
    size from the camera (the comp's first camera, or a new one). Then move the camera (camera_move) and the layers
    slide past each other at different speeds."""
    if len(layers) < 2:
        raise ToolError("give at least 2 layers")
    if spacing <= 0:
        raise ToolError("spacing must be above 0")
    return _call("depth_stack", comp=comp, layers=layers, spacing=spacing, compensate=compensate, camera=camera)


# --- Essential Graphics ---


@_tool
def mogrt_add_property(layer: int | str, property: str | list[str], name: str | None = None,
                       comp: int | str | None = None) -> dict:
    """Expose a property in the comp's Essential Graphics panel (e.g. a title's Source Text or a color) under
    `name`, for a Motion Graphics template editors can change in Premiere Pro."""
    return _call("mogrt_add_property", comp=comp, layer=layer, property=property, name=name)


@_tool
def export_mogrt(path: str, name: str | None = None, comp: int | str | None = None) -> dict:
    """Export the comp as a Motion Graphics template (.mogrt) with its Essential Graphics properties."""
    path = _path(path)
    if not path.lower().endswith(".mogrt"):
        raise ToolError("the path must end in .mogrt")
    return _call("export_mogrt", comp=comp, path=path, name=name)


# --- keyframe work, editing, variants, footage ---


@_tool
def bake_expression(layer: int | str, property: str | list[str], step: int = 1, start: float | None = None,
                    end: float | None = None, comp: int | str | None = None) -> dict:
    """Turn a property's expression into keyframes: its value is sampled every `step` frames from `start` to `end`
    (default the layer's in and out points), written as keyframes, and the expression is switched off (kept, so it
    can be switched back on). Use it to hand-edit a wiggle, or to speed up heavy expressions."""
    if not 1 <= step <= 100:
        raise ToolError("step must be 1-100 frames")
    return _call("bake_expression", comp=comp, layer=layer, property=property, step=step, start=start, end=end)


@_tool
def ease_keyframes(layer: int | str, property: str | list[str], ease: str = "ease", influence: float = 33.33,
                   comp: int | str | None = None) -> dict:
    """Re-ease every keyframe of a property: ease, ease_in, ease_out, linear or hold, with `influence` 0.1-100 for
    the easing strength (e.g. 75 for a strong ease)."""
    if ease not in EASES:
        raise ToolError(f"ease must be one of: {', '.join(EASES)}")
    if not 0.1 <= influence <= 100:
        raise ToolError("influence must be 0.1-100")
    return _call("ease_keyframes", comp=comp, layer=layer, property=property, ease=ease, influence=influence)


@_tool
def copy_keyframes(layer: int | str, property: str | list[str], targets: list[int | str], offset: float = 0.0,
                   comp: int | str | None = None) -> dict:
    """Copy a property's keyframes, with their interpolation and easing, to other layers (replacing theirs). offset
    cascades: the first target starts `offset` seconds later, the second 2 x offset, and so on."""
    if not targets:
        raise ToolError("no targets")
    return _call("copy_keyframes", comp=comp, layer=layer, property=property, targets=targets, offset=offset)


@_tool
def stagger_layers(layers: list[int | str], offset: float = 0.1, start: float | None = None,
                   comp: int | str | None = None) -> dict:
    """Offset layers' start times by `offset` seconds each, in the given order (the classic stagger), starting at
    `start` (default the first layer's in point). Keyframes move with their layers."""
    if not layers:
        raise ToolError("no layers")
    return _call("stagger_layers", comp=comp, layers=layers, offset=offset, start=start)


@_tool
def split_layer(layer: int | str, time: float, name: str | None = None, comp: int | str | None = None) -> dict:
    """Split a layer at `time` (seconds): the original ends there and a copy right above it starts there."""
    return _call("split_layer", comp=comp, layer=layer, time=time, name=name)


@_tool
def trim_comp(to: str = "layers", comp: int | str | None = None) -> dict:
    """Trim a comp to its layers (from the earliest in point to the latest out point) or to its work area; layers
    move so the kept part starts at 0."""
    if to not in ("layers", "work_area"):
        raise ToolError("to must be layers or work_area")
    return _call("trim_comp", comp=comp, to=to)


@_tool
def make_variants(rows: list[dict], comp: int | str | None = None, output_dir: str | None = None,
                  template: str | None = None) -> dict:
    """Versions of a template comp from data: each row {"name": "...", "texts": {"<text layer>": "<text>"}} gets a
    duplicate of the comp named `name` with those texts. With output_dir each one is queued to render there (name
    as the file name; template from render_templates); then call render or render_background."""
    if not rows:
        raise ToolError("no rows")
    names = [r.get("name") for r in rows if isinstance(r, dict)]
    if len(names) != len(rows) or not all(isinstance(n, str) and n.strip() for n in names):
        raise ToolError('each row needs a "name" and "texts"')
    if len(set(names)) != len(names):
        raise ToolError("row names must be unique (they name the comps and the files)")
    if any(any(c in n for c in '/\\:*?"<>|') for n in names):
        raise ToolError("row names are file names: no / \\ : * ? \" < > |")
    if any(not isinstance(r.get("texts", {}), dict) for r in rows):
        raise ToolError('"texts" maps text layer names to their text')
    rows = [{"name": r["name"], "texts": r.get("texts", {})} for r in rows]
    if output_dir:
        output_dir = _path(output_dir)
        os.makedirs(output_dir, exist_ok=True)
    return _call("make_variants", comp=comp, rows=rows, outputDir=output_dir, template=template)


LAYERED_MODES = ("comp_cropped", "comp", "footage")


@_tool
def import_layered(path: str, mode: str = "comp_cropped") -> dict:
    """Import a layered Photoshop or Illustrator file: as a comp with each layer cropped to its content
    (comp_cropped, best for animating), as a comp with layers at document size (comp), or flattened (footage)."""
    if mode not in LAYERED_MODES:
        raise ToolError(f"mode must be one of: {', '.join(LAYERED_MODES)}")
    return _call("import_layered", path=_path(path), mode=mode)


@_tool
def find_missing_footage(search: str | None = None, max_files: int = 200000) -> dict:
    """Footage whose file is missing. With search (a folder), each missing file is looked for by name under it
    (recursively, up to max_files files) and relinked when found."""
    missing = _call("missing_footage")
    out = {"missing": missing}
    if not search:
        return out
    root = _path(search)
    if not os.path.isdir(root):
        raise ToolError(f"folder not found: {root}")
    wanted = {os.path.basename(m["file"]).lower(): m for m in missing if m.get("file")}
    found, seen = {}, 0
    for dirpath, _, files in os.walk(root):
        for fn in files:
            seen += 1
            if fn.lower() in wanted and fn.lower() not in found:
                found[fn.lower()] = os.path.join(dirpath, fn)
        if seen >= max_files or len(found) == len(wanted):
            break
    links = [{"id": wanted[k]["id"], "path": p} for k, p in found.items()]  # empty when nothing is missing
    out["relinked"] = _call("relink_footage", links=links) if links else []
    out["still_missing"] = [m["name"] for m in missing if m.get("file") is None or
                            os.path.basename(m["file"]).lower() not in found]
    out["files_searched"] = seen
    return out


# --- shape animation, text, rhythm, project ---


@_tool
def trim_paths(layer: int | str, duration: float = 1.0, start: float | None = None, erase: bool = False,
               offset: float = 0.0, ease: bool = True, comp: int | str | None = None) -> dict:
    """Draw a shape layer's strokes on (or erase=True: off) with Trim Paths over `duration` seconds from `start`
    (default the layer's in point); offset (degrees) rotates where the line starts. Works best on stroked shapes
    (add_shape with a stroke)."""
    if duration <= 0:
        raise ToolError("duration must be above 0")
    return _call("trim_paths", comp=comp, layer=layer, duration=duration, start=start, erase=erase or None,
                 offset=offset or None, ease=ease)


@_tool
def shape_repeater(layer: int | str, copies: int = 5, offset: list[float] = [100, 0], scale: float = 100,
                   rotation: float = 0, end_opacity: float = 100, comp: int | str | None = None) -> dict:
    """Repeat a shape layer's contents: `copies` copies, each moved by offset [x, y] px, scaled by `scale` % and
    rotated by `rotation` degrees from the previous one, fading to end_opacity % on the last."""
    if not 1 <= copies <= 500:
        raise ToolError("copies must be 1-500")
    if len(offset) != 2:
        raise ToolError("offset needs [x, y]")
    if not 0 <= end_opacity <= 100:
        raise ToolError("end_opacity must be 0-100")
    return _call("shape_repeater", comp=comp, layer=layer, copies=copies, offset=[float(v) for v in offset],
                 scale=scale, rotation=rotation, endOpacity=end_opacity)


@_tool
def text_style(layer: int | str, tracking: float | None = None, leading: float | None = None,
               stroke_color: list[float] | None = None, stroke_width: float | None = None,
               all_caps: bool | None = None, comp: int | str | None = None) -> dict:
    """Typography of a text layer: tracking (letter spacing, 1/1000 em), leading (line spacing in px), an outline
    stroke (color [r, g, b] 0-1 and width px), all caps. Use set_text for text, font, size, fill and alignment."""
    if stroke_width is not None and stroke_width < 0:
        raise ToolError("stroke_width must be 0 or more")
    if leading is not None and leading <= 0:
        raise ToolError("leading must be above 0")
    value = {k: v for k, v in (("tracking", tracking), ("leading", leading),
                               ("strokeColor", _color("stroke_color", stroke_color)), ("strokeWidth", stroke_width),
                               ("allCaps", all_caps)) if v is not None}
    if not value:
        raise ToolError("nothing to change")
    return _call("set_property", comp=comp, layer=layer, property="text", value=value)


@_tool
def add_paragraph(text: str, box: list[float] = [800, 400], name: str | None = None,
                  comp: int | str | None = None) -> dict:
    """Add paragraph text: a text layer that wraps inside a box [width, height] in pixels. Style it with set_text
    and text_style."""
    if len(box) != 2 or min(box) <= 0:
        raise ToolError("box needs [width, height] above 0")
    return _call("add_paragraph", comp=comp, text=text, box=[float(v) for v in box], name=name)


@_tool
def set_motion_blur(on: bool = True, layers: str | list[int | str] | None = "all", shutter_angle: float | None = None,
                    shutter_phase: float | None = None, comp: int | str | None = None) -> dict:
    """Motion blur: the comp switch, and the layers' switches (layers "all", a list, or None to leave them);
    shutter_angle 0-720 degrees (180 is filmic), shutter_phase -360 to 360."""
    if shutter_angle is not None and not 0 <= shutter_angle <= 720:
        raise ToolError("shutter_angle must be 0-720")
    if shutter_phase is not None and not -360 <= shutter_phase <= 360:
        raise ToolError("shutter_phase must be -360 to 360")
    if isinstance(layers, str) and layers != "all":
        raise ToolError('layers must be "all", a list of layers, or None')
    return _call("set_motion_blur", comp=comp, on=on, layers=layers, shutterAngle=shutter_angle,
                 shutterPhase=shutter_phase)


def _onsets(path, sensitivity, min_gap):
    """Times (s) of sharp rises in loudness in a PCM WAV: an energy envelope in 10 ms hops, the positive change,
    peaks above mean + sensitivity x deviation, at least min_gap apart."""
    try:
        w = wave.open(path, "rb")
    except (wave.Error, EOFError, OSError) as e:
        raise ToolError(f"{os.path.basename(path)} is not a PCM WAV ({e}); beat markers read WAV files") from None
    with w:
        rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        pcm = array("h", raw)
    elif width in (3, 4):  # the top two bytes are enough for loudness
        hi = bytearray(len(raw) // width * 2)
        hi[0::2], hi[1::2] = raw[width - 2::width], raw[width - 1::width]
        pcm = array("h", bytes(hi))
    else:
        raise ToolError(f"unsupported WAV sample width: {8 * width}-bit")
    if sys.byteorder != "little":
        pcm.byteswap()
    hop = max(1, rate // 100) * ch
    env = []
    for i in range(0, len(pcm) - hop + 1, hop):
        chunk = pcm[i:i + hop:max(1, ch * (rate // 8000))]
        env.append(math.sqrt(math.fsum(x * x for x in chunk) / max(1, len(chunk))))
    rise = [max(0.0, b - a) for a, b in zip(env, env[1:])]
    if not rise or max(rise) == 0:
        return []
    mean = math.fsum(rise) / len(rise)
    dev = math.sqrt(math.fsum((r - mean) ** 2 for r in rise) / len(rise))
    floor, gap, out = mean + sensitivity * dev, int(min_gap * 100), []
    for i in range(1, len(rise) - 1):
        if rise[i] > floor and rise[i] >= rise[i - 1] and rise[i] >= rise[i + 1]:
            if out and i - out[-1] < gap:
                continue
            out.append(i)
    return [round((i + 1) / 100, 3) for i in out]  # the rise ends at hop i + 1


@_tool
def markers_from_audio(layer: int | str, sensitivity: float = 1.5, min_gap: float = 0.25, max_markers: int = 300,
                       comment: str = "beat", comp: int | str | None = None) -> dict:
    """Put comp markers on the beats (sharp rises in loudness) of an audio layer's WAV file, within the layer's
    trim: for cutting and animating to the music. Raise sensitivity for fewer, stronger hits; min_gap (s) keeps
    markers apart. Analysis runs here, not in After Effects. Then sequence_to_markers cuts layers to them."""
    if sensitivity <= 0 or min_gap <= 0:
        raise ToolError("sensitivity and min_gap must be above 0")
    src = _call("audio_source", comp=comp, layer=layer)
    times = [src["startTime"] + t for t in _onsets(src["file"], sensitivity, min_gap)]
    times = [round(t, 3) for t in times if src["inPoint"] <= t < src["outPoint"]][:max_markers]
    if not times:
        raise ToolError("no beats found: lower the sensitivity")
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    bpm = round(60 / gaps[len(gaps) // 2], 1) if gaps else None
    out = _call("add_markers", comp=comp, times=times, comment=comment)
    return {**out, "bpm_estimate": bpm, "first": times[:8]}


@_tool
def sequence_to_markers(layers: list[int | str], trim: bool = True, comp: int | str | None = None) -> dict:
    """Place layers on the comp's markers in order: the first starts at the first marker, the second at the second,
    and so on; trim=True also cuts each at the next marker (a cut on every beat)."""
    if not layers:
        raise ToolError("no layers")
    return _call("sequence_to_markers", comp=comp, layers=layers, trim=trim)


@_tool
def reduce_project(comps: list[int | str]) -> dict:
    """Remove everything the given comps do not use (other comps, footage, empty folders): File > Dependencies >
    Reduce Project. Save a copy first if unsure."""
    if not comps:
        raise ToolError("name the comps to keep")
    return _call("reduce_project", comps=comps)


@_tool
def render_queue_list() -> list[dict]:
    """The render queue: index, comp, status (queued, done, rendering...) and output file of each item."""
    return _call("render_queue_list")


@_tool
def clear_render_queue(all_items: bool = False) -> dict:
    """Remove finished (done) items from the render queue, or every item with all_items=True; an item rendering is
    kept."""
    return _call("clear_render_queue", all=all_items or None)


# --- understanding and organizing comps, layout, counters ---


@_tool
def describe_comp(comp: int | str | None = None) -> dict:
    """What a comp does, layer by layer: type, timing, parent, effects, masks, label, 3D, every animated property
    (path, keyframe count, first and last key time) and every expression. Read-only: start here to understand an
    existing comp before changing it."""
    return _call("describe_comp", comp=comp)


LAYER_TYPES = ("text", "shape", "solid", "null", "adjustment", "camera", "light", "footage", "precomp")


@_tool
def find_layers(name: str | None = None, type: str | None = None, effect: str | None = None,
                animated: bool | None = None, expression: bool | None = None,
                comp: int | str | None = None) -> list[dict]:
    """Find layers across every comp (or one): by name fragment, type (text, shape, solid, null, adjustment,
    camera, light, footage, precomp), an effect's name fragment, whether they have keyframes, and whether they
    have expressions. Filters combine."""
    if type is not None and type not in LAYER_TYPES:
        raise ToolError(f"type must be one of: {', '.join(LAYER_TYPES)}")
    if all(v is None for v in (name, type, effect, animated, expression)):
        raise ToolError("give at least one filter")
    return _call("find_layers", comp=comp, name=name, type=type, effect=effect, animated=animated,
                 expression=expression)


@_tool
def rename_layers(find: str | None = None, replace: str = "", pattern: str | None = None, start_at: int = 1,
                  ignore_case: bool = False, layers: list[int | str] | None = None,
                  comp: int | str | None = None) -> dict:
    """Rename layers (all of a comp, or `layers` in the given order): find/replace with a regular expression, or a
    pattern where {n} is a running number from start_at and {name} the old name, e.g. "Shot {n}" or "BG - {name}".
    Returns the old and new names."""
    if (find is None) == (pattern is None):
        raise ToolError("give find (and replace) or pattern")
    if find is not None:
        if not find:
            raise ToolError("find must not be empty")
        try:
            re.compile(find)
        except re.error as e:
            raise ToolError(f"find is not a valid regular expression: {e}") from None
    if pattern is not None and "{n}" not in pattern and "{name}" not in pattern:
        raise ToolError("pattern needs {n} and/or {name}, or every layer would get the same name")
    return _call("rename_layers", comp=comp, find=find, replace=replace, pattern=pattern, startAt=start_at,
                 ignoreCase=ignore_case or None, layers=layers)


LABEL_COLORS = ("none", "red", "yellow", "aqua", "pink", "lavender", "peach", "sea_foam", "blue", "green", "purple",
                "orange", "brown", "fuchsia", "cyan", "sandstone", "dark_green")


@_tool
def set_label(layers: list[int | str], color: str, comp: int | str | None = None) -> dict:
    """Set layers' label color: none, red, yellow, aqua, pink, lavender, peach, sea_foam, blue, green, purple,
    orange, brown, fuchsia, cyan, sandstone or dark_green (After Effects' default label names)."""
    if color not in LABEL_COLORS:
        raise ToolError(f"color must be one of: {', '.join(LABEL_COLORS)}")
    if not layers:
        raise ToolError("no layers")
    return _call("set_label", comp=comp, layers=layers, color=color)


ALIGNS = ("left", "center", "right", "top", "middle", "bottom")


@_tool
def align_layers(layers: list[int | str], align: str, to: str = "comp", comp: int | str | None = None) -> dict:
    """Align layers' boxes (what they draw, scaled): left, center, right, top, middle or bottom, to the comp or to
    the selection (the box around all the given layers). Unrotated, unparented layers with a static position."""
    if align not in ALIGNS:
        raise ToolError(f"align must be one of: {', '.join(ALIGNS)}")
    if to not in ("comp", "selection"):
        raise ToolError("to must be comp or selection")
    if not layers or (to == "selection" and len(layers) < 2):
        raise ToolError("give the layers (at least 2 to align to the selection)")
    return _call("align_layers", comp=comp, layers=layers, align=align, to=to)


@_tool
def distribute_layers(layers: list[int | str], axis: str = "x", comp: int | str | None = None) -> dict:
    """Space layers evenly along x or y: the outermost two stay, the others' centers are spread between them."""
    if axis not in ("x", "y"):
        raise ToolError("axis must be x or y")
    if len(layers) < 3:
        raise ToolError("distributing needs at least 3 layers")
    return _call("distribute_layers", comp=comp, layers=layers, axis=axis)


@_tool
def grid_layout(layers: list[int | str], columns: int = 3, gap: float = 20, margin: float = 40, fit: bool = True,
                comp: int | str | None = None) -> dict:
    """Arrange layers in a grid filling the comp, row by row in the given order: `columns` columns, `gap` px
    between cells, `margin` px around; fit=True scales each layer to fit its cell (keeping its proportions)."""
    if not layers:
        raise ToolError("no layers")
    if not 1 <= columns <= 50:
        raise ToolError("columns must be 1-50")
    if gap < 0 or margin < 0:
        raise ToolError("gap and margin must be 0 or more")
    return _call("grid_layout", comp=comp, layers=layers, columns=columns, gap=gap, margin=margin, fit=fit)


@_tool
def comp_from_footage(item: int | str, name: str | None = None, still_duration: float = 5.0,
                      frame_rate: float = 30.0) -> dict:
    """Make a comp that matches a footage item (size, pixel aspect, frame rate, duration) with the footage in it,
    and open it. Stills get still_duration seconds at frame_rate."""
    if still_duration <= 0 or frame_rate <= 0:
        raise ToolError("still_duration and frame_rate must be above 0")
    return _call("comp_from_footage", item=item, name=name, stillDuration=still_duration, frameRate=frame_rate)


@_tool
def number_counter(from_value: float = 0, to_value: float = 100, duration: float = 2.0, start: float = 0.0,
                   decimals: int = 0, prefix: str = "", suffix: str = "", separator: str = "", ease: bool = True,
                   layer: int | str | None = None, name: str | None = None, comp: int | str | None = None) -> dict:
    """An animated number: a Slider Control named Counter keyframed from from_value to to_value over `duration`
    seconds, shown by an expression on a text layer's Source Text (a new text layer unless `layer` names one) with
    `decimals`, a prefix and suffix (e.g. "$", " %") and a thousands separator (e.g. ","). Style it with set_text."""
    if not 0 <= decimals <= 6:
        raise ToolError("decimals must be 0-6")
    if duration <= 0 or start < 0:
        raise ToolError("duration must be above 0 and start 0 or more")
    if len(separator) > 1:
        raise ToolError("separator is one character, e.g. ',' or ' '")
    return _call("number_counter", comp=comp, layer=layer, name=name, **{"from": from_value, "to": to_value},
                 duration=duration, start=start, decimals=decimals, prefix=prefix, suffix=suffix,
                 separator=separator, ease=ease)


@_tool
def run_jsx(code: str) -> Any:
    """Run ExtendScript in After Effects and return its value (numbers, strings, arrays; other objects as text).
    For anything the other tools do not cover; runs inside one undo group."""
    return _call("run_jsx", code=code)


if __name__ == "__main__":
    _setup_logging()
    log.info("starting", extra={"fields": {"platform": sys.platform, "pid": os.getpid()}})
    mcp.run()
