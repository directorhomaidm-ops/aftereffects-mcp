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
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
    "vector shapes, text animation presets, precomposing, layer order and switches, track mattes, markers, import, "
    "the render queue and frame export. Start with status and list_items, then comp_info to see a comp's layers. "
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
    # saveFrameToPng returns at once and fills the file later: wait for the PNG's IEND chunk.
    deadline = monotonic() + DEFAULT_TIMEOUT
    while monotonic() < deadline:
        try:
            if Path(out["path"]).read_bytes()[-8:-4] == b"IEND":
                return out
        except OSError:
            pass
        sleep(0.1)
    raise ToolError(f"After Effects did not finish writing {out['path']} within {DEFAULT_TIMEOUT}s")


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
             bg_color: list[float] | None = None, work_area: list[float] | None = None) -> dict:
    """Change a comp's name, size, duration, frame rate, background color, or work area [start, end] in seconds
    (what renders when the render settings use the work area)."""
    if width is not None and not 4 <= width <= 30000 or height is not None and not 4 <= height <= 30000:
        raise ToolError("width and height must be 4-30000 pixels")
    if duration is not None and not 0 < duration <= 10800:
        raise ToolError("duration must be above 0 and at most 3 hours")
    if frame_rate is not None and not 1 <= frame_rate <= 999:
        raise ToolError("frame_rate must be 1-999")
    if work_area is not None and len(work_area) != 2:
        raise ToolError("work_area needs [start, end]")
    return _call("set_comp", comp=comp, name=name, width=width, height=height, duration=duration,
                 frameRate=frame_rate, bgColor=_color("bg_color", bg_color), workArea=work_area)


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


@_tool
def run_jsx(code: str) -> Any:
    """Run ExtendScript in After Effects and return its value (numbers, strings, arrays; other objects as text).
    For anything the other tools do not cover; runs inside one undo group."""
    return _call("run_jsx", code=code)


if __name__ == "__main__":
    _setup_logging()
    log.info("starting", extra={"fields": {"platform": sys.platform, "pid": os.getpid()}})
    mcp.run()
