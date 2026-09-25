#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2"]
# ///
"""Live check: run aftereffects-mcp's tools against the real After Effects and write a report.

    uv run scripts/live_check.py

Before running: open After Effects with a NEW, EMPTY project (File > New > New Project), and allow
Settings > Scripting & Expressions > "Allow Scripts to Write Files and Access Network". Close any dialog.
Set AE_APP if your After Effects is not "Adobe After Effects 2026".

What it touches: only the open (empty) project, which it fills with test comps and layers and saves as
aemcp_check.aep in a new temporary folder, printed at the start and kept with report.md / report.json inside.
"""
import json
import os
import struct
import sys
import tempfile
import traceback
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aftereffects_mcp as d  # noqa: E402

RESULTS = []


def step(name, fn, *, needs=True, note=""):
    """Run one check; PASS/FAIL/SKIP with the result or error. Returns the value, or None."""
    if needs is not True:
        RESULTS.append({"check": name, "status": "SKIP", "detail": needs or "prerequisite failed"})
        print(f"  SKIP  {name}: {needs or 'prerequisite failed'}")
        return None
    try:
        value = fn()
    except Exception as e:  # noqa: BLE001 - every failure is a finding
        detail = f"{type(e).__name__}: {e}"
        RESULTS.append({"check": name, "status": "FAIL", "detail": detail,
                        "trace": traceback.format_exc(limit=3) if not isinstance(e, d.ToolError) else None})
        print(f"  FAIL  {name}: {detail}")
        return None
    RESULTS.append({"check": name, "status": "PASS", "detail": _short(value), "note": note})
    print(f"  PASS  {name}: {_short(value)}")
    return value if value is not None else True


def _short(v, n=300):
    text = json.dumps(v, default=str, ensure_ascii=False) if not isinstance(v, str) else v
    return text if len(text) <= n else text[:n] + "…"


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def write_png(path, w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    work = Path(tempfile.mkdtemp(prefix="aemcp_live_"))
    print(f"Work folder (kept): {work}\n")

    print("Transport")
    raw = step("transport: what DoScript prints on stdout (no result file)",
               lambda: d._osascript("'stdout ' + (1 + 1);", 30),
               note="After Effects 26.5 prints 0 whatever the script returns: results come through the result file")
    st = step("status", d.status)
    if not st:
        print("\nCannot reach After Effects: is it open with no dialog showing, and is AE_APP right?")
        return write_report(None, work)
    info = {"version": st["version"], "work": str(work), "stdout_results": bool(raw)}
    if st["items"]:
        print("\nThe open project is not empty: open a new project (File > New > New Project) and run again.")
        RESULTS.append({"check": "empty project", "status": "FAIL", "detail": f"{st['items']} items in the project"})
        return write_report(info, work)

    print("\nComps and layers")
    comp = step("create_comp 1080x1920 5 s 30 fps", lambda: d.create_comp("aemcp check", 1080, 1920, 5, 30))
    have = True if comp else "no comp"
    step("add every layer kind", lambda: [d.add_layer(k, name=k.title()) for k in
                                          ("solid", "null", "shape", "adjustment", "camera", "light")], needs=have)
    step("add a text layer (Arabic)", lambda: d.add_layer("text", text="مرحبًا aemcp"), needs=have)
    step("comp_info types", lambda: _types(), needs=have, note="text, light, camera, adjustment, shape, null, solid")
    step("set_text font/size/color/center", lambda: d.set_text(1, text="أهلًا بكم", size=120, color=[1, 0.8, 0],
                                                               justification="center"), needs=have)
    step("get_property text round trip", lambda: _text_back(), needs=have)

    print("\nProperties and keyframes")
    step("set_property position/scale/opacity on the solid", lambda: [
        d.set_property("Solid", "position", [540, 960]), d.set_property("Solid", "scale", [80, 80]),
        d.set_property("Solid", "opacity", 90)], needs=have)
    step("set_keyframes position ease_out (spatial: one ease)", lambda: d.set_keyframes(
        "Solid", "position", [{"time": 0, "value": [0, 960]}, {"time": 1, "value": [540, 960], "ease": "ease_out",
                                                               "influence": 80}]), needs=have)
    step("set_keyframes scale ease (one ease per dimension)", lambda: d.set_keyframes(
        "Solid", "scale", [{"time": 0, "value": [0, 0]}, {"time": 1, "value": [100, 100]}], clear=True), needs=have)
    step("set_keyframes opacity linear + hold", lambda: d.set_keyframes(
        "Solid", "opacity", [{"time": 0, "value": 0, "ease": "linear"}, {"time": 2, "value": 100, "ease": "hold"}],
        clear=True), needs=have)
    step("get_property scale keys", lambda: d.get_property("Solid", "scale"), needs=have)
    step("set_keyframes on Source Text", lambda: d.set_keyframes(
        1, "text", [{"time": 0, "value": "one"}, {"time": 1, "value": "two", "ease": "hold"}]), needs=have)

    print("\nExpressions and effects")
    step("set_expression wiggle on the null", lambda: _expr_ok(), needs=have)
    step("set_expression broken: error reported", lambda: _expr_bad(), needs=have,
         note="shows whether expressionError is filled right after setting")
    step("list_effects blur", lambda: d.list_effects("blur"), needs=have)
    step("add_effect Gaussian Blur Blurriness 20", lambda: d.add_effect("Solid", "Gaussian Blur", {"Blurriness": 20}),
         needs=have, note="checks the display name and parameter name in this language")
    step("add_effect by match name ADBE Glo2", lambda: d.add_effect("Solid", "ADBE Glo2"), needs=have)
    step("get_property through the effect path", lambda: d.get_property(
        "Solid", ["Effects", "Gaussian Blur", "Blurriness"]), needs=have)

    print("\nMasks, shapes, text animation")
    step("add_mask ellipse, feather 20, on the solid", lambda: d.add_mask(
        "Solid", "ellipse", rect=[140, 560, 800, 800], feather=20, name="Spot"), needs=have,
         note="Shape, MaskMode and the mask property match names")
    step("add_mask path, subtract, inverted", lambda: d.add_mask(
        "Solid", "path", points=[[0, 0], [300, 0], [150, 260]], mode="subtract", inverted=True), needs=have)
    step("add_shape rect with fill + stroke, new layer", lambda: d.add_shape(
        "rect", size=[600, 200], fill=[0.1, 0.4, 0.9], stroke=[1, 1, 1], stroke_width=6, roundness=30,
        layer_name="Card"), needs=have, note="shape contents match names; colors with alpha")
    step("add_shape star into the same layer", lambda: d.add_shape(
        "star", size=[160, 160], points=5, layer="Card", position=[380, 0]), needs=have)
    for preset in ("fade_in", "typewriter", "slide_up", "scale_in", "blur_in"):
        step(f"animate_text {preset}", lambda p=preset: _text_preset(p), needs=have,
             note="text animator, selector and property match names")
    step("export_frame mid reveal (text half shown)", lambda: _frame_at(work, "frame_text.png", 0.5), needs=have)

    print("\nLayer structure and switches")
    step("duplicate_layer the card", lambda: d.duplicate_layer("Card", name="Card copy"), needs=have,
         note="shows how After Effects names a duplicate when renamed")
    step("move_layer: copy to bottom, then above the card", lambda: (
        d.move_layer("Card copy", "bottom"), d.move_layer("Card copy", "before", other="Card"))[1], needs=have)
    step("set_switches screen + 3D + motion blur", lambda: d.set_switches(
        "Card copy", blending="screen", three_d=True, motion_blur=True), needs=have)
    step("set_switches track matte luma from the text", lambda: _matte(), needs=have,
         note="Layer.setTrackMatte (After Effects 2023+)")
    step("precompose the card and its copy", lambda: d.precompose(["Card", "Card copy"], "Cards"), needs=have)
    step("add_marker on the comp and on a layer", lambda: (
        d.add_marker(1, "beat", duration=0.5, comp="aemcp check"), d.add_marker(2, "hit", layer="Null",
                                                                             comp="aemcp check")), needs=have)
    step("set_comp work area 1-4 s", lambda: d.set_comp("aemcp check", work_area=[1, 4]), needs=have)
    templates = step("render_templates", d.render_templates, needs=have,
                     note="the names to pass as add_to_render_queue(template=...)")
    h264 = next((t for t in (templates or {}).get("outputModules", []) if "264" in t), None)
    step(f"add_to_render_queue with template {h264!r} + render", lambda: _render_h264(work, h264),
         needs=True if h264 else "no H.264 output module template found")

    print("\nImport, frame, render, save")
    png = work / "swatch.png"
    write_png(png, 64, 64, (30, 120, 220))
    step("import_file PNG + add as a layer", lambda: (d.import_file(str(png)), d.add_layer("item", item="swatch.png"))[1],
         needs=have)
    step("export_frame at 1 s", lambda: _frame(work), needs=have, note="CompItem.saveFrameToPng")
    job = step("add_to_render_queue (default template)", lambda: d.add_to_render_queue(str(work / "check_render")),
               needs=have)
    step("render and find the output", lambda: _render(work), needs=True if job else "nothing queued")

    print("\nExpression presets, timing, placement, project")
    step("expression_preset wiggle on the null", lambda: d.expression_preset("Null", "position", "wiggle",
                                                                             comp="aemcp check"), needs=have)
    step("expression_preset bounce on the keyed solid position", lambda: _bounce(), needs=have,
         note="the bounce expression must evaluate without an error")
    step("expression_preset spin + blink", lambda: (
        d.expression_preset("Null", "rotation", "spin", comp="aemcp check"),
        d.expression_preset("Null", "opacity", "blink", comp="aemcp check")), needs=have)
    step("property_tree of a text layer (depth 2)", lambda: _tree(), needs=have,
         note="the real group and property names under a text layer")
    step("sequence_layers the preset text layers, 0.2 s overlap", lambda: d.sequence_layers(
        [f"{p} aemcp" for p in ("fade_in", "typewriter", "slide_up", "scale_in", "blur_in")], start=0, overlap=0.2,
        comp="aemcp check"), needs=have)
    step("center_anchor on a text layer", lambda: d.center_anchor("fade_in aemcp", comp="aemcp check"), needs=have,
         note="sourceRectAtTime on text")
    step("fit_to_comp the swatch (fill)", lambda: d.fit_to_comp("swatch.png", "fill", comp="aemcp check"),
         needs=have, note="a 64x64 image in 1080x1920: expect scale 3000%")
    step("time_remap a precomp: slow then freeze", lambda: _remap(), needs=have)
    step("null_control over two text layers", lambda: d.null_control(
        ["typewriter aemcp", "slide_up aemcp"], name="Text Rig", comp="aemcp check"), needs=have)
    step("create_folder + move_items", lambda: (d.create_folder("Check Assets"), d.move_items(
        ["swatch.png"], "Check Assets"))[1], needs=have)
    step("replace_footage swatch with a second PNG", lambda: _replace(work), needs=have)
    step("clean_project", lambda: _clean(work), needs=have,
         note="consolidateFootage and removeUnusedFootage counts")
    step("save_project", lambda: d.save_project(str(work / "aemcp_check.aep")), needs=have)
    step("run_jsx", lambda: d.run_jsx("[app.project.numItems, app.version]"))
    step("list_items", d.list_items)
    return write_report(info, work)


def _text_preset(preset):
    d.add_layer("text", text=f"{preset} aemcp", comp="aemcp check")
    return d.animate_text(1, preset, duration=1, start=0, comp="aemcp check")


def _frame_at(work, name, t):
    path = work / name
    d.export_frame(str(path), time=t, comp="aemcp check")
    expect(path.exists(), "no frame written")
    return {"path": str(path), "bytes": path.stat().st_size}


def _matte():
    d.add_layer("text", text="MATTE", name="Matte text", comp="aemcp check")
    d.move_layer("Matte text", "before", other="Card copy", comp="aemcp check")
    return d.set_switches("Card copy", matte="luma", matte_layer="Matte text", comp="aemcp check")


def _render_h264(work, template):
    d.add_to_render_queue(str(work / "h264_check"), comp="aemcp check", template=template)
    out = d.render(timeout=600)
    files = [p.name for p in work.iterdir() if p.name.startswith("h264_check")]
    expect(files, f"no H.264 output in {work}: {out}")
    return {"render": out, "files": files}


def _bounce():
    out = d.expression_preset("Solid", "position", "bounce", comp="aemcp check")
    expect(out["error"] is None, f"expression error: {out['error']}")
    return {k: out[k] for k in ("preset", "params", "error")}


def _tree():
    out = d.property_tree("fade_in aemcp", depth=2, comp="aemcp check")
    return [(g["name"], g["matchName"]) for g in out["properties"]]


def _remap():
    d.create_comp("remap source", 1080, 1920, 4, 30)
    d.add_layer("solid", name="Source BG", comp="remap source")
    d.add_layer("item", item="remap source", comp="aemcp check")
    return d.time_remap("remap source", [{"time": 0, "source": 0}, {"time": 2, "source": 1},
                                         {"time": 3, "source": 1, "hold": True}], comp="aemcp check")


def _replace(work):
    other = work / "swatch_v2.png"
    write_png(other, 64, 64, (220, 60, 30))
    return d.replace_footage("swatch.png", str(other))


def _clean(work):
    extra = work / "unused.png"
    write_png(extra, 8, 8, (0, 0, 0))
    d.import_file(str(extra))
    return d.clean_project()


def _types():
    layers = d.comp_info()["layers"]
    return [(l["index"], l["name"], l["type"]) for l in layers]


def _text_back():
    v = d.get_property(1, "text")["value"]
    expect(v["text"] == "أهلًا بكم", f"text came back as {v['text']!r}")
    return v


def _expr_ok():
    out = d.set_expression("Null", "position", "wiggle(2, 40)")
    expect(out["error"] is None, f"expression error: {out['error']}")
    return out


def _expr_bad():
    out = d.set_expression("Null", "rotation", "time * (10")
    d.set_expression("Null", "rotation", "")
    return out


def _frame(work):
    path = work / "frame.png"
    out = d.export_frame(str(path), time=1)
    expect(path.exists() and path.read_bytes()[:4] == b"\x89PNG", "no PNG written")
    return {**out, "bytes": path.stat().st_size}


def _render(work):
    out = d.render(timeout=600)
    files = [p.name for p in work.iterdir() if p.name.startswith("check_render")]
    expect(files, f"no output file in {work}: {out}")
    return {"render": out, "files": files}


def write_report(info, work):
    counts = {s: sum(r["status"] == s for r in RESULTS) for s in ("PASS", "FAIL", "SKIP")}
    print(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped")
    (work / "report.json").write_text(json.dumps({"info": info, "counts": counts, "results": RESULTS},
                                                 indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    lines = ["# aftereffects-mcp live check\n", f"{json.dumps(info, ensure_ascii=False)}\n",
             f"**{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped**\n",
             "| Check | Result | Detail |", "|---|---|---|"]
    for r in RESULTS:
        detail = str(r.get("detail", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['check']} | {r['status']} | {detail} |")
    (work / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report: {work / 'report.md'}  (send this file back)")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
