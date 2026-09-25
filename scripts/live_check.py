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
import math
import os
import struct
import time
import wave
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


def write_wav(path, seconds=4, rate=44100):
    """Clicks every half second on a low hum: something for the amplitude to follow."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(rate * seconds):
            t = i / rate
            beat = math.exp(-(t % 0.5) * 30)
            v = 0.1 * math.sin(2 * math.pi * 110 * t) + 0.8 * beat * math.sin(2 * math.pi * 880 * t)
            frames += int(max(-1, min(1, v)) * 32767).to_bytes(2, "little", signed=True)
        w.writeframes(bytes(frames))


def write_png(path, w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_moving_square(folder, frames=31, w=640, h=360):
    """A PNG sequence: a checkered 24 px square on gray, centered at (100 + 12 f, 150 + 4 f) on frame f."""
    for f in range(frames):
        cx, cy = 100 + 12 * f, 150 + 4 * f
        rows = []
        for y in range(h):
            row = bytearray(b"\x28" * (w * 3))
            if abs(y - cy) < 12:
                for x in range(cx - 12, cx + 12):
                    v = 230 if ((x - cx + 12) // 6 + (y - cy + 12) // 6) % 2 else 90
                    row[x * 3:x * 3 + 3] = bytes((v, v, v))
            rows.append(bytes(row))
        raw = b"".join(b"\x00" + r for r in rows)

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        (folder / f"square_{f:04d}.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


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

    print("\nCamera, light, audio, captions, Essential Graphics")
    step("camera_move push_in (new camera)", lambda: d.camera_move("push_in", amount=500, duration=2,
                                                                     comp="aemcp check"), needs=have)
    step("camera_move orbit 90 degrees", lambda: d.camera_move("orbit", duration=3, start=2, camera="Camera",
                                                               comp="aemcp check"), needs=have,
         note="rig null with Y Rotation; the camera parented to it")
    step("set_light spot 120 %, warm, shadows", lambda: d.set_light(
        "Light", type="spot", intensity=120, color=[1, 0.9, 0.8], cone_angle=60, shadows=True, comp="aemcp check"),
         needs=have, note="light option match names")
    wav = work / "beat.wav"
    write_wav(wav)
    step("import a WAV, audio_fade in 0.5 s / out 1 s at -6 dB", lambda: _audio(wav), needs=have)
    step("audio_react: a circle's scale pumps with the beat", lambda: _react(), needs=have,
         note="Convert Audio to Keyframes through app.findMenuCommandId / executeCommand")
    srt = work / "captions.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nمرحبًا بكم\n\n2\n00:00:02,500 --> 00:00:04,000\n"
                   "Second <i>line</i>\nand more\n", encoding="utf-8")
    step("captions_from_srt (Arabic + two lines)", lambda: d.captions_from_srt(str(srt), comp="aemcp check"),
         needs=have)
    step("mogrt_add_property + export_mogrt", lambda: _mogrt(work), needs=have,
         note="addToMotionGraphicsTemplateAs and exportAsMotionGraphicsTemplate")

    print("\nKeyframe work, editing, variants, footage")
    step("bake_expression: the null's wiggle into keyframes (every 2 frames)", lambda: d.bake_expression(
        "Null", "position", step=2, start=0, end=1, comp="aemcp check"), needs=have,
         note="valueAtTime(t, false) and setValuesAtTimes")
    step("ease_keyframes: the solid's scale, strong ease", lambda: d.ease_keyframes(
        "Solid", "scale", "ease", influence=80, comp="aemcp check"), needs=have)
    step("copy_keyframes: the solid's opacity onto two captions, cascading", lambda: d.copy_keyframes(
        "Solid", "opacity", ["Caption 1", "Caption 2"], offset=0.2, comp="aemcp check"), needs=have,
         note="keyIn/OutInterpolationType and keyIn/OutTemporalEase")
    step("stagger_layers the captions by 0.1 s", lambda: d.stagger_layers(
        ["Caption 1", "Caption 2"], offset=0.1, comp="aemcp check"), needs=have)
    step("split_layer the Pulse circle at 1.5 s", lambda: d.split_layer("Pulse", 1.5, name="Pulse B",
                                                                         comp="aemcp check"), needs=have)
    step("make_variants: two versions of a card comp, queued", lambda: _variants(work), needs=have,
         note="CompItem.duplicate and text replacement")
    step("trim_comp to its layers", lambda: d.trim_comp(comp="variants source"), needs=have)
    step("find_missing_footage after deleting a file, then relink", lambda: _missing(work), needs=have)

    print("\nShape animation, typography, rhythm, render queue")
    step("trim_paths: draw a stroked ring on over 1 s", lambda: _ring(), needs=have,
         note="Trim Paths match names on the shape root")
    step("shape_repeater: 6 dots, 80 px apart, fading", lambda: _dots(), needs=have,
         note="Repeater match names and its transform group")
    step("add_paragraph (Arabic, 700x300 box) + text_style", lambda: _paragraph(), needs=have,
         note="addBoxText, tracking, leading, stroke, allCaps on TextDocument")
    step("set_motion_blur on, shutter 270", lambda: d.set_motion_blur(shutter_angle=270, comp="aemcp check"),
         needs=have)
    step("markers_from_audio on the beat WAV (clicks every 0.5 s: expect about 120 bpm)", lambda: d.markers_from_audio(
        "beat.wav", comp="aemcp check"), needs=have)
    step("sequence_to_markers: the two captions on the first beats", lambda: d.sequence_to_markers(
        ["Caption 1", "Caption 2"], comp="aemcp check"), needs=have)
    step("render_queue_list + clear finished items", lambda: (d.render_queue_list(), d.clear_render_queue())[1],
         needs=have, note="RQItemStatus names and RenderQueueItem.remove")
    print("\nMotion, tracking, 3D")
    step("motion_path: curved, even speed, auto-orient", lambda: _path(), needs=have,
         note="spatial auto Bezier, roving inner keys, AutoOrientType.ALONG_PATH")
    step("bezier_ease ease_in_out_cubic matches the curve", lambda: _ease(), needs=have,
         note="rotation sampled against cubic-bezier(0.65, 0, 0.35, 1)")
    step("set_comp motion blur 180", lambda: d.set_comp("aemcp check", motion_blur=True, shutter_angle=180),
         needs=have)
    seq = work / "track"
    seq.mkdir()
    write_moving_square(seq)
    track = step("track_point a moving square (1 s)", lambda: _track(seq), needs=have,
                 note="the square's center moves 12 px right and 4 px down per frame: max error in pixels")
    step("attach_to_track: a text follows the track", lambda: _attach(), needs=True if track else "no track")
    step("set_camera 50 mm, depth of field, autofocus", lambda: _lens(), needs=have)
    step("set_3d_layer: extruded, beveled text", lambda: _extrude(work), needs=have,
         note="the comp switches to the Advanced 3D renderer (ADBE Calder); a frame renders")
    step("depth_stack three layers + camera push", lambda: _stack(work), needs=have)

    step("save_project", lambda: d.save_project(str(work / "aemcp_check.aep")), needs=have)
    step("render_background through aerender", lambda: _background(work), needs=have,
         note="set AE_RENDER if aerender is not next to the application")
    step("reduce_project to the check comp (last: it deletes items)", lambda: d.reduce_project(["aemcp check"]),
         needs=have, note="Project.reduceProject; the project was saved just before")
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


def _audio(wav):
    d.import_file(str(wav))
    d.add_layer("item", item="beat.wav", comp="aemcp check")
    return d.audio_fade("beat.wav", fade_in=0.5, fade_out=1, level_db=-6, comp="aemcp check")


def _react():
    d.add_shape("ellipse", size=[300, 300], layer_name="Pulse", comp="aemcp check")
    out = d.audio_react("beat.wav", "Pulse", "scale", amount=2, comp="aemcp check")
    expect(out["error"] is None, f"expression error: {out['error']}")
    return out


def _mogrt(work):
    d.add_layer("text", text="Editable title", name="Title", comp="aemcp check")
    d.mogrt_add_property("Title", "text", name="Title text", comp="aemcp check")
    path = work / "aemcp_check.mogrt"
    out = d.export_mogrt(str(path), name="aemcp check", comp="aemcp check")
    expect(path.exists(), "no .mogrt written")
    return {**out, "bytes": path.stat().st_size}


def _background(work):
    d.add_to_render_queue(str(work / "background_check"), comp="aemcp check")
    job = d.render_background()
    deadline = time.time() + 900
    while time.time() < deadline:
        st = d.render_background_status(job["job"])
        if st["state"] != "running":
            break
        time.sleep(3)
    expect(st["state"] == "done", f"aerender: {st}")
    files = [p.name for p in work.iterdir() if p.name.startswith("background_check")]
    expect(files, f"no output from aerender in {work}")
    return {"state": st["state"], "files": files, "log_tail": st["log_tail"][-3:]}


def _variants(work):
    d.create_comp("variants source", 1080, 1080, 3, 30)
    d.add_layer("text", text="NAME", name="Name", comp="variants source")
    return d.make_variants([{"name": "Variant One", "texts": {"Name": "واحد"}},
                            {"name": "Variant Two", "texts": {"Name": "Two"}}], comp="variants source",
                           output_dir=str(work / "variants"))


def _missing(work):
    gone = work / "to_move.png"
    write_png(gone, 16, 16, (10, 200, 10))
    d.import_file(str(gone))
    moved = work / "moved"
    moved.mkdir()
    gone.rename(moved / "to_move.png")
    out = d.find_missing_footage(search=str(moved))
    expect(out["relinked"] and not out["still_missing"], f"relink failed: {out}")
    return out


def _ring():
    d.add_shape("ellipse", size=[400, 400], stroke=[1, 1, 1], stroke_width=12, layer_name="Ring",
                comp="aemcp check")
    return d.trim_paths("Ring", duration=1, start=0, comp="aemcp check")


def _dots():
    d.add_shape("ellipse", size=[30, 30], fill=[1, 0.5, 0], layer_name="Dots", comp="aemcp check")
    return d.shape_repeater("Dots", copies=6, offset=[80, 0], end_opacity=10, comp="aemcp check")


def _paragraph():
    d.add_paragraph("نص طويل يلتف داخل صندوق الفقرة في أفتر إفكتس", box=[700, 300], name="Paragraph",
                    comp="aemcp check")
    out = d.text_style("Paragraph", tracking=40, leading=80, stroke_color=[0, 0, 0], stroke_width=3,
                       comp="aemcp check")
    expect(out["value"].get("strokeWidth") == 3, f"stroke not applied: {out}")
    return out


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


def _path():
    d.add_shape("star", size=[120, 120], layer_name="Flyer", comp="aemcp check")
    out = d.motion_path("Flyer", [[100, 300], [900, 700], [200, 1200], [900, 1700]], duration=3, constant_speed=True,
                        auto_orient=True, comp="aemcp check")
    keys = d.get_property("Flyer", "position", comp="aemcp check")["keys"]
    expect(len(keys) == 4, f"{len(keys)} keys")
    return {**out, "times": [k["time"] for k in keys]}


def _css(x1, y1, x2, y2, x):
    lo, hi = 0.0, 1.0
    for _ in range(60):
        t = (lo + hi) / 2
        bx = 3 * (1 - t) ** 2 * t * x1 + 3 * (1 - t) * t * t * x2 + t ** 3
        lo, hi = (t, hi) if bx < x else (lo, t)
    t = (lo + hi) / 2
    return 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t * t * y2 + t ** 3


def _ease():
    d.set_keyframes("Flyer", "rotation", [{"time": 0, "value": 0}, {"time": 2, "value": 360}], comp="aemcp check")
    d.bezier_ease("Flyer", "rotation", "ease_in_out_cubic", comp="aemcp check")
    got = d.run_jsx('var c = null, i; for (i = 1; i <= app.project.numItems; i++) '
                    'if (app.project.item(i).name === "aemcp check") c = app.project.item(i); '
                    'var p = c.layer("Flyer").property("ADBE Transform Group").property("ADBE Rotate Z"), r = []; '
                    'for (i = 0; i <= 8; i++) r.push(p.valueAtTime(i / 4, true)); r')
    err = max(abs(v / 360 - _css(0.65, 0, 0.35, 1, i / 8)) for i, v in enumerate(got))
    expect(err < 0.002, f"off the curve by {err:.4f}")
    return {"samples": got, "max_error": round(err, 5)}


def _track(seq):
    it = d.import_file(str(seq / "square_0000.png"), sequence=True)
    d.run_jsx(f"app.project.itemByID({it['id']}).mainSource.conformFrameRate = 30; 1")
    d.create_comp("track check", 640, 360, 1, 30)
    d.add_layer("item", item=it["id"], name="Plate", comp="track check")
    out = d.track_point("Plate", [100, 150], start=0, end=1, comp="track check")
    keys = d.get_property("Plate", ["Motion Trackers", "aemcp track", "Track Point 1", "Attach Point"],
                          comp="track check")["keys"]
    err = max(max(abs(k["value"][0] - (100 + 360 * k["time"])), abs(k["value"][1] - (150 + 120 * k["time"])))
              for k in keys)
    expect(err < 0.75, f"the track is off by {err:.2f} px")
    return {**out, "max_error_px": round(err, 3)}


def _attach():
    d.add_layer("text", text="TRACKED", name="Tag", comp="track check")
    out = d.attach_to_track("Tag", "Plate", comp="track check")
    expect(out["error"] is None, f"expression error: {out['error']}")
    at = d.run_jsx('var c = null, i; for (i = 1; i <= app.project.numItems; i++) '
                   'if (app.project.item(i).name === "track check") c = app.project.item(i); '
                   'c.layer("Tag").property("ADBE Transform Group").property("ADBE Position").valueAtTime(0.5, false)')
    expect(abs(at[0] - 280) < 1 and abs(at[1] - 210) < 1, f"at 0.5 s the text is at {at}, the square at [280, 210]")
    return {"expression_error": out["error"], "position_at_0.5s": at}


def _lens():
    d.add_layer("text", text="FOCUS", name="Focus target", comp="aemcp check")
    out = d.set_camera("Camera", focal_length=50, depth_of_field=True, f_stop=2.8, focus_on="Focus target",
                       comp="aemcp check")
    expect(out["error"] is None, f"autofocus expression error: {out['error']}")
    return out


def _extrude(work):
    d.create_comp("extrude check", 1080, 1080, 1, 30)
    d.add_layer("text", text="3D", name="Logo", comp="extrude check")
    d.set_text("Logo", size=400, comp="extrude check")
    d.add_layer("light", name="Key", comp="extrude check")
    out = d.set_3d_layer("Logo", extrusion=80, bevel=6, bevel_style="convex", metal=60, comp="extrude check")
    d.set_property("Logo", ["ADBE Transform Group", "ADBE Rotate Y"], 35, comp="extrude check")
    d.add_layer("camera", name="Cam", comp="extrude check")
    path = work / "extrude.png"
    d.export_frame(str(path), time=0, comp="extrude check")
    return {**out, "frame": str(path)}


def _stack(work):
    d.create_comp("stack check", 1920, 1080, 4, 30)
    for name, color in (("Sky", [0.2, 0.4, 0.8]), ("Hills", [0.2, 0.6, 0.3]), ("Tree", [0.4, 0.25, 0.1])):
        d.add_layer("solid", name=name, color=color, comp="stack check")
    d.set_property("Hills", "scale", [60, 40], comp="stack check")
    d.set_property("Tree", "scale", [15, 50], comp="stack check")
    out = d.depth_stack(["Tree", "Hills", "Sky"], spacing=800, comp="stack check")
    d.camera_move("pan_right", amount=500, duration=4, camera="Camera", comp="stack check")
    path = work / "stack.png"
    d.export_frame(str(path), time=2, comp="stack check")
    return {**out, "frame": str(path)}


if __name__ == "__main__":
    sys.exit(main())
