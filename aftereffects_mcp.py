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
(`osascript`) and its JSON result comes back on stdout, or from a result file when DoScript returns nothing (that
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
    "null, adjustment, camera, light, footage), properties, keyframes with easing, expressions, effects, import, "
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
        result = r.stdout.strip()
        if not result and os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                result = f.read().strip()
        return result
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
    return _call("export_frame", comp=comp, path=_path(path), time=time)


@_tool
def save_project(path: str | None = None) -> dict:
    """Save the project (to `path`, an .aep, when given or never saved)."""
    return _call("save_project", path=_path(path) if path else None)


@_tool
def run_jsx(code: str) -> Any:
    """Run ExtendScript in After Effects and return its value (numbers, strings, arrays; other objects as text).
    For anything the other tools do not cover; runs inside one undo group."""
    return _call("run_jsx", code=code)


if __name__ == "__main__":
    _setup_logging()
    log.info("starting", extra={"fields": {"platform": sys.platform, "pid": os.getpid()}})
    mcp.run()
