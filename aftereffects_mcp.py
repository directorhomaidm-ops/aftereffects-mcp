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
    "paths and easing curves, point tracking, cameras, extruded 3D and parallax, color grading looks, cutting to the "
    "beat, transitions, speed ramps, animated mask reveals, green screen keying, camera shake, glitches, weather, "
    "letterbox, arrows, spotlights, text boxes, timers, split screens, picture-in-picture, region blur, audio "
    "visualizers, entrances and exits, lower thirds, Ken Burns slideshows, progress bars, gradients, social "
    "reframing, corner pin, neon, silence cutting, music ducking, charts, film looks, bounces, paths, orbits, "
    "import, the render queue and frame export. Start with status and list_items, then comp_info to see a comp's layers. "
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


def _ffmpeg_wav(path):
    """Decode a file's audio to a temporary 16 kHz mono WAV with ffmpeg; the caller deletes it."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ToolError(f"{os.path.basename(path)} is not a PCM WAV; install ffmpeg to read audio from other files "
                        "(brew install ffmpeg)")
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="aemcp_")
    os.close(fd)
    r = subprocess.run([exe, "-v", "error", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", out],
                       capture_output=True, text=True)
    if r.returncode:
        os.unlink(out)
        raise ToolError(f"ffmpeg could not read {os.path.basename(path)}: {r.stderr.strip()[:300]}")
    return out


def _envelope(path):
    """Loudness of a file's audio in 10 ms hops: RMS of 16-bit samples. Reads PCM WAV here; anything else (a
    video's sound) through ffmpeg."""
    try:
        w = wave.open(path, "rb")
    except (wave.Error, EOFError, OSError):
        tmp = _ffmpeg_wav(path)
        try:
            return _envelope(tmp)
        finally:
            os.unlink(tmp)
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
    return env


def _onsets(path, sensitivity, min_gap):
    """Times (s) of sharp rises in loudness: the energy envelope's positive change, peaks above mean + sensitivity x
    deviation, at least min_gap apart."""
    env = _envelope(path)
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


# --- keyframe timing, footage, colors, text, fonts, collecting ---


@_tool
def retime_keyframes(layer: int | str, property: str | list[str] | None = None, offset: float = 0.0,
                     scale: float = 1.0, reverse: bool = False, anchor: float | None = None,
                     comp: int | str | None = None) -> dict:
    """Move, stretch or reverse a layer's keyframes, keeping values and easing: each key goes to
    anchor + (time - anchor) * scale + offset (anchor defaults to the first key; scale 2 is twice as slow, 0.5 twice
    as fast); reverse plays the animation backwards over the same time. With no property, every animated property
    on the layer moves together (markers stay). Custom spatial (motion path) handles go back to auto."""
    if scale <= 0:
        raise ToolError("scale must be above 0")
    if not offset and scale == 1 and not reverse:
        raise ToolError("nothing to do: pass offset, scale or reverse")
    return _call("retime_keyframes", comp=comp, layer=layer, property=property, offset=offset, scale=scale,
                 reverse=reverse or None, anchor=anchor)


ALPHA_MODES = ("straight", "premultiplied", "ignore")


@_tool
def interpret_footage(item: int | str, frame_rate: float | None = None, alpha: str | None = None,
                      invert_alpha: bool | None = None, loop: int | None = None,
                      pixel_aspect: float | None = None) -> dict:
    """Interpret Footage: conform the frame rate (0 = the file's own), alpha (straight, premultiplied, ignore),
    invert alpha, loop count, pixel aspect. Every layer using the item follows. Returns the item's settings."""
    if frame_rate is not None and not 0 <= frame_rate <= 999:
        raise ToolError("frame_rate must be 0-999 (0 = the file's own rate)")
    if alpha is not None and alpha not in ALPHA_MODES:
        raise ToolError(f"alpha must be one of: {', '.join(ALPHA_MODES)}")
    if loop is not None and not 1 <= loop <= 9999:
        raise ToolError("loop must be 1-9999")
    if pixel_aspect is not None and not 0.01 <= pixel_aspect <= 100:
        raise ToolError("pixel_aspect must be 0.01-100")
    return _call("interpret_footage", item=item, frameRate=frame_rate, alpha=alpha, invertAlpha=invert_alpha,
                 loop=loop, pixelAspect=pixel_aspect)


def _rgb(name, v):
    if isinstance(v, str):
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", v.strip())
        if not m:
            raise ToolError(f"{name} needs a hex color like #FF8800, or [r, g, b] 0-1")
        return [round(int(m.group(1)[i:i + 2], 16) / 255, 6) for i in (0, 2, 4)]
    return _color(name, v)


@_tool
def replace_color(find: str | list[float], replace: str | list[float], tolerance: float = 0.02,
                  layers: list[int | str] | None = None, comp: int | str | None = None,
                  all_comps: bool = False) -> dict:
    """Swap one color for another everywhere it is used: shape fills and strokes, effect colors, text fill and
    stroke, solid colors, including keyframed values. Colors are hex (#FF8800) or [r, g, b] 0-1; tolerance (0-1)
    also catches near matches. Limit it to some layers, or run over every comp with all_comps. A solid's color is
    shared by all layers using that solid."""
    if not 0 <= tolerance <= 1:
        raise ToolError("tolerance must be 0-1")
    if all_comps and (comp is not None or layers):
        raise ToolError("all_comps covers every comp: leave out comp and layers")
    return _call("replace_color", comp=comp, allComps=all_comps or None, layers=layers,
                 find=_rgb("find", find), replace=_rgb("replace", replace), tolerance=tolerance)


@_tool
def fit_text(layer: int | str, width: float, height: float | None = None, min_size: float = 6,
             max_size: float = 1296, comp: int | str | None = None) -> dict:
    """Set a text layer's font size so the text fills `width` (and fits `height`, when given) in comp pixels,
    counting the layer's scale. The size stays within min_size-max_size."""
    if width <= 0 or (height is not None and height <= 0):
        raise ToolError("width and height must be above 0")
    if not 0 < min_size <= max_size <= 1296:
        raise ToolError("need 0 < min_size <= max_size <= 1296")
    return _call("fit_text", comp=comp, layer=layer, width=width, height=height, minSize=min_size, maxSize=max_size)


SORT_KEYS = ("name", "in_point", "duration", "label", "type")


@_tool
def sort_layers(by: str = "name", reverse: bool = False, layers: list[int | str] | None = None,
                comp: int | str | None = None) -> dict:
    """Reorder layers in the stack by name, in_point, duration, label or type (first on top; reverse flips it).
    With `layers`, only those are sorted, into the positions they already hold; the others stay put."""
    if by not in SORT_KEYS:
        raise ToolError(f"by must be one of: {', '.join(SORT_KEYS)}")
    if layers is not None and len(layers) < 2:
        raise ToolError("sorting needs at least 2 layers")
    return _call("sort_layers", comp=comp, by=by, reverse=reverse or None, layers=layers)


def _copy_new(src, folder):
    # copy without overwriting: name.ext, name_2.ext, ...
    stem, ext = os.path.splitext(os.path.basename(src))
    dst, n = os.path.join(folder, stem + ext), 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(folder, f"{stem}_{n}{ext}")
    shutil.copy2(src, dst)
    return dst


def _sequence_files(first):
    # the frames of an image sequence: same prefix and extension, a frame number in place of the digits
    folder, base = os.path.split(first)
    m = re.fullmatch(r"(.*?)(\d+)(\.\w+)", base)
    if not m:
        return [first]
    pat = re.compile(re.escape(m.group(1)) + r"\d+" + re.escape(m.group(3)) + "$")
    return sorted(os.path.join(folder, f) for f in os.listdir(folder) if pat.match(f))


@_tool
def collect_files(folder: str, name: str | None = None) -> dict:
    """Collect the project into `folder`: copy every footage file it uses into folder/Footage (image sequences
    into a subfolder each; files are never overwritten), relink the items to the copies and save the project there
    as `name`.aep (default: the project's name). The open project becomes the collected one. Missing footage is
    listed, not copied."""
    root = _path(folder)
    if os.path.exists(root) and not os.path.isdir(root):
        raise ToolError(f"not a folder: {root}")
    if name is not None and (not name.strip() or os.sep in name or name.startswith(".")):
        raise ToolError("name is a file name without folders, e.g. 'my project'")
    items = _call("footage_files")
    footage = os.path.join(root, "Footage")
    os.makedirs(footage, exist_ok=True)
    copies, links, missing, size = {}, [], [], 0
    for it in items:
        if it["missing"]:
            missing.append(it["name"])
            continue
        src = it["file"]
        if src not in copies:
            if it["sequence"]:
                frames = _sequence_files(src)
                sub = os.path.join(footage, os.path.splitext(os.path.basename(src))[0].rstrip("0123456789_-. ") or "frames")
                n, dest = 1, sub
                while os.path.exists(dest):
                    n += 1
                    dest = f"{sub}_{n}"
                os.makedirs(dest)
                for f in frames:
                    shutil.copy2(f, os.path.join(dest, os.path.basename(f)))
                    size += os.path.getsize(f)
                copies[src] = os.path.join(dest, os.path.basename(src))
            else:
                copies[src] = _copy_new(src, footage)
                size += os.path.getsize(src)
        links.append({"id": it["id"], "path": copies[src], "sequence": it["sequence"] or None})
    relinked = _call("relink_footage", links=links) if links else []
    if name is None:
        project = _call("status").get("project")
        name = os.path.splitext(os.path.basename(project))[0] if project else "project"
    target = os.path.join(root, name if name.endswith(".aep") else name + ".aep")
    saved = _call("save_project", path=target)
    return {"project": saved["project"], "files": len(copies), "bytes": size, "relinked": len(relinked),
            "missing": missing}


@_tool
def replace_text(find: str, replace: str, regex: bool = False, ignore_case: bool = False,
                 comp: int | str | None = None, all_comps: bool = False) -> dict:
    """Find and replace in the text of every text layer (keyframed Source Text included) in a comp, or in every
    comp with all_comps. With regex, `replace` can use $1, $2. Text driven by an expression is skipped and listed.
    Styling (font, size, color) stays."""
    if not find:
        raise ToolError("find is empty")
    if regex:
        try:
            re.compile(find)
        except re.error as e:
            raise ToolError(f"bad regex: {e}")
    if all_comps and comp is not None:
        raise ToolError("all_comps covers every comp: leave out comp")
    return _call("replace_text", comp=comp, allComps=all_comps or None, find=find, replace=replace,
                 regex=regex or None, ignoreCase=ignore_case or None)


@_tool
def list_fonts() -> list:
    """The fonts the project's text layers use (PostScript names), each with its layers ("comp > layer") and
    whether it is installed (null when After Effects cannot tell)."""
    return _call("list_fonts")


@_tool
def replace_font(find: str, replace: str, comp: int | str | None = None, all_comps: bool = True) -> dict:
    """Swap one font for another in every text layer that uses it (keyframed Source Text included). Fonts are
    PostScript names as list_fonts shows them, e.g. Montserrat-Bold; the new one must be installed. Every comp by
    default, or one `comp`."""
    if not find.strip() or not replace.strip():
        raise ToolError("find and replace are PostScript font names, e.g. ArialMT")
    return _call("replace_font", comp=comp, allComps=(all_comps and comp is None) or None, find=find.strip(),
                 replace=replace.strip())


# --- color grading, editing, masks, keying, effects ---

# Lumetri Basic Correction ranges, and the Creative and Vignette ones
GRADE_BASIC = {"exposure": ("Exposure", -5, 5), "contrast": ("Contrast", -100, 100),
               "highlights": ("Highlights", -100, 100), "shadows": ("Shadows", -100, 100),
               "whites": ("Whites", -100, 100), "blacks": ("Blacks", -100, 100),
               "temperature": ("Temperature", -100, 100), "tint": ("Tint", -100, 100),
               "saturation": ("Saturation", 0, 200)}
GRADE_CREATIVE = {"faded_film": ("Faded Film", 0, 100), "sharpen": ("Sharpen", -100, 100),
                  "vibrance": ("Vibrance", -100, 100)}
# looks: Lumetri settings, plus Color Balance split toning (red, green, blue pushes, -100 to 100)
LOOKS = {
    "teal_orange": {"contrast": 15, "saturation": 115, "vibrance": 10,
                    "shadow_balance": [-20, 0, 25], "highlight_balance": [20, 5, -20]},
    "bleach_bypass": {"contrast": 45, "saturation": 45, "highlights": -10, "blacks": -10, "sharpen": 15},
    "noir": {"saturation": 0, "contrast": 50, "blacks": -20, "vignette": -1.5},
    "warm": {"temperature": 25, "tint": 5, "vibrance": 10},
    "cool": {"temperature": -25, "tint": -3},
    "vintage": {"faded_film": 40, "temperature": 15, "saturation": 80, "contrast": -10,
                "highlight_balance": [10, 5, -10]},
    "cyberpunk": {"tint": 25, "temperature": -20, "contrast": 25, "vibrance": 30,
                  "shadow_balance": [0, -10, 30], "highlight_balance": [30, -10, 20]},
    "matrix": {"tint": -30, "saturation": 70, "contrast": 20, "shadow_balance": [-10, 20, -10]},
    "golden_hour": {"temperature": 35, "exposure": 0.2, "highlights": -15, "vibrance": 20},
    "day_for_night": {"exposure": -1.5, "temperature": -40, "saturation": 45, "contrast": 15, "vignette": -2},
}


def _balance(name, v):
    if v is None:
        return None
    if len(v) != 3 or not all(-100 <= float(x) <= 100 for x in v):
        raise ToolError(f"{name} needs [red, green, blue] pushes, each -100 to 100")
    return [float(x) for x in v]


@_tool
def color_grade(look: str | None = None, layer: int | str | None = None, exposure: float | None = None,
                contrast: float | None = None, highlights: float | None = None, shadows: float | None = None,
                whites: float | None = None, blacks: float | None = None, temperature: float | None = None,
                tint: float | None = None, saturation: float | None = None, vibrance: float | None = None,
                faded_film: float | None = None, sharpen: float | None = None, vignette: float | None = None,
                shadow_balance: list[float] | None = None, midtone_balance: list[float] | None = None,
                highlight_balance: list[float] | None = None, name: str = "Grade",
                comp: int | str | None = None) -> dict:
    """Grade with Lumetri Color: on a new adjustment layer named `name` over the whole comp (default), or on
    `layer`. Start from a look (teal_orange, bleach_bypass, noir, warm, cool, vintage, cyberpunk, matrix,
    golden_hour, day_for_night) and/or set: exposure (-5 to 5 stops), contrast, highlights, shadows, whites, blacks,
    temperature, tint (-100 to 100), saturation (0-200, 100 = unchanged), vibrance, faded_film (0-100), sharpen,
    vignette (-3 to 3, negative darkens the edges). Split toning: shadow/midtone/highlight_balance [r, g, b] pushes
    (-100 to 100) with Color Balance. Given values override the look's."""
    if look is not None and look not in LOOKS:
        raise ToolError(f"look must be one of: {', '.join(LOOKS)}")
    given = {k: v for k, v in dict(exposure=exposure, contrast=contrast, highlights=highlights, shadows=shadows,
                                   whites=whites, blacks=blacks, temperature=temperature, tint=tint,
                                   saturation=saturation, vibrance=vibrance, faded_film=faded_film,
                                   sharpen=sharpen, vignette=vignette, shadow_balance=shadow_balance,
                                   midtone_balance=midtone_balance, highlight_balance=highlight_balance).items()
             if v is not None}
    s = {**LOOKS.get(look, {}), **given}
    if not s:
        raise ToolError("nothing to grade: pass a look or some settings")
    basic, creative = {}, {}
    for table, out in ((GRADE_BASIC, basic), (GRADE_CREATIVE, creative)):
        for key, (param, lo, hi) in table.items():
            if key in s:
                if not lo <= s[key] <= hi:
                    raise ToolError(f"{key} must be {lo} to {hi}")
                out[param] = s[key]
    if "vignette" in s and not -3 <= s["vignette"] <= 3:
        raise ToolError("vignette must be -3 to 3")
    balance = {k: _balance(f"{k[:-1]}_balance", s.get(f"{k[:-1]}_balance"))
               for k in ("shadows", "midtones", "highlights")}
    balance = {k: v for k, v in balance.items() if v is not None}
    return _call("color_grade", comp=comp, layer=layer, name=name, basic=basic, creative=creative,
                 vignette=s.get("vignette"), balance=balance or None)


@_tool
def beat_cut(layers: list[int | str], beats: list[float] | None = None, bpm: float | None = None, every: int = 1,
             start: float | None = None, end: float | None = None, skip: float = 0.0,
             comp: int | str | None = None) -> dict:
    """Cut clips to the music: from `start` to `end` (default the first beat to the comp's end), a cut on every
    `every`-th beat, cycling through `layers` (each cut a copy named "<layer> cut <n>"). Beats come from `beats`
    (seconds), a `bpm`, or else the comp's markers (see markers_from_audio). Each clip keeps playing where its last
    cut left off, plus `skip` seconds, and loops back when its footage runs out. The original layers are turned
    off."""
    if not layers:
        raise ToolError("no layers to cut")
    if bpm is not None and not 20 <= bpm <= 400:
        raise ToolError("bpm must be 20-400")
    if beats is not None and (not beats or beats != sorted(beats) or beats[0] < 0):
        raise ToolError("beats must be ascending times in seconds, 0 or more")
    if beats is not None and bpm is not None:
        raise ToolError("pass beats or bpm, not both")
    if every < 1 or skip < 0:
        raise ToolError("every must be 1 or more and skip 0 or more")
    if start is not None and end is not None and end <= start:
        raise ToolError("end must be after start")
    return _call("beat_cut", comp=comp, layers=layers, beats=beats, bpm=bpm, every=every, start=start, end=end,
                 skip=skip)


TRANSITIONS = ("crossfade", "dip_to_black", "dip_to_white", "push_left", "push_right", "push_up", "push_down",
               "whip_left", "whip_right", "whip_up", "whip_down", "zoom", "spin")


@_tool
def transition(from_layer: int | str, to_layer: int | str, kind: str = "crossfade", duration: float = 0.5,
               comp: int | str | None = None) -> dict:
    """A transition from one layer to the next: to_layer is moved in time to overlap the end of from_layer by
    `duration` seconds (dips: to start where it ends) and above it. kind: crossfade, dip_to_black, dip_to_white
    (a solid fades up and down over the cut), push_<left|right|up|down> (both slide by a frame), whip_<dir> (a fast
    push with motion blur), zoom (out-zoom and fade into an in-zoom), spin. Refuses to overwrite properties that
    already have keyframes."""
    if kind not in TRANSITIONS:
        raise ToolError(f"kind must be one of: {', '.join(TRANSITIONS)}")
    if not 0 < duration <= 10:
        raise ToolError("duration must be above 0 and at most 10 seconds")
    return _call("transition", comp=comp, kind=kind, duration=duration, **{"from": from_layer, "to": to_layer})


@_tool
def speed_ramp(layer: int | str, points: list[dict], ramp: float = 0.3, comp: int | str | None = None) -> dict:
    """Speed ramps with time remapping: points [{"time": comp seconds, "speed": percent}] change the speed from that
    time on (100 = normal, 50 = half, 0 = freeze, negative = backwards; the layer plays at 100 % until the first
    point). Each change eases over `ramp` seconds (0 = instant). Fails, naming the time, when the footage would run
    out."""
    if not points:
        raise ToolError("no points")
    for pt in points:
        if not isinstance(pt, dict) or "time" not in pt or "speed" not in pt:
            raise ToolError(f"each point needs time and speed: {pt!r}")
        if not -1000 <= pt["speed"] <= 1000:
            raise ToolError("speed must be -1000 to 1000 percent")
    times = [pt["time"] for pt in points]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ToolError("point times must increase")
    if not 0 <= ramp <= 5:
        raise ToolError("ramp must be 0-5 seconds")
    return _call("speed_ramp", comp=comp, layer=layer, ramp=ramp,
                 points=[{"time": pt["time"], "speed": pt["speed"]} for pt in points])


REVEALS = ("wipe_right", "wipe_left", "wipe_down", "wipe_up", "split_vertical", "split_horizontal", "box", "iris")


@_tool
def reveal_mask(layer: int | str, style: str = "wipe_right", duration: float = 1.0, start: float | None = None,
                feather: float = 0.0, reverse: bool = False, ease: bool = True, comp: int | str | None = None) -> dict:
    """Reveal a layer with an animated mask: wipe_right (the edge travels left to right), wipe_left, wipe_down,
    wipe_up, split_vertical (opens sideways from the center line), split_horizontal, box (grows from the center),
    iris (a circle). Over `duration` seconds from `start` (default the layer's in point); feather softens the edge
    (px); reverse hides instead. Over existing masks it intersects them."""
    if style not in REVEALS:
        raise ToolError(f"style must be one of: {', '.join(REVEALS)}")
    if duration <= 0 or feather < 0:
        raise ToolError("duration must be above 0 and feather 0 or more")
    return _call("reveal_mask", comp=comp, layer=layer, style=style, duration=duration, start=start,
                 feather=feather, reverse=reverse or None, ease=ease)


@_tool
def set_mask(layer: int | str, mask: int | str, mode: str | None = None, feather: float | None = None,
             expansion: float | None = None, opacity: float | None = None, inverted: bool | None = None,
             name: str | None = None, comp: int | str | None = None) -> dict:
    """Change a mask (by index or name): mode (none, add, subtract, intersect, lighten, darken, difference),
    feather and expansion (px), opacity (0-100), inverted, name."""
    if mode is not None and mode not in MASK_MODES:
        raise ToolError(f"mode must be one of: {', '.join(MASK_MODES)}")
    if feather is not None and feather < 0:
        raise ToolError("feather must be 0 or more")
    if opacity is not None and not 0 <= opacity <= 100:
        raise ToolError("opacity must be 0-100")
    return _call("set_mask", comp=comp, layer=layer, mask=mask, mode=mode, feather=feather, expansion=expansion,
                 opacity=opacity, inverted=inverted, name=name)


@_tool
def animate_mask(layer: int | str, mask: int | str, keys: list[dict], ease: bool = True,
                 comp: int | str | None = None) -> dict:
    """Keyframe a mask by hand (rotoscoping, moving holes and shapes): keys [{"time": s, and any of "rect" or
    "ellipse" [x, y, width, height] or "points" [[x, y], ...] (layer pixels), "feather", "expansion" (px),
    "opacity"}]. Shapes with the same number of points morph smoothly."""
    if not keys:
        raise ToolError("no keys")
    for k in keys:
        if not isinstance(k, dict) or "time" not in k:
            raise ToolError(f"each key needs a time: {k!r}")
        shapes = [s for s in ("rect", "ellipse", "points") if s in k]
        if len(shapes) > 1:
            raise ToolError("a key takes one shape: rect, ellipse or points")
        if not shapes and not any(p in k for p in ("feather", "expansion", "opacity")):
            raise ToolError(f"key at {k['time']} s sets nothing")
        for s in ("rect", "ellipse"):
            if s in k:
                _rect(s, k[s])
        if "points" in k and (len(k["points"]) < 3 or any(len(pt) != 2 for pt in k["points"])):
            raise ToolError("points needs at least 3 [x, y]")
        if k.get("feather", 0) < 0 or not 0 <= k.get("opacity", 0) <= 100:
            raise ToolError("feather must be 0 or more and opacity 0-100")
    return _call("animate_mask", comp=comp, layer=layer, mask=mask, keys=keys, ease=ease)


SCREENS = {"green": [0.2, 0.75, 0.3], "blue": [0.1, 0.25, 0.75]}


@_tool
def key_out(layer: int | str, screen: str | list[float] = "green", screen_gain: float | None = None,
            clip_black: float | None = None, clip_white: float | None = None, shrink: float | None = None,
            softness: float | None = None, clean: bool = True, edge_radius: float | None = None,
            despill: bool = True, choke: float | None = None, comp: int | str | None = None) -> dict:
    """Key a green or blue screen with Keylight: screen green, blue, or the screen's exact color (hex or [r, g, b]
    0-1, best sampled from the footage). screen_gain (default 100), clip_black/clip_white (0-100) crush the matte,
    shrink (negative shrinks the matte, px), softness. clean adds Key Cleaner (edge_radius), despill the Advanced
    Spill Suppressor, choke a Simple Choker."""
    color = SCREENS.get(screen) if isinstance(screen, str) and screen in SCREENS else _rgb("screen", screen)
    for nm, v, lo, hi in (("clip_black", clip_black, 0, 100), ("clip_white", clip_white, 0, 100),
                          ("screen_gain", screen_gain, 0, 200), ("choke", choke, -10, 10)):
        if v is not None and not lo <= v <= hi:
            raise ToolError(f"{nm} must be {lo} to {hi}")
    if clip_black is not None and clip_white is not None and clip_black >= clip_white:
        raise ToolError("clip_black must be below clip_white")
    return _call("key_out", comp=comp, layer=layer, color=color, screenGain=screen_gain, clipBlack=clip_black,
                 clipWhite=clip_white, shrink=shrink, softness=softness, clean=clean or None, edgeRadius=edge_radius,
                 despill=despill or None, choke=choke or None)


@_tool
def camera_shake(style: str = "handheld", amount: float = 10, frequency: float = 6, rotation: float = 0.5,
                 at: float = 0.0, decay: float = 4, motion_blur: bool = True, layer: int | str | None = None,
                 name: str = "Shake", comp: int | str | None = None) -> dict:
    """Shake the shot with a Transform effect driven by wiggle: style handheld (steady drift) or impact (a hit at
    `at` seconds that dies away at `decay` per second). amount in px, rotation in degrees, frequency per second. On
    a new adjustment layer over the whole comp (scaled up so no edges show), or on `layer`."""
    if style not in ("handheld", "impact"):
        raise ToolError("style must be handheld or impact")
    if not 0 < amount <= 500 or not 0 < frequency <= 60 or not 0 <= rotation <= 45 or decay <= 0 or at < 0:
        raise ToolError("need 0 < amount <= 500, 0 < frequency <= 60, rotation 0-45, decay > 0, at >= 0")
    return _call("camera_shake", comp=comp, layer=layer, name=name, style=style, amount=amount, frequency=frequency,
                 rotation=rotation, at=at, decay=decay, motionBlur=motion_blur or None)


@_tool
def glitch(start: float = 0.0, duration: float = 1.0, intensity: float = 1.0, frequency: float = 12,
           density: float = 0.5, layer: int | str | None = None, name: str = "Glitch",
           comp: int | str | None = None) -> dict:
    """Digital glitch hits: blocky displacement and horizontal jumps, random per frame chunk (`frequency` chunks
    per second, `density` 0-1 of them hit), from `start` for `duration` seconds at `intensity` (0.1-5). On a new
    adjustment layer spanning that time, or on `layer`."""
    if start < 0 or duration <= 0:
        raise ToolError("start must be 0 or more and duration above 0")
    if not 0.1 <= intensity <= 5 or not 1 <= frequency <= 60 or not 0 < density <= 1:
        raise ToolError("need intensity 0.1-5, frequency 1-60, density above 0 up to 1")
    return _call("glitch", comp=comp, layer=layer, name=name, start=start, duration=duration, intensity=intensity,
                 frequency=frequency, density=density)


@_tool
def weather(kind: str = "snow", amount: float | None = None, size: float | None = None, speed: float | None = None,
            wind: float | None = None, opacity: float | None = None, name: str | None = None,
            comp: int | str | None = None) -> dict:
    """Snow or rain over the comp: CC Snowfall or CC Rainfall on a new adjustment layer. amount is flakes or drops,
    size, speed, wind and opacity are the effect's own units; left out, its defaults stay."""
    if kind not in ("snow", "rain"):
        raise ToolError("kind must be snow or rain")
    if amount is not None and amount < 0 or size is not None and size <= 0 or opacity is not None and not \
            0 <= opacity <= 100:
        raise ToolError("amount must be 0 or more, size above 0, opacity 0-100")
    return _call("weather", comp=comp, kind=kind, name=name or kind.capitalize(), amount=amount, size=size,
                 speed=speed, wind=wind, opacity=opacity)


@_tool
def letterbox(aspect: float = 2.39, color: list[float] = [0, 0, 0], animate: float = 0.0, start: float = 0.0,
              name: str = "Letterbox", comp: int | str | None = None) -> dict:
    """Cinema bars: a solid on top with a mask cut to the picture area for `aspect` (e.g. 2.39, 2.0, 1.85),
    sliding in over `animate` seconds from `start` when above 0."""
    if not 1 <= aspect <= 4:
        raise ToolError("aspect must be 1-4 (width / height)")
    if animate < 0 or start < 0:
        raise ToolError("animate and start must be 0 or more")
    return _call("letterbox", comp=comp, aspect=aspect, color=_color("color", color), animate=animate, start=start,
                 name=name)


# --- explainers, privacy, layouts, text, audio ---


def _point(name, v):
    if v is None:
        return None
    if len(v) != 2:
        raise ToolError(f"{name} needs [x, y]")
    return [float(v[0]), float(v[1])]


@_tool
def draw_arrow(from_point: list[float], to_point: list[float], color: list[float] = [1, 1, 1], width: float = 8,
               head: float = 30, curve: float = 0.0, duration: float = 0.8, start: float = 0.0, name: str = "Arrow",
               comp: int | str | None = None) -> dict:
    """An arrow from from_point to to_point (comp pixels) on a new shape layer: a stroke `width` px wide with round
    ends, a head of `head` px, bent sideways by `curve` px (negative bends the other way). It draws itself on over
    `duration` seconds from `start` (0: shown at once), the head appearing when the line arrives."""
    if width <= 0 or head < 0 or duration < 0 or start < 0:
        raise ToolError("width must be above 0; head, duration and start 0 or more")
    return _call("draw_arrow", comp=comp, name=name, **{"from": _point("from_point", from_point),
                                                       "to": _point("to_point", to_point)},
                 color=_color("color", color), width=width, head=head, curve=curve, duration=duration, start=start)


@_tool
def spotlight(rect: list[float], shape: str = "ellipse", darkness: float = 70, feather: float = 40, radius: float = 0,
              fade: float = 0.5, start: float = 0.0, name: str = "Spotlight", comp: int | str | None = None) -> dict:
    """Darken everything but one area to point at it: a black solid at `darkness` % opacity with a hole (ellipse,
    or rect with corner `radius`) at rect [x, y, width, height] in comp pixels, feathered. It fades in over `fade`
    seconds from `start`. Move the hole later with animate_mask on the "Light" mask."""
    _rect("rect", rect)
    if shape not in ("ellipse", "rect"):
        raise ToolError("shape must be ellipse or rect")
    if not 0 < darkness <= 100 or feather < 0 or radius < 0 or fade < 0 or start < 0:
        raise ToolError("darkness must be 1-100; feather, radius, fade and start 0 or more")
    return _call("spotlight", comp=comp, name=name, rect=[float(v) for v in rect], shape=shape, darkness=darkness,
                 feather=feather, radius=radius, fade=fade, start=start)


@_tool
def blur_region(rect: list[float], kind: str = "blur", amount: float = 30, shape: str = "rect", feather: float = 10,
                start: float = 0.0, end: float | None = None, name: str = "Blur", comp: int | str | None = None) -> dict:
    """Hide part of the picture (faces, plates, screens): an adjustment layer masked to rect [x, y, width, height]
    in comp pixels (or an ellipse in it) with Gaussian Blur (amount = blurriness) or Mosaic (amount = block size in
    px), from `start` to `end` seconds. To follow a moving subject, animate its "Region" mask with animate_mask or
    parent it to a tracked null."""
    _rect("rect", rect)
    if kind not in ("blur", "mosaic"):
        raise ToolError("kind must be blur or mosaic")
    if shape not in ("rect", "ellipse"):
        raise ToolError("shape must be rect or ellipse")
    if amount <= 0 or feather < 0 or start < 0 or (end is not None and end <= start):
        raise ToolError("amount must be above 0, feather and start 0 or more, end after start")
    return _call("blur_region", comp=comp, name=name, rect=[float(v) for v in rect], kind=kind, amount=amount,
                 shape=shape, feather=feather, start=start, end=end)


CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")


@_tool
def picture_in_picture(layer: int | str, corner: str = "top_right", size: float = 0.3, margin: float = 40,
                       radius: float = 16, border: float = 0, border_color: list[float] = [1, 1, 1],
                       comp: int | str | None = None) -> dict:
    """Make a layer a picture-in-picture: scaled to `size` of the comp's width, placed in a corner with `margin`
    px, corners rounded by `radius` px (a mask), and an optional `border` px frame in border_color behind it."""
    if corner not in CORNERS:
        raise ToolError(f"corner must be one of: {', '.join(CORNERS)}")
    if not 0.05 <= size <= 1 or margin < 0 or radius < 0 or border < 0:
        raise ToolError("size must be 0.05-1; margin, radius and border 0 or more")
    return _call("picture_in_picture", comp=comp, layer=layer, corner=corner, size=size, margin=margin, radius=radius,
                 border=border, borderColor=_color("border_color", border_color))


@_tool
def split_screen(layers: list[int | str], layout: str = "columns", gap: float = 0,
                 comp: int | str | None = None) -> dict:
    """Split the frame between layers: columns (side by side), rows (stacked) or grid. Each layer is scaled to fill
    its pane, centered, and cropped to it with a mask; `gap` px between panes shows what is below."""
    if layout not in ("columns", "rows", "grid"):
        raise ToolError("layout must be columns, rows or grid")
    if not 2 <= len(layers) <= 16:
        raise ToolError("split_screen takes 2-16 layers")
    if gap < 0:
        raise ToolError("gap must be 0 or more")
    return _call("split_screen", comp=comp, layers=layers, layout=layout, gap=gap)


@_tool
def split_text(layer: int | str, by: str = "words", comp: int | str | None = None) -> dict:
    """Split a text layer into one layer per word, character or line, each placed where it sat in the original
    (measured with its own font), so they can be animated one by one (stagger_layers, animate_text). The original
    is turned off. Words and characters need one line of text."""
    if by not in ("words", "characters", "lines"):
        raise ToolError("by must be words, characters or lines")
    return _call("split_text", comp=comp, layer=layer, by=by)


TIMER_FORMATS = ("mm:ss", "m:ss", "hh:mm:ss", "ss.cs", "mm:ss.cs")


@_tool
def timer(from_seconds: float = 0, to_seconds: float = 60, duration: float | None = None, start: float = 0.0,
          format: str = "mm:ss", layer: int | str | None = None, name: str | None = None,
          comp: int | str | None = None) -> dict:
    """A clock: counts from from_seconds to to_seconds (down when to is lower) over `duration` seconds (default
    real time) from `start`, shown as mm:ss, m:ss, hh:mm:ss, ss.cs or mm:ss.cs (cs: hundredths) on a new text layer
    or `layer`. Driven by a Slider Control named Timer; style it with set_text."""
    if format not in TIMER_FORMATS:
        raise ToolError(f"format must be one of: {', '.join(TIMER_FORMATS)}")
    if from_seconds < 0 or to_seconds < 0 or from_seconds == to_seconds:
        raise ToolError("from_seconds and to_seconds must differ and be 0 or more")
    duration = abs(to_seconds - from_seconds) if duration is None else duration
    if duration <= 0 or start < 0:
        raise ToolError("duration must be above 0 and start 0 or more")
    return _call("timer", comp=comp, layer=layer, name=name, **{"from": from_seconds, "to": to_seconds},
                 duration=duration, start=start, format=format)


@_tool
def audio_visualizer(audio: int | str, kind: str = "spectrum", from_point: list[float] | None = None,
                     to_point: list[float] | None = None, color: list[float] = [1, 1, 1], height: float = 300,
                     detail: int = 64, thickness: float = 6, name: str | None = None,
                     comp: int | str | None = None) -> dict:
    """Bars or a wave that move with a layer's audio: kind spectrum (Audio Spectrum: `detail` frequency bands) or
    waveform (Audio Waveform: `detail` samples), drawn from from_point to to_point (comp pixels, default across the
    middle), up to `height` px, `thickness` px, in `color`, on a new layer above the audio."""
    if kind not in ("spectrum", "waveform"):
        raise ToolError("kind must be spectrum or waveform")
    if height <= 0 or thickness <= 0 or not 1 <= detail <= 4096:
        raise ToolError("height and thickness must be above 0, detail 1-4096")
    return _call("audio_visualizer", comp=comp, audio=audio, kind=kind, name=name or kind.capitalize(),
                 **{"from": _point("from_point", from_point), "to": _point("to_point", to_point)},
                 color=_color("color", color), height=height, detail=detail, thickness=thickness)


@_tool
def text_box(layer: int | str, padding: list[float] = [30, 16], color: list[float] = [0, 0, 0], opacity: float = 80,
             radius: float = 8, name: str | None = None, comp: int | str | None = None) -> dict:
    """A background box behind a text layer that follows it: sized and placed by expressions from the text's
    sourceRectAtTime, so it fits when the text, its size or its position changes. padding [x, y] px, corner
    radius px, color and opacity. For captions, lower thirds and labels."""
    if len(padding) != 2 or min(padding) < 0:
        raise ToolError("padding needs [x, y], 0 or more")
    if not 0 <= opacity <= 100 or radius < 0:
        raise ToolError("opacity must be 0-100 and radius 0 or more")
    return _call("text_box", comp=comp, layer=layer, name=name, padding=[float(v) for v in padding],
                 color=_color("color", color), opacity=opacity, radius=radius)


# --- motion design: entrances, lower thirds, photos, formats, screens, neon ---

ANIM_STYLES = ("fade", "slide_left", "slide_right", "slide_up", "slide_down", "scale", "pop", "blur", "rotate")


@_tool
def animate_layer(layer: int | str, style: str = "fade", direction: str = "in", duration: float = 0.6,
                  start: float | None = None, distance: float = 200, ease: bool = True,
                  comp: int | str | None = None) -> dict:
    """Animate any layer in or out: fade, slide_<left|right|up|down> (travels that way by `distance` px, with a
    fade), scale, pop (scale with an overshoot), blur (a Gaussian Blur clears, with a fade) or rotate (a quarter
    turn, with a fade). In starts at the layer's in point, out ends at its out point, unless `start` is given.
    An in and an out on the same layer combine; keys already inside the span are refused."""
    if style not in ANIM_STYLES:
        raise ToolError(f"style must be one of: {', '.join(ANIM_STYLES)}")
    if direction not in ("in", "out"):
        raise ToolError("direction must be in or out")
    if duration <= 0 or distance <= 0 or (start is not None and start < 0):
        raise ToolError("duration and distance must be above 0, start 0 or more")
    return _call("animate_layer", comp=comp, layer=layer, style=style, direction=direction, duration=duration,
                 start=start, distance=distance, ease=ease)


@_tool
def lower_third(title: str, subtitle: str | None = None, start: float = 0.0, duration: float = 5.0,
                side: str = "left", accent: list[float] = [1, 0.75, 0.1], text_color: list[float] = [1, 1, 1],
                subtitle_color: list[float] = [0.85, 0.85, 0.85], title_size: float = 64, subtitle_size: float = 36,
                font: str | None = None, bar_width: float = 8, margin: float = 120, name: str = "Lower third",
                comp: int | str | None = None) -> dict:
    """A name strap: title and optional subtitle text next to an accent bar, in the bottom left (or right) corner
    `margin` px from the edges, from `start` for `duration` seconds. The bar grows in, the text slides out from
    behind it, and everything leaves at the end. font is a PostScript name (see list_fonts)."""
    if not title.strip():
        raise ToolError("title is empty")
    if side not in ("left", "right"):
        raise ToolError("side must be left or right")
    if duration < 1.5 or start < 0:
        raise ToolError("duration must be at least 1.5 seconds (in and out) and start 0 or more")
    if title_size <= 0 or subtitle_size <= 0 or bar_width <= 0 or margin < 0:
        raise ToolError("sizes and bar_width must be above 0, margin 0 or more")
    return _call("lower_third", comp=comp, title=title, subtitle=subtitle or None, start=start, duration=duration,
                 side=side, accent=_color("accent", accent), textColor=_color("text_color", text_color),
                 subtitleColor=_color("subtitle_color", subtitle_color), titleSize=title_size,
                 subtitleSize=subtitle_size, font=font, barWidth=bar_width, margin=margin, name=name)


PANS = ("none", "left", "right", "up", "down")


def _zoom(zoom):
    if len(zoom) != 2 or min(zoom) < 100 or max(zoom) > 400:
        raise ToolError("zoom needs [start, end] percent of the frame-filling size, each 100-400")
    return [float(z) for z in zoom]


@_tool
def ken_burns(layer: int | str, zoom: list[float] = [100, 115], pan: str = "none", amount: float = 0,
              start: float | None = None, end: float | None = None, ease: bool = False,
              comp: int | str | None = None) -> dict:
    """A slow push on a still or a shot: scale from zoom[0] to zoom[1] percent of the size that fills the frame,
    while the picture moves `pan` (left, right, up, down) by `amount` px, from `start` to `end` (default the
    layer's in and out points). Refuses a pan that would show the picture's edges, and says how much room there is."""
    if pan not in PANS:
        raise ToolError(f"pan must be one of: {', '.join(PANS)}")
    if amount < 0 or (pan != "none" and amount == 0):
        raise ToolError("amount must be 0 or more, and above 0 with a pan")
    return _call("ken_burns", comp=comp, layer=layer, zoom=_zoom(zoom), pan=pan, amount=amount, start=start, end=end,
                 ease=ease)


@_tool
def slideshow(items: list[int | str], duration: float = 3.0, overlap: float = 0.5, ken_burns: bool = True,
              zoom: float = 115, pan: float = 80, width: int = 1920, height: int = 1080, fps: float = 30,
              name: str = "Slideshow") -> dict:
    """A new comp that shows `items` (project items, or image and video files, which are imported) one after
    another for `duration` seconds each, crossfading over `overlap` seconds, each filling the frame with a slow
    Ken Burns move: zooming in and out by turns (to `zoom` %) and panning up to `pan` px in turn."""
    if not items:
        raise ToolError("no items")
    if duration <= 0 or not 0 <= overlap < duration:
        raise ToolError("duration must be above 0 and overlap 0 or more, below duration")
    if not 100 <= zoom <= 200 or pan < 0:
        raise ToolError("zoom must be 100-200 and pan 0 or more")
    if not (16 <= width <= 8192 and 16 <= height <= 8192 and 1 <= fps <= 120):
        raise ToolError("width and height must be 16-8192, fps 1-120")
    refs = []
    for it in items:
        if isinstance(it, str) and os.path.isfile(_path(it)):
            refs.append(import_file(it)["id"])
        else:
            refs.append(it)
    return _call("slideshow", items=refs, duration=duration, overlap=overlap, kenBurns=ken_burns, zoom=zoom, pan=pan,
                 width=width, height=height, fps=fps, name=name)


@_tool
def progress_bar(start: float = 0.0, duration: float | None = None, position: str = "bottom", height: float = 8,
                 margin: float = 0, color: list[float] = [1, 1, 1], track_color: list[float] = [1, 1, 1],
                 track_opacity: float = 25, rounded: bool = True, name: str = "Progress",
                 comp: int | str | None = None) -> dict:
    """A bar that fills left to right from `start` over `duration` seconds (default to the comp's end), along the
    bottom or top edge, `height` px, `margin` px in from the edges, over a faint track (track_opacity 0 hides it)."""
    if position not in ("bottom", "top"):
        raise ToolError("position must be bottom or top")
    if height <= 0 or margin < 0 or start < 0 or (duration is not None and duration <= 0):
        raise ToolError("height and duration must be above 0, margin and start 0 or more")
    if not 0 <= track_opacity <= 100:
        raise ToolError("track_opacity must be 0-100")
    return _call("progress_bar", comp=comp, start=start, duration=duration, position=position, height=height,
                 margin=margin, color=_color("color", color), trackColor=_color("track_color", track_color),
                 trackOpacity=track_opacity, rounded=rounded or None, name=name)


@_tool
def gradient_background(colors: list[str | list[float]] = ["#1B2A49", "#5B2A86"], kind: str = "linear",
                        angle: float = 90, cycle: float = 0, name: str = "Background",
                        comp: int | str | None = None) -> dict:
    """A gradient background at the bottom of the comp (Gradient Ramp on a solid): two colors (hex or [r, g, b]),
    linear at `angle` degrees (90: top to bottom, 0: left to right) or radial from the center. cycle > 0 turns a
    linear gradient around once every `cycle` seconds."""
    if len(colors) != 2:
        raise ToolError("colors takes two colors")
    if kind not in ("linear", "radial"):
        raise ToolError("kind must be linear or radial")
    if cycle < 0 or (cycle and kind == "radial"):
        raise ToolError("cycle must be 0 or more, and only turns a linear gradient")
    return _call("gradient_background", comp=comp, colors=[_rgb("colors", c) for c in colors], kind=kind, angle=angle,
                 cycle=cycle, name=name)


FORMATS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080), "4:3": (1440, 1080)}


@_tool
def reframe(comp: int | str, aspect: str = "9:16", mode: str = "fill", focus: list[float] = [0.5, 0.5],
            blur: float = 60, name: str | None = None) -> dict:
    """Make another format of a comp for social video: a new comp at 9:16 (1080 x 1920), 1:1, 4:5, 16:9 or 4:3
    holding the comp, scaled to fill (cropping; focus [x, y] 0-1 is the point of the picture to keep in frame),
    fit (whole picture, bars) or blur_fill (whole picture over a blurred, filling copy of itself)."""
    if aspect not in FORMATS:
        raise ToolError(f"aspect must be one of: {', '.join(FORMATS)}")
    if mode not in ("fill", "fit", "blur_fill"):
        raise ToolError("mode must be fill, fit or blur_fill")
    if len(focus) != 2 or not all(0 <= f <= 1 for f in focus):
        raise ToolError("focus needs [x, y], each 0-1")
    if blur <= 0:
        raise ToolError("blur must be above 0")
    w, h = FORMATS[aspect]
    return _call("reframe", comp=comp, width=w, height=h, label=aspect.replace(":", "x"), mode=mode,
                 focus=[float(f) for f in focus], blur=blur, name=name)


def _corners(name, v):
    if len(v) != 4 or any(len(pt) != 2 for pt in v):
        raise ToolError(f"{name} needs four [x, y] points: top left, top right, bottom right, bottom left")
    return [[float(pt[0]), float(pt[1])] for pt in v]


@_tool
def corner_pin(layer: int | str, corners: list[list[float]] | None = None, keys: list[dict] | None = None,
               comp: int | str | None = None) -> dict:
    """Pin a layer's four corners onto a screen, sign or page in the shot (comp pixels, clockwise from the top
    left: [[tl], [tr], [br], [bl]]) with Corner Pin. For a moving shot, keys [{"time": s, "corners": [...]}]
    keyframe them (e.g. from tracked points). The layer must be unparented and unrotated."""
    if (corners is None) == (keys is None):
        raise ToolError("pass corners or keys")
    if corners is not None:
        rows = [{"corners": _corners("corners", corners)}]
    else:
        if not keys or any(not isinstance(k, dict) or "time" not in k or "corners" not in k for k in keys):
            raise ToolError("each key needs time and corners")
        rows = [{"time": float(k["time"]), "corners": _corners("corners", k["corners"])} for k in keys]
    return _call("corner_pin", comp=comp, layer=layer, keys=rows)


@_tool
def neon(layer: int | str, color: str | list[float] = "#FF2D95", intensity: float = 1.0, flicker: float = 0.0,
         comp: int | str | None = None) -> dict:
    """A neon sign look for text or shapes: a hot, nearly white core (Fill) inside two colored glows, a tight halo
    and a wide spill, at `intensity` (0.2-4). flicker (0-1) is the share of moments the tube dips."""
    if not 0.2 <= intensity <= 4 or not 0 <= flicker <= 1:
        raise ToolError("intensity must be 0.2-4 and flicker 0-1")
    return _call("neon", comp=comp, layer=layer, color=_rgb("color", color), intensity=intensity,
                 flicker=flicker or None)


# --- talking heads, sound, charts, film, physics, paths ---


def _runs(flags, min_len):
    """[start, end) hop ranges where flags is True for at least min_len hops."""
    out, start = [], None
    for i, f in enumerate(list(flags) + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            if i - start >= min_len:
                out.append((start, i))
            start = None
    return out


def _db(env):
    return [20 * math.log10(max(e, 1.0) / 32768) for e in env]


@_tool
def cut_silences(layer: int | str, threshold_db: float = -40, min_silence: float = 0.5, padding: float = 0.12,
                 comp: int | str | None = None) -> dict:
    """Jump cuts for talking heads and voice-overs: find the pauses in a layer's sound (quieter than threshold_db
    for at least min_silence seconds), cut them out and close the gaps. `padding` seconds of each pause stay on
    both sides so words are not clipped. The kept parts become copies "<layer> 1", "<layer> 2"... back to back from
    the layer's in point; the original is turned off. Reads WAV, or other files through ffmpeg."""
    if not -90 <= threshold_db <= 0:
        raise ToolError("threshold_db must be -90 to 0")
    if min_silence <= 0 or padding < 0 or 2 * padding >= min_silence:
        raise ToolError("min_silence must be above 0, and padding 0 or more and below half of min_silence")
    src = _call("audio_source", comp=comp, layer=layer)
    db = _db(_envelope(src["file"]))
    s0, s1 = src["inPoint"] - src["startTime"], src["outPoint"] - src["startTime"]  # the trim, in source seconds
    s1 = min(s1, len(db) / 100)  # the sound ends with the file
    lo, hi = max(0, int(math.ceil(s0 * 100))), min(len(db), int(s1 * 100))
    quiet = _runs([x < threshold_db for x in db[lo:hi]], int(round(min_silence * 100)))
    gaps = [((lo + a) / 100 + padding, (lo + b) / 100 - padding) for a, b in quiet]
    # a pause at either end of the trim only loses the side that touches speech
    gaps = [(s0 if a - padding <= s0 + 1e-9 else a, s1 if b + padding >= s1 - 1e-9 else b) for a, b in gaps]
    keep, t = [], s0
    for a, b in gaps:
        if a - t > 0.01:
            keep.append([round(t, 3), round(a, 3)])
        t = b
    if s1 - t > 0.01:
        keep.append([round(t, 3), round(s1, 3)])
    removed = round((s1 - s0) - sum(b - a for a, b in keep), 3)
    if not gaps:
        return {"layer": layer, "parts": [], "removed": 0, "message": "no pauses found: raise threshold_db or lower "
                "min_silence"}
    if not keep:
        raise ToolError("the whole layer is quieter than threshold_db: lower it")
    out = _call("cut_segments", comp=comp, layer=layer, segments=keep)
    return {**out, "removed": removed, "pauses": len(gaps)}


@_tool
def duck_audio(music: int | str, voice: int | str, amount_db: float = -12, threshold_db: float = -35,
               attack: float = 0.15, release: float = 0.4, hold: float = 0.5, comp: int | str | None = None) -> dict:
    """Lower the music under speech: wherever `voice` is louder than threshold_db, the music's Audio Levels dip by
    amount_db, easing down over `attack` seconds before the words and back up over `release` after them; pauses
    shorter than `hold` stay ducked. Reads WAV, or other files through ffmpeg. Existing fades are kept."""
    if not -60 <= amount_db < 0:
        raise ToolError("amount_db must be below 0, down to -60")
    if not -90 <= threshold_db <= 0:
        raise ToolError("threshold_db must be -90 to 0")
    if not (0 < attack <= 3 and 0 < release <= 5 and 0 <= hold <= 10):
        raise ToolError("attack and release must be above 0 (up to 3 and 5 s), hold 0-10 s")
    src = _call("audio_source", comp=comp, layer=voice)
    db = _db(_envelope(src["file"]))
    speech = [(src["startTime"] + a / 100, src["startTime"] + b / 100) for a, b in _runs([x >= threshold_db for x in
                                                                                          db], 1)]
    speech = [(max(a, src["inPoint"]), min(b, src["outPoint"])) for a, b in speech
              if b > src["inPoint"] and a < src["outPoint"]]
    merged = []
    for a, b in speech:
        # close pauses shorter than hold, and dips that would overlap their ramps
        if merged and a - merged[-1][1] < max(hold, attack + release):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    if not merged:
        raise ToolError("no speech louder than threshold_db in the voice: lower it")
    keys = []
    for a, b in merged:
        keys += [{"time": round(max(0.0, a - attack), 3), "db": 0}, {"time": round(a, 3), "db": amount_db},
                 {"time": round(b, 3), "db": amount_db}, {"time": round(b + release, 3), "db": 0}]
    out = _call("duck_audio", comp=comp, layer=music, keys=keys)
    return {**out, "ducks": [[round(a, 3), round(b, 3)] for a, b in merged]}


def _colors(name, colors):
    if not colors:
        raise ToolError(f"{name} needs at least one color")
    return [_rgb(name, c) for c in colors]


PALETTE = ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#EDC948", "#B07AA1", "#FF9DA7"]


@_tool
def bar_chart(values: list[float], labels: list[str] | None = None, origin: list[float] = [360, 860],
              height: float = 500, bar_width: float = 80, gap: float = 40, max_value: float | None = None,
              colors: list[str | list[float]] | None = None, radius: float = 6, show_values: bool = True,
              value_format: str = "{:,.0f}", prefix: str = "", suffix: str = "", font_size: float = 32,
              text_color: list[float] = [1, 1, 1], start: float = 0.0, duration: float = 0.8, stagger: float = 0.12,
              name: str = "Bar chart", comp: int | str | None = None) -> dict:
    """An animated bar chart: bars standing on a baseline that starts at origin [x, y] (comp pixels, the first
    bar's left edge), max_value (default the largest) `height` px tall, growing up one after another (`stagger` s
    apart) from `start`. labels go under the bars; show_values puts each value above its bar, formatted with
    value_format, prefix and suffix (e.g. "$", "%")."""
    if not values or any(v < 0 for v in values) or len(values) > 60:
        raise ToolError("values: 1-60 numbers, 0 or more")
    top = max_value if max_value is not None else max(values)
    if top <= 0 or max(values) > top:
        raise ToolError("max_value must be above 0 and at least the largest value")
    if labels is not None and len(labels) != len(values):
        raise ToolError("labels needs one label per value")
    if height <= 0 or bar_width <= 0 or gap < 0 or duration <= 0 or stagger < 0 or start < 0:
        raise ToolError("height, bar_width and duration must be above 0; gap, stagger and start 0 or more")
    try:
        texts = [prefix + value_format.format(v) + suffix for v in values] if show_values else None
    except (ValueError, IndexError, KeyError) as e:
        raise ToolError(f"bad value_format: {e}") from None
    return _call("bar_chart", comp=comp, values=[float(v) for v in values], labels=labels, valueTexts=texts,
                 origin=_point("origin", origin), height=height, barWidth=bar_width, gap=gap, max=float(top),
                 colors=_colors("colors", colors or PALETTE), radius=radius, fontSize=font_size,
                 textColor=_color("text_color", text_color), start=start, duration=duration, stagger=stagger,
                 name=name)


@_tool
def donut_chart(values: list[float], labels: list[str] | None = None, center: list[float] = [960, 540],
                radius: float = 260, thickness: float = 90, colors: list[str | list[float]] | None = None,
                show_percent: bool = True, font_size: float = 30, text_color: list[float] = [1, 1, 1],
                start: float = 0.0, duration: float = 1.5, name: str = "Donut chart",
                comp: int | str | None = None) -> dict:
    """An animated donut (or pie) chart: slices sized by `values`, sweeping on clockwise from the top over
    `duration` seconds. thickness is the ring's width in px (thickness = radius makes a pie). Labels sit outside
    their slices, with the percentage when show_percent."""
    if not values or any(v < 0 for v in values) or sum(values) <= 0 or len(values) > 24:
        raise ToolError("values: 1-24 numbers, 0 or more, not all 0")
    if labels is not None and len(labels) != len(values):
        raise ToolError("labels needs one label per value")
    if radius <= 0 or not 0 < thickness <= radius or duration <= 0 or start < 0:
        raise ToolError("need radius > 0, 0 < thickness <= radius, duration > 0, start >= 0")
    total = sum(values)
    texts = None
    if labels is not None or show_percent:
        texts = []
        for i, v in enumerate(values):
            pct = f"{round(100 * v / total)}%" if show_percent else ""
            texts.append(" ".join(t for t in ((labels[i] if labels else ""), pct) if t))
    return _call("donut_chart", comp=comp, values=[float(v) for v in values], labels=texts,
                 center=_point("center", center), radius=radius, thickness=thickness,
                 colors=_colors("colors", colors or PALETTE), fontSize=font_size,
                 textColor=_color("text_color", text_color), start=start, duration=duration, name=name)


@_tool
def line_chart(values: list[float], box: list[float] = [260, 240, 1400, 600], min_value: float | None = None,
               max_value: float | None = None, color: str | list[float] = "#4E79A7", width: float = 6,
               smooth: bool = True, dots: bool = True, start: float = 0.0, duration: float = 2.0,
               name: str = "Line chart", comp: int | str | None = None) -> dict:
    """An animated line chart drawing itself on from left to right inside box [x, y, width, height] (comp pixels),
    values spread evenly across it between min_value and max_value (default their range); smooth curves it, dots
    pop on as the line reaches each point."""
    if len(values) < 2 or len(values) > 200:
        raise ToolError("values: 2-200 numbers")
    _rect("box", box)
    lo = min(values) if min_value is None else min_value
    hi = max(values) if max_value is None else max_value
    if hi <= lo:
        hi = lo + 1  # a flat line sits on the bottom
    if min(values) < lo or max(values) > hi:
        raise ToolError("min_value and max_value must include every value")
    if width <= 0 or duration <= 0 or start < 0:
        raise ToolError("width and duration must be above 0, start 0 or more")
    return _call("line_chart", comp=comp, values=[float(v) for v in values], box=[float(v) for v in box],
                 lo=float(lo), hi=float(hi), color=_rgb("color", color), width=width, smooth=smooth,
                 dots=dots or None, start=start, duration=duration, name=name)


@_tool
def film_look(grain: float = 4, weave: float = 1.0, flicker: float = 0.03, halation: float = 0.4,
              layer: int | str | None = None, name: str = "Film", comp: int | str | None = None) -> dict:
    """Make digital footage feel like film: grain (Noise amount, %), gate weave (px of drift), exposure flicker
    (stops) and halation (a red-orange glow around highlights, intensity); 0 leaves one out. On a new adjustment
    layer over the whole comp, or on `layer`. Pair it with color_grade("vintage") or a look of your own."""
    if not (0 <= grain <= 30 and 0 <= weave <= 10 and 0 <= flicker <= 0.5 and 0 <= halation <= 3):
        raise ToolError("need grain 0-30, weave 0-10, flicker 0-0.5, halation 0-3")
    if not (grain or weave or flicker or halation):
        raise ToolError("everything is 0: nothing to add")
    return _call("film_look", comp=comp, layer=layer, name=name, grain=grain, weave=weave, flicker=flicker,
                 halation=halation)


@_tool
def bounce_drop(layer: int | str, height: float = 400, bounces: int = 3, elasticity: float = 0.45,
                fall: float = 0.5, start: float | None = None, comp: int | str | None = None) -> dict:
    """Drop a layer into its place with a bounce: it falls `height` px over `fall` seconds onto its current
    position, then bounces `bounces` times, each `elasticity` (0-1) as high as the last, faster as they shrink."""
    if height <= 0 or fall <= 0 or not 0 <= bounces <= 10 or not 0 < elasticity < 1:
        raise ToolError("need height > 0, fall > 0, bounces 0-10, 0 < elasticity < 1")
    return _call("bounce_drop", comp=comp, layer=layer, height=height, bounces=bounces, elasticity=elasticity,
                 fall=fall, start=start)


@_tool
def draw_path(points: list[list[float]], closed: bool = False, smooth: bool = False,
              stroke: list[float] | None = [1, 1, 1], width: float = 6, fill: list[float] | None = None,
              duration: float = 0.0, start: float = 0.0, name: str = "Path", comp: int | str | None = None) -> dict:
    """A custom vector shape through points [[x, y], ...] (comp pixels): open or closed, straight or smooth (a curve
    through every point), stroked and/or filled. duration > 0 draws it on from `start`."""
    if len(points) < 2 or (closed and len(points) < 3):
        raise ToolError("a path needs at least 2 points (3 when closed)")
    pts = [_point("points", p) for p in points]
    if stroke is None and fill is None:
        raise ToolError("give it a stroke or a fill")
    if width <= 0 or duration < 0 or start < 0:
        raise ToolError("width must be above 0, duration and start 0 or more")
    return _call("draw_path", comp=comp, points=pts, closed=closed, smooth=smooth, stroke=_color("stroke", stroke),
                 width=width, fill=_color("fill", fill), duration=duration, start=start, name=name)


@_tool
def orbit(layer: int | str, center: list[float] = [960, 540], radius: float = 300, period: float = 4.0,
          start_angle: float = -90, clockwise: bool = True, ratio: float = 1.0, comp: int | str | None = None) -> dict:
    """Move a layer around a circle (or an ellipse: ratio is height / width) at center, once every `period` seconds,
    starting at start_angle degrees (-90 is the top, 0 the right). An expression drives the position."""
    if radius <= 0 or period <= 0 or ratio <= 0:
        raise ToolError("radius, period and ratio must be above 0")
    return _call("orbit", comp=comp, layer=layer, center=_point("center", center), radius=radius, period=period,
                 startAngle=start_angle, dir=1 if clockwise else -1, ratio=ratio)


@_tool
def run_jsx(code: str) -> Any:
    """Run ExtendScript in After Effects and return its value (numbers, strings, arrays; other objects as text).
    For anything the other tools do not cover; runs inside one undo group."""
    return _call("run_jsx", code=code)


if __name__ == "__main__":
    _setup_logging()
    log.info("starting", extra={"fields": {"platform": sys.platform, "pid": os.getpid()}})
    mcp.run()
