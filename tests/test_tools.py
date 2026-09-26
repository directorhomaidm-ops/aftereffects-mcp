import asyncio
import json
import os
import re
import subprocess

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import aftereffects_mcp as d
from conftest import ROOT


def test_all_tools_registered():
    names = {t.name for t in asyncio.run(d.mcp.list_tools())}
    assert names == {
        "status", "list_items", "create_comp", "comp_info", "add_layer", "delete_layer", "set_layer", "set_text",
        "get_property", "set_property", "set_keyframes", "set_expression", "add_effect", "list_effects",
        "import_file", "add_to_render_queue", "render", "export_frame", "save_project", "run_jsx",
        "add_mask", "add_shape", "animate_text", "precompose", "duplicate_layer", "move_layer", "set_switches",
        "add_marker", "set_comp", "render_templates", "expression_preset", "property_tree", "sequence_layers",
        "time_remap", "fit_to_comp", "center_anchor", "null_control", "apply_preset", "replace_footage",
        "create_folder", "move_items", "clean_project", "camera_move", "set_light", "audio_fade", "audio_react",
        "captions_from_srt", "render_background", "render_background_status", "mogrt_add_property", "export_mogrt",
        "bake_expression", "ease_keyframes", "copy_keyframes", "stagger_layers", "split_layer", "trim_comp",
        "make_variants", "import_layered", "find_missing_footage",
        "motion_path", "bezier_ease", "track_point", "attach_to_track", "set_camera", "set_3d_layer", "depth_stack",
    }


def test_library_is_es3():
    """ExtendScript is ES3: no ES6 syntax (the host also strips ES5 built-ins at run time)."""
    src = (ROOT / "jsx" / "aemcp.jsx").read_text()
    code = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*', '""', src)  # ignore strings and comments
    for pattern, what in [(r"=>", "arrow function"), (r"\blet\b", "let"), (r"\bconst\b", "const"), (r"`", "template"),
                          (r"\bclass\b", "class"), (r"\.\.\.", "spread"), (r"\bJSON\.", "JSON")]:
        assert not re.search(pattern, code), what


def test_es5_builtins_are_gone_in_the_host(ae):
    with pytest.raises(ToolError, match="not a function"):
        d.run_jsx("[1, 2].map(function (x) { return x; })")


def test_status_and_empty_project(ae):
    assert d.status() == {"version": "26.0x10", "project": None, "items": 0, "activeComp": None}
    assert d.list_items() == []
    with pytest.raises(ToolError, match="no active comp"):
        d.comp_info()


def test_create_comp(ae):
    c = d.create_comp("Reel", 1080, 1920, duration=15, frame_rate=30, bg_color=[0.1, 0.1, 0.1])
    assert (c["name"], c["width"], c["height"], c["duration"], c["frameRate"], c["layers"]) == \
        ("Reel", 1080, 1920, 15, 30, [])
    assert d.status()["activeComp"] == "Reel"
    assert d.list_items() == [{"id": 1, "name": "Reel", "type": "comp", "folder": None, "width": 1080,
                               "height": 1920, "duration": 15, "frameRate": 30}]
    assert ae.inspect("ae.undoLog") == ["aemcp: create_comp"]
    for kwargs, msg in [({"width": 1}, "width and height"), ({"duration": 0}, "duration"),
                        ({"frame_rate": 0}, "frame_rate"), ({"bg_color": [2, 0, 0]}, "bg_color")]:
        with pytest.raises(ToolError, match=msg):
            d.create_comp("x", **kwargs)


def test_comp_lookup(ae):
    d.create_comp("A")
    d.create_comp("A")
    with pytest.raises(ToolError, match="several comps are named A: pass the id, one of 1, 2"):
        d.comp_info("A")
    assert d.comp_info(2)["id"] == 2
    with pytest.raises(ToolError, match="comp not found: Z"):
        d.comp_info("Z")


def test_add_layers_on_top(ae):
    d.create_comp("Main", duration=5)
    d.add_layer("solid", name="BG", color=[0, 0, 1])
    d.add_layer("text", text="Hello")
    d.add_layer("null", name="Rig")
    d.add_layer("adjustment", name="Look")
    d.add_layer("shape")
    d.add_layer("camera")
    d.add_layer("light")
    layers = d.comp_info()["layers"]
    assert [(l["index"], l["name"], l["type"]) for l in layers] == [
        (1, "Light", "light"), (2, "Camera", "camera"), (3, "Shape Layer 5", "shape"), (4, "Look", "adjustment"),
        (5, "Rig", "null"), (6, "Hello", "text"), (7, "BG", "solid")]
    assert layers[6]["outPoint"] == 5 and layers[0]["effects"] == []
    with pytest.raises(ToolError, match="kind must be"):
        d.add_layer("sticker")
    with pytest.raises(ToolError, match="needs item"):
        d.add_layer("item")


def test_footage_and_precomp_layers(ae, tmp_path):
    clip = tmp_path / "shot.mov"
    clip.write_bytes(b"x")
    imported = d.import_file(str(clip))
    assert imported == {"id": 1, "name": "shot.mov", "type": "footage"}
    d.create_comp("Inner")
    d.create_comp("Outer")
    d.add_layer("item", item="shot.mov")
    d.add_layer("item", item="Inner")
    assert [(l["type"], l["source"]) for l in d.comp_info("Outer")["layers"]] == [("precomp", "Inner"),
                                                                                  ("footage", "shot.mov")]
    assert d.list_items()[0]["file"] == str(clip)
    with pytest.raises(ToolError, match="project item not found: nope"):
        d.add_layer("item", item="nope")


def test_import_sequence_and_errors(ae, tmp_path):
    first = tmp_path / "frame_0001.png"
    first.write_bytes(b"x")
    assert d.import_file(str(first), sequence=True)["name"] == "frame_[####].png"
    with pytest.raises(ToolError, match="file not found"):
        d.import_file(str(tmp_path / "missing.mov"))


def test_set_layer_timing_and_parent(ae):
    d.create_comp("Main", duration=10)
    d.add_layer("null", name="Rig")
    d.add_layer("text", text="Title")
    out = d.set_layer("Title", name="Headline", start_time=1, in_point=1.5, out_point=4, parent="Rig")
    assert (out["name"], out["startTime"], out["inPoint"], out["outPoint"], out["parent"]) == ("Headline", 1, 1.5, 4, 2)
    assert d.set_layer(1, unparent=True)["parent"] is None
    with pytest.raises(ToolError, match="give parent or unparent"):
        d.set_layer(1, parent=2, unparent=True)
    with pytest.raises(ToolError, match="cannot be its own parent"):
        d.set_layer(1, parent=1)
    with pytest.raises(ToolError, match=r"layer 9 not found in Main \(2 layers\)"):
        d.set_layer(9, name="x")


def test_delete_layer(ae):
    d.create_comp("Main")
    d.add_layer("null", name="A")
    d.add_layer("null", name="B")
    assert d.delete_layer("A") == {"deleted": "A", "layers": 1}


def test_set_text_arabic_and_style(ae):
    d.create_comp("Main")
    d.add_layer("text", text="x")
    out = d.set_text(1, text="مرحبًا بكم", font="Arial-BoldMT", size=96, color=[1, 0.8, 0], justification="center")
    assert out["value"] == {"text": "مرحبًا بكم", "font": "Arial-BoldMT", "fontSize": 96, "fillColor": [1, 0.8, 0]}
    assert ae.inspect("ae.app.project.item(1).layer(1).groups[0].children[0]._value.justification") == 7415
    with pytest.raises(ToolError, match="nothing to change"):
        d.set_text(1)
    with pytest.raises(ToolError, match="justification"):
        d.set_text(1, justification="justify")


def test_properties(ae):
    d.create_comp("Main", 1920, 1080)
    d.add_layer("solid", name="BG")
    assert d.get_property(1, "position") == {"property": "Position", "matchName": "ADBE Position",
                                             "value": [960, 540, 0], "keys": [], "expression": None}
    assert d.set_property(1, "scale", [50, 50])["value"] == [50, 50, 100]
    assert d.set_property(1, ["Transform", "Opacity"], 40)["value"] == 40
    assert d.get_property(1, ["ADBE Transform Group", "ADBE Rotate Z"])["property"] == "Rotation"
    with pytest.raises(ToolError, match=r"no property Wobble under Transform \(has: Anchor Point, Position"):
        d.get_property(1, ["Transform", "Wobble"])
    with pytest.raises(ToolError, match="Transform is a group, not a property"):
        d.get_property(1, "Transform")
    with pytest.raises(ToolError, match="needs an array"):
        d.set_property(1, "position", 5)


def test_keyframes_and_eases(ae):
    d.create_comp("Main", duration=5)
    d.add_layer("solid", name="BG")
    out = d.set_keyframes(1, "position", [{"time": 0, "value": [0, 540]}, {"time": 1, "value": [960, 540],
                                                                              "ease": "ease_out", "influence": 75}])
    assert (out["keys"], out["times"]) == (2, [0, 1])
    key = "ae.app.project.item(1).layer(1).groups[0].children[1].keys"
    # position is spatial: one ease per side; ease_out keeps the incoming side flat
    assert ae.inspect(f"Array.from({key}[1].outEase, e => e.influence)") == [75]
    assert ae.inspect(f"Array.from({key}[1].inEase, e => e.influence)") == [0.1]
    d.set_keyframes(1, "scale", [{"time": 0, "value": [0, 0]}, {"time": 0.5, "value": [100, 100], "ease": "linear"},
                                 {"time": 2, "value": [120, 120], "ease": "hold"}])
    skey = "ae.app.project.item(1).layer(1).groups[0].children[2].keys"
    assert ae.inspect(f"{skey}[0].inEase.length") == 3  # scale is not spatial: one ease per dimension
    assert ae.inspect(f"{skey}.map(k => k.outType)") == [6613, 6612, 6614]  # bezier, linear, hold
    got = d.get_property(1, "scale")
    assert got["keys"] == [{"time": 0, "value": [0, 0, 100]}, {"time": 0.5, "value": [100, 100, 100]},
                           {"time": 2, "value": [120, 120, 100]}]
    with pytest.raises(ToolError, match="is animated"):
        d.set_property(1, "scale", [10, 10])
    assert d.set_keyframes(1, "scale", [{"time": 1, "value": [50, 50]}], clear=True)["keys"] == 1
    for keys, msg in [([], "no keys"), ([{"time": 0}], "time and value"),
                      ([{"time": 0, "value": 1, "ease": "bounce"}], "ease must be"),
                      ([{"time": 0, "value": 1, "influence": 500}], "influence")]:
        with pytest.raises(ToolError, match=msg):
            d.set_keyframes(1, "opacity", keys)


def test_text_keyframes(ae):
    d.create_comp("Main")
    d.add_layer("text", text="One")
    out = d.set_keyframes(1, "text", [{"time": 0, "value": "One"}, {"time": 1, "value": "Two", "ease": "hold"}])
    assert out["keys"] == 2
    assert [k["value"]["text"] for k in d.get_property(1, "text")["keys"]] == ["One", "Two"]


def test_expressions(ae):
    d.create_comp("Main")
    d.add_layer("solid", name="BG")
    out = d.set_expression(1, "position", "wiggle(2, 30)")
    assert (out["enabled"], out["error"]) == (True, None)
    assert d.get_property(1, "position")["expression"] == "wiggle(2, 30)"
    bad = d.set_expression(1, "rotation", "time * (10")
    assert bad["error"].startswith("Error: SyntaxError")
    assert d.set_expression(1, "position", "")["enabled"] is False


def test_effects(ae):
    d.create_comp("Main")
    d.add_layer("solid", name="BG")
    out = d.add_effect(1, "Gaussian Blur", {"Blurriness": 20})
    assert out == {"layer": "BG", "effect": "Gaussian Blur", "matchName": "ADBE Gaussian Blur 2",
                   "set": {"Blurriness": 20}}
    assert d.add_effect(1, "ADBE Gaussian Blur 2")["effect"] == "Gaussian Blur 2"
    assert d.comp_info()["layers"][0]["effects"] == ["Gaussian Blur", "Gaussian Blur 2"]
    assert d.get_property(1, ["Effects", "Gaussian Blur", "Blurriness"])["value"] == 20
    d.add_effect(1, "Fill", {"Color": [0, 1, 0, 1]}, name="Tint")
    with pytest.raises(ToolError, match="unknown effect: Sparkle"):
        d.add_effect(1, "Sparkle")
    with pytest.raises(ToolError, match=r"Glow was added but has no parameter Size \(has: Glow Threshold"):
        d.add_effect(1, "Glow", {"Size": 3})
    d.add_layer("camera")
    with pytest.raises(ToolError):
        d.add_effect("Camera", "Glow")  # cameras take no effects


def test_list_effects(ae):
    assert [e["name"] for e in d.list_effects()] == ["Gaussian Blur", "Glow", "Fill"]
    assert d.list_effects("blur & sharpen") == [{"name": "Gaussian Blur", "matchName": "ADBE Gaussian Blur 2",
                                                 "category": "Blur & Sharpen"}]


def test_render_queue(ae, tmp_path):
    d.create_comp("Main")
    with pytest.raises(ToolError, match="nothing queued"):
        d.render()
    q = d.add_to_render_queue(str(tmp_path / "main.mp4"), template="H.264 - Match Render Settings - 15 Mbps")
    assert q == {"comp": "Main", "index": 1, "output": str(tmp_path / "main.mp4")}
    with pytest.raises(ToolError, match="no output module template named Nope"):
        d.add_to_render_queue(str(tmp_path / "x.mov"), template="Nope")
    assert ae.inspect("ae.app.project.renderQueue.numItems") == 1  # the failed item was taken out again
    out = d.render(timeout=600)
    assert out["rendered"] == 1 and (tmp_path / "main.mp4").read_text() == "rendered Main"
    assert out["items"] == [{"comp": "Main", "status": "done", "output": str(tmp_path / "main.mp4")}]
    assert ae.scripts[-1][1] == 600  # the render's own timeout reaches the transport


def test_export_frame_and_save(ae, tmp_path):
    d.create_comp("Main", duration=2)
    assert d.export_frame(str(tmp_path / "f.png"), time=1)["path"] == str(tmp_path / "f.png")
    assert (tmp_path / "f.png").read_bytes().startswith(b"\x89PNG")
    with pytest.raises(ToolError, match="outside the comp"):
        d.export_frame(str(tmp_path / "g.png"), time=5)
    with pytest.raises(ToolError, match="never saved"):
        d.save_project()
    assert d.save_project(str(tmp_path / "p.aep")) == {"project": str(tmp_path / "p.aep")}
    assert d.save_project()["project"] == str(tmp_path / "p.aep")


def test_run_jsx(ae):
    d.create_comp("Main")
    assert d.run_jsx("app.project.numItems + 1") == 2
    assert d.run_jsx("[app.project.item(1).name, 1.23456]") == ["Main", 1.2346]
    with pytest.raises(ToolError, match="nope is not defined"):
        d.run_jsx("nope + 1")


def test_undo_groups(ae):
    d.create_comp("Main")
    d.add_layer("null")
    d.comp_info()
    d.list_items()
    with pytest.raises(ToolError):
        d.set_layer(5, name="x")  # a failing change still closes its group
    assert ae.inspect("ae.undoLog") == ["aemcp: create_comp", "aemcp: add_layer", "aemcp: set_layer"]


def test_unexpected_replies(ae, monkeypatch):
    monkeypatch.setattr(d, "RUNNER", lambda script, timeout: "")
    with pytest.raises(ToolError, match="Allow Scripts to Write Files"):
        d.status()
    monkeypatch.setattr(d, "RUNNER", lambda script, timeout: "undefined")
    with pytest.raises(ToolError, match="unexpected reply"):
        d.status()


# --- the macOS transport ---


def test_osascript_command(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, capture_output, text, timeout):
        calls.append((cmd, timeout))
        src = re.search(r'new File\(\\"(.+?)\\"\)', cmd[4]).group(1)
        body = open(src, encoding="utf-8").read()
        assert "__AEMCP_RESULT_FILE__" not in body and "مرحبا" in body  # UTF-8 file, placeholder filled in
        return subprocess.CompletedProcess(cmd, 0, '{"ok": 5}\n', "")

    monkeypatch.setattr(d.sys, "platform", "darwin")
    monkeypatch.setattr(d.subprocess, "run", fake_run)
    monkeypatch.setenv("AE_APP", "Adobe After Effects 2026")
    assert d._osascript('"مرحبا"; __AEMCP_RESULT_FILE__', 30) == '{"ok": 5}'
    cmd, timeout = calls[0]
    assert cmd[:3] == ["osascript", "-e", "with timeout of 30 seconds"] and cmd[-1] == "end timeout"
    assert cmd[4].startswith('tell application "Adobe After Effects 2026" to DoScript "var __f = new File(\\"')
    assert timeout == 45


def test_osascript_result_file_fallback(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        src = re.search(r'new File\(\\"(.+?)\\"\)', cmd[4]).group(1)
        out = re.search(r'new File\("(.+?)"\)', open(src, encoding="utf-8").read()).group(1)
        with open(out, "w", encoding="utf-8") as f:
            f.write('{"ok": "from file"}')
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(d.sys, "platform", "darwin")
    monkeypatch.setattr(d.subprocess, "run", fake_run)
    assert d._osascript("x = new File(__AEMCP_RESULT_FILE__)", 30) == '{"ok": "from file"}'


@pytest.mark.parametrize("stderr, msg", [
    ("execution error: Can’t get application \"Adobe After Effects 2026\". (-1728)", "set AE_APP"),
    ("execution error: AppleEvent timed out. (-1712)", "did not answer within 30s"),
    ("execution error: something else (-1)", "osascript failed: execution error: something else"),
])
def test_osascript_errors(monkeypatch, stderr, msg):
    monkeypatch.setattr(d.sys, "platform", "darwin")
    monkeypatch.setattr(d.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", stderr))
    with pytest.raises(ToolError, match=msg):
        d._osascript("1", 30)


def test_osascript_macos_only(monkeypatch):
    monkeypatch.setattr(d.sys, "platform", "linux")
    with pytest.raises(ToolError, match="macOS only"):
        d._osascript("1", 30)


def test_replies_are_ascii(ae, monkeypatch):
    """Results cross AppleScript and a shell on the way back: non-ASCII text travels as \\uXXXX escapes."""
    raw = []
    run = d.RUNNER
    monkeypatch.setattr(d, "RUNNER", lambda script, timeout: raw.append(run(script, timeout)) or raw[-1])
    d.create_comp("عربي")
    assert d.comp_info()["name"] == "عربي"
    assert all(r.isascii() for r in raw) and "\\u0639" in raw[-1]



# --- masks, shapes, text animation ---

L1 = "ae.app.project.item(1).layer(1)"


def _group(layer_expr, match):
    return f"{layer_expr}.groups.find(g => g.matchName === {json.dumps(match)})"


def test_add_mask_rect_ellipse_path(ae):
    d.create_comp("Main", 1000, 500)
    d.add_layer("solid", name="BG")
    out = d.add_mask("BG", "rect", rect=[100, 50, 200, 100], mode="subtract", feather=12, expansion=-3, opacity=80,
                     inverted=True, name="Hole")
    assert out == {"layer": "BG", "mask": "Hole", "masks": 1, "mode": "subtract", "vertices": 4}
    m = _group(L1, "ADBE Mask Parade") + ".children[0]"
    assert ae.inspect(f"{m}.children[0]._value.vertices") == [[100, 50], [300, 50], [300, 150], [100, 150]]
    assert ae.inspect(f"[{m}.maskMode, {m}.inverted, {m}.children[1]._value, {m}.children[2]._value, "
                      f"{m}.children[3]._value]") == [6814, True, [12, 12], 80, -3]
    d.add_mask("BG", "ellipse", rect=[0, 0, 200, 100])
    e = _group(L1, "ADBE Mask Parade") + ".children[1].children[0]._value"
    assert ae.inspect(f"{e}.vertices") == [[100, 0], [200, 50], [100, 100], [0, 50]]
    assert ae.inspect(f"{e}.outTangents[0][0]") == pytest.approx(100 * 0.5523)  # round quarter arcs
    assert d.add_mask("BG", "path", points=[[0, 0], [50, 100], [100, 0]])["vertices"] == 3
    # the mask's feather animates through the normal property path
    assert d.set_keyframes("BG", ["Masks", "Hole", "Mask Feather"], [{"time": 0, "value": [0, 0]},
                                                                      {"time": 1, "value": [40, 40]}])["keys"] == 2
    for kwargs, msg in [({"rect": [0, 0, 0, 5]}, "positive width"), ({"shape": "path", "points": [[0, 0]]}, "3 points"),
                        ({"shape": "star"}, "shape must be"), ({"rect": [0, 0, 5, 5], "mode": "xor"}, "mode must"),
                        ({"rect": [0, 0, 5, 5], "opacity": 120}, "opacity")]:
        with pytest.raises(ToolError, match=msg):
            d.add_mask("BG", **kwargs)
    d.add_layer("camera")
    with pytest.raises(ToolError, match="cannot take masks"):
        d.add_mask("Camera", "rect", rect=[0, 0, 5, 5])


def test_add_shape_layers_and_groups(ae):
    d.create_comp("Main")
    out = d.add_shape("rect", size=[300, 120], fill=[1, 0, 0], stroke=[0, 0, 0], stroke_width=6, roundness=20,
                      layer_name="Card", name="Box")
    assert out == {"layer": "Card", "index": 1, "group": "Box", "shape": "rect"}
    grp = _group(L1, "ADBE Root Vectors Group") + ".children[0]"
    contents = grp + ".children[0].children"
    assert ae.inspect(f"{contents}.map(c => c.matchName)") == [
        "ADBE Vector Shape - Rect", "ADBE Vector Graphic - Fill", "ADBE Vector Graphic - Stroke"]
    assert ae.inspect(f"[{contents}[0].children[0]._value, {contents}[0].children[2]._value, "
                      f"{contents}[1].children[0]._value, {contents}[2].children[1]._value]") == \
        [[300, 120], 20, [1, 0, 0, 1], 6]  # colors carry alpha
    # a second shape into the same layer, as its own group, moved off the anchor
    d.add_shape("star", size=[100, 100], points=6, layer="Card", position=[150, 0])
    star = _group(L1, "ADBE Root Vectors Group") + ".children[1]"
    assert ae.inspect(f"{star}.name") == "Group 2"
    assert ae.inspect(f"{star}.children[0].children[0].children.map(c => c._value)") == [1, 6, 50, 25]
    assert ae.inspect(f"{star}.children[0].children[1].children[0]._value") == [1, 1, 1, 1]  # default white fill
    assert ae.inspect(f"{star}.children[1].children[0]._value") == [150, 0]
    d.add_shape("polygon", points=3)
    assert ae.inspect(_group(L1, "ADBE Root Vectors Group") + ".children[0].children[0].children[0]"
                      ".children.map(c => c._value)") == [2, 3, 100, 25]  # no inner radius on a polygon
    d.add_layer("null", name="Rig")
    with pytest.raises(ToolError, match="Rig is not a shape layer"):
        d.add_shape("rect", layer="Rig")
    for kwargs, msg in [({"shape": "heart"}, "shape must"), ({"shape": "rect", "size": [0, 5]}, "size"),
                        ({"shape": "star", "points": 2}, "points"), ({"shape": "rect", "stroke_width": 0}, "stroke")]:
        with pytest.raises(ToolError, match=msg):
            d.add_shape(**kwargs)


@pytest.mark.parametrize("preset, props, smooth, interp", [
    ("fade_in", {"ADBE Text Opacity": 0}, 100, 6613),
    ("typewriter", {"ADBE Text Opacity": 0}, 0, 6612),
    ("slide_up", {"ADBE Text Opacity": 0, "ADBE Text Position 3D": [0, 60, 0]}, 100, 6613),
    ("blur_in", {"ADBE Text Opacity": 0, "ADBE Text Blur": [30, 30]}, 100, 6613),
])
def test_animate_text(ae, preset, props, smooth, interp):
    d.create_comp("Main", duration=6)
    d.add_layer("text", text="Hello")
    d.set_layer(1, in_point=1)
    out = d.animate_text(1, preset, duration=1.5)
    assert out == {"layer": "Hello", "animator": f"aemcp {preset}", "preset": preset, "from": 1, "to": 2.5, "keys": 2}
    anim = _group(L1, "ADBE Text Properties") + ".children[1].children[0]"
    assert ae.inspect(f"Object.fromEntries({anim}.children[1].children.map(p => [p.matchName, p._value]))") == props
    sel = anim + ".children[0].children[0]"
    assert ae.inspect(f"{sel}.children[0].keys.map(k => [k.time, k.value, k.outType])") == \
        [[1, 0, interp], [2.5, 100, interp]]
    assert ae.inspect(f"{sel}.children[3].children[0]._value") == smooth


def test_animate_text_errors(ae):
    d.create_comp("Main")
    d.add_layer("null", name="Rig")
    with pytest.raises(ToolError, match="Rig is not a text layer"):
        d.animate_text("Rig")
    with pytest.raises(ToolError, match="preset must be"):
        d.animate_text("Rig", "explode")
    with pytest.raises(ToolError, match="duration"):
        d.animate_text("Rig", duration=0)


# --- layer structure and switches ---


def test_precompose(ae):
    d.create_comp("Main")
    for n in ("A", "B", "C"):
        d.add_layer("null", name=n)
    out = d.precompose(["B", 3], "Inner")
    assert out == {"comp": "Main", "precomp": "Inner", "id": 2, "precompLayers": 2, "layer": 2}
    assert [(l["name"], l["type"]) for l in d.comp_info("Main")["layers"]] == [("C", "null"), ("Inner", "precomp")]
    assert [l["name"] for l in d.comp_info("Inner")["layers"]] == ["B", "A"]
    with pytest.raises(ToolError, match="one layer only"):
        d.precompose([1, 2], "X", move_attributes=False)
    assert d.precompose([1], "Solo", move_attributes=False)["precompLayers"] == 1


def test_duplicate_and_move(ae):
    d.create_comp("Main")
    for n in ("A", "B", "C"):
        d.add_layer("null", name=n)  # C, B, A from the top
    d.set_keyframes("A", "opacity", [{"time": 0, "value": 0}, {"time": 1, "value": 100}])
    copy = d.duplicate_layer("A", name="A copy")
    assert (copy["name"], copy["index"]) == ("A copy", 3)
    assert d.get_property("A copy", "opacity")["keys"] == d.get_property("A", "opacity")["keys"]
    d.set_property("A copy", "rotation", 45)
    assert d.get_property("A", "rotation")["value"] == 0  # the copy is independent
    names = lambda: [l["name"] for l in d.comp_info()["layers"]]  # noqa: E731
    assert d.move_layer("A", "top") == {"layer": "A", "index": 1}
    assert names() == ["A", "C", "B", "A copy"]
    d.move_layer("C", "bottom")
    d.move_layer("A copy", "before", other="A")
    d.move_layer("B", "after", other="C")
    assert names() == ["A copy", "A", "C", "B"]
    with pytest.raises(ToolError, match="needs the other layer"):
        d.move_layer("A", "before")
    with pytest.raises(ToolError, match="relative to itself"):
        d.move_layer("A", "after", other="A")
    with pytest.raises(ToolError, match="to must be"):
        d.move_layer("A", "up")


def test_switches_and_track_matte(ae):
    d.create_comp("Main")
    d.add_layer("solid", name="Footage")
    d.add_layer("text", text="MATTE")
    out = d.set_switches("Footage", blending="screen", three_d=True, motion_blur=True, shy=True, matte="luma",
                         matte_layer="MATTE", locked=True)
    assert out == {"layer": "Footage", "threeD": True, "motionBlur": True, "shy": True, "solo": False,
                   "locked": True, "matteLayer": "MATTE"}
    layer = "ae.app.project.item(1).layer(2)"
    assert ae.inspect(f"[{layer}.blendingMode, {layer}.trackMatteType]") == [5219, 5015]
    d.set_switches("Footage", locked=False, matte="none")
    assert ae.inspect(f"{layer}.trackMatteLayer") is None
    for kwargs, msg in [({"blending": "glow"}, "blending must"), ({"matte": "shape"}, "matte must"),
                        ({"matte": "alpha"}, "needs matte_layer")]:
        with pytest.raises(ToolError, match=msg):
            d.set_switches("Footage", **kwargs)
    with pytest.raises(ToolError, match="another layer of the same comp"):
        d.set_switches("Footage", matte="alpha", matte_layer="Footage")
    ae.inspect(f"(delete Object.getPrototypeOf({layer}).setTrackMatte, 1)")
    with pytest.raises(ToolError, match="needs After Effects 2023"):
        d.set_switches("Footage", matte="alpha", matte_layer="MATTE")


def test_markers(ae):
    d.create_comp("Main")
    d.add_layer("null", name="Rig")
    assert d.add_marker(1.5, "Beat", duration=0.5) == {"target": "Main", "time": 1.5, "markers": 1}
    assert d.add_marker(2, "Hit", layer="Rig") == {"target": "Rig", "time": 2, "markers": 1}
    assert ae.inspect("[ae.app.project.item(1).markerProperty.keys[0].value.comment, "
                      "ae.app.project.item(1).markerProperty.keys[0].value.duration]") == ["Beat", 0.5]
    with pytest.raises(ToolError, match="0 or more"):
        d.add_marker(-1)


def test_set_comp(ae):
    d.create_comp("Main", duration=10, frame_rate=25)
    out = d.set_comp(name="Final", width=1080, height=1350, frame_rate=30, bg_color=[1, 1, 1], work_area=[2, 6])
    assert out == {"id": 1, "name": "Final", "width": 1080, "height": 1350, "duration": 10, "frameRate": 30,
                   "workArea": [2, 6], "motionBlur": False, "shutterAngle": 180}
    blur = d.set_comp("Final", motion_blur=True, shutter_angle=270)
    assert (blur["motionBlur"], blur["shutterAngle"]) == (True, 270)
    assert ae.inspect("ae.app.project.item(1).shutterPhase") == -135  # centered on the frame
    assert d.set_comp("Final", work_area=[8, 9.5])["workArea"] == [8, 9.5]  # moving later than the old end
    assert d.set_comp("Final", work_area=[0, 10])["workArea"] == [0, 10]  # longer than 10 - 8: start moves first
    assert d.set_comp("Final", work_area=[0, 1])["workArea"] == [0, 1]
    with pytest.raises(ToolError, match="inside the comp"):
        d.set_comp("Final", work_area=[5, 12])
    for kwargs, msg in [({"width": 2}, "width"), ({"duration": 0}, "duration"), ({"frame_rate": 0}, "frame_rate"),
                        ({"work_area": [1]}, r"\[start, end\]"), ({"shutter_angle": 800}, "shutter_angle")]:
        with pytest.raises(ToolError, match=msg):
            d.set_comp("Final", **kwargs)


def test_render_templates(ae):
    with pytest.raises(ToolError, match="no comp"):
        d.render_templates()
    d.create_comp("Main")
    assert d.render_templates() == {
        "outputModules": ["High Quality", "H.264 - Match Render Settings - 15 Mbps", "Lossless"],
        "renderSettings": ["Best Settings", "Draft Settings", "Multi-Machine Settings"]}
    assert ae.inspect("ae.app.project.renderQueue.numItems") == 0  # the probe item is removed again



# --- expression presets, timing, placement ---


def test_expression_presets(ae):
    d.create_comp("Main")
    d.add_layer("null", name="Rig")
    out = d.expression_preset("Rig", "position", "wiggle", {"freq": 3})
    assert (out["expression"], out["params"], out["error"]) == ("wiggle(3, 30)", {"freq": 3, "amp": 30}, None)
    assert d.expression_preset("Rig", "rotation", "spin", {"speed": 45})["expression"] == "value + time * 45"
    assert d.expression_preset("Rig", "scale", "loop_pingpong")["expression"] == 'loopOut("pingpong")'
    bounce = d.expression_preset("Rig", "position", "bounce", {"decay": 5})["expression"]
    assert "var amp = 0.05, freq = 4, decay = 5;" in bounce and bounce.count("{") == bounce.count("}")
    assert d.get_property("Rig", "position")["expression"] == bounce
    with pytest.raises(ToolError, match="preset must be"):
        d.expression_preset("Rig", "position", "shake")
    with pytest.raises(ToolError, match=r"wiggle takes freq, amp \(got speed\)"):
        d.expression_preset("Rig", "position", "wiggle", {"speed": 2})
    with pytest.raises(ToolError, match="numbers"):
        d.expression_preset("Rig", "position", "wiggle", {"freq": "fast"})


def test_property_tree(ae):
    d.create_comp("Main")
    d.add_layer("text", text="Hi")
    d.set_keyframes(1, "opacity", [{"time": 0, "value": 0}, {"time": 1, "value": 100}])
    out = d.property_tree(1, depth=2)
    groups = {g["matchName"]: g for g in out["properties"]}
    assert groups["ADBE Text Properties"]["children"][1] == {"name": "Animators", "matchName": "ADBE Text Animators",
                                                             "children": 0}
    opacity = next(p for p in groups["ADBE Transform Group"]["children"] if p["name"] == "Opacity")
    assert opacity == {"name": "Opacity", "matchName": "ADBE Opacity", "value": 0, "keys": 2}
    assert groups["ADBE Marker"]["value"] is None
    shallow = d.property_tree(1, depth=1)["properties"]
    assert all(isinstance(r.get("children", 0), int) for r in shallow)
    with pytest.raises(ToolError, match="depth"):
        d.property_tree(1, depth=0)
    assert ae.inspect("ae.undoLog").count("aemcp: property_tree") == 0  # read-only: no undo step


def test_sequence_layers(ae):
    d.create_comp("Main", duration=20)
    for n in ("A", "B", "C"):
        d.add_layer("null", name=n)
    d.set_layer("A", out_point=4)
    d.set_layer("B", in_point=1, out_point=3)
    d.set_layer("C", out_point=5)
    out = d.sequence_layers(["A", "B", "C"], start=1, overlap=0.5)
    assert [(r["layer"], r["inPoint"], r["outPoint"]) for r in out["layers"]] == [
        ("A", 1, 5), ("B", 4.5, 6.5), ("C", 6, 11)]  # durations kept, 0.5 s overlaps
    assert out["end"] == 11
    gap = d.sequence_layers(["C", "A"], overlap=-1)["layers"]
    assert [(r["inPoint"], r["outPoint"]) for r in gap] == [(0, 5), (6, 10)]
    with pytest.raises(ToolError, match="no layers"):
        d.sequence_layers([])


def test_time_remap(ae, tmp_path):
    clip = tmp_path / "shot.mov"
    clip.write_bytes(b"x")
    d.import_file(str(clip))
    d.create_comp("Main", duration=10)
    d.add_layer("item", item="shot.mov")
    out = d.time_remap(1, [{"time": 0, "source": 0}, {"time": 4, "source": 2},
                           {"time": 5, "source": 2, "hold": True}], smooth=False)
    assert out == {"layer": "shot.mov", "keys": 3, "timeRemap": True}
    keys = d.get_property(1, ["ADBE Time Remapping"])["keys"]
    assert keys == [{"time": 0, "value": 0}, {"time": 4, "value": 2}, {"time": 5, "value": 2}]  # AE's own keys gone
    assert ae.inspect("ae.app.project.item(2).layer(1).groups[0].keys.map(k => k.outType)") == [6612, 6612, 6614]
    d.add_layer("solid", name="BG")
    with pytest.raises(ToolError, match="cannot be time remapped"):
        d.time_remap("BG", [{"time": 0, "source": 0}])
    for keys, msg in [([], "no keys"), ([{"time": 0}], "time and source"), ([{"time": 0, "source": -1}], "0 or more")]:
        with pytest.raises(ToolError, match=msg):
            d.time_remap(1, keys)


# a 1920x1080 clip in a 1080x1920 comp: 0.5625 of its width fits across, 1.7778 of its height fills the height
@pytest.mark.parametrize("mode, scale", [("fill", [177.78, 177.78]), ("fit", [56.25, 56.25]),
                                         ("width", [56.25, 56.25]), ("height", [177.78, 177.78]),
                                         ("stretch", [56.25, 177.78])])
def test_fit_to_comp(ae, tmp_path, mode, scale):
    clip = tmp_path / "wide.mov"
    clip.write_bytes(b"x")
    d.import_file(str(clip))  # 1920x1080 in the fake
    d.create_comp("Reel", 1080, 1920)
    d.add_layer("item", item="wide.mov")
    out = d.fit_to_comp(1, mode)
    assert (out["scale"], out["size"]) == (scale, [1920, 1080])
    # centered: anchor in the middle of the footage, position in the middle of the comp
    assert d.get_property(1, "anchor")["value"][:2] == [960, 540]
    assert d.get_property(1, "position")["value"][:2] == [540, 960]


def test_fit_to_comp_text_and_errors(ae):
    d.create_comp("Main", 1920, 1080)
    d.add_layer("text", text="Title")  # 300 x 80 in the fake, drawn from (-150, -60)
    out = d.fit_to_comp(1, "width")
    assert out["scale"] == [640, 640] and d.get_property(1, "anchor")["value"][:2] == [0, -20]
    d.add_layer("null", name="Rig")
    with pytest.raises(ToolError, match="no size to measure"):
        d.fit_to_comp("Rig")
    with pytest.raises(ToolError, match="mode must be"):
        d.fit_to_comp(1, "zoom")


def test_center_anchor(ae):
    d.create_comp("Main", 1920, 1080)
    d.add_layer("text", text="Title")
    d.set_property(1, "scale", [200, 200])
    out = d.center_anchor(1)
    # anchor from (960, 540) to (0, -20): the position moves by the shift times the scale, so nothing moves on screen
    assert out["anchor"] == [0, -20] and out["position"][:2] == [960 - 1920, 540 - 1120]
    d.set_keyframes(1, "position", [{"time": 0, "value": [0, 0]}, {"time": 1, "value": [10, 10]}])
    with pytest.raises(ToolError, match="animated"):
        d.center_anchor(1)


def test_null_control(ae):
    d.create_comp("Main", 1000, 1000)
    d.add_layer("null", name="A")
    d.add_layer("null", name="B")
    d.set_property("A", "position", [100, 200])
    d.set_property("B", "position", [300, 600])
    out = d.null_control(["A", "B"], name="Rig")
    assert out == {"control": "Rig", "index": 1, "children": ["A", "B"]}
    assert d.get_property("Rig", "position")["value"][:2] == [200, 400]
    assert [l["parent"] for l in d.comp_info()["layers"]] == [None, 1, 1]
    with pytest.raises(ToolError, match="no layers"):
        d.null_control([])


# --- presets, footage, project organization ---


def test_apply_preset(ae, tmp_path):
    d.create_comp("Main")
    d.add_layer("solid", name="BG")
    preset = tmp_path / "Glow Pulse.ffx"
    preset.write_bytes(b"x")
    assert d.apply_preset("BG", str(preset))["effects"] == ["Glow"]
    with pytest.raises(ToolError, match=r"\.ffx"):
        d.apply_preset("BG", str(tmp_path / "x.aep"))
    with pytest.raises(ToolError, match="preset not found"):
        d.apply_preset("BG", str(tmp_path / "missing.ffx"))


def test_replace_footage(ae, tmp_path):
    old, new, seq = tmp_path / "v1.mov", tmp_path / "v2.mov", tmp_path / "shot_0001.png"
    for f in (old, new, seq):
        f.write_bytes(b"x")
    d.import_file(str(old))
    assert d.replace_footage("v1.mov", str(new)) == {"id": 1, "name": "v2.mov", "file": str(new)}
    assert d.replace_footage(1, str(seq), sequence=True)["name"] == "shot_[####].png"
    with pytest.raises(ToolError, match="file not found"):
        d.replace_footage(1, str(tmp_path / "gone.mov"))
    d.create_comp("Main")
    with pytest.raises(ToolError, match="Main is not footage"):
        d.replace_footage("Main", str(new))


def test_folders(ae, tmp_path):
    clip = tmp_path / "a.mov"
    clip.write_bytes(b"x")
    d.import_file(str(clip))
    d.create_comp("Main")
    assert d.create_folder("Footage") == {"id": 3, "name": "Footage", "parent": None}
    assert d.create_folder("Day 1", parent="Footage")["parent"] == "Footage"
    assert d.move_items(["a.mov", "Main"], "Day 1") == {"folder": "Day 1", "moved": ["a.mov", "Main"]}
    rows = {r["name"]: r["folder"] for r in d.list_items()}
    assert (rows["a.mov"], rows["Main"], rows["Day 1"]) == ("Day 1", "Day 1", "Footage")
    with pytest.raises(ToolError, match="Main is not a folder"):
        d.move_items(["a.mov"], "Main")
    with pytest.raises(ToolError, match="into itself"):
        d.move_items(["Footage"], "Footage")
    with pytest.raises(ToolError, match="not a folder"):
        d.create_folder("x", parent="Main")


def test_clean_project(ae, tmp_path):
    a, b = tmp_path / "a.mov", tmp_path / "b.mov"
    for f in (a, b):
        f.write_bytes(b"x")
    d.import_file(str(a))
    d.import_file(str(a))  # the same file twice
    d.import_file(str(b))  # never used
    d.create_comp("Main")
    d.add_layer("item", item=2)  # uses the duplicate
    out = d.clean_project()
    assert out == {"consolidated": 1, "removedUnused": 1, "items": [4, 2]}
    assert [r["name"] for r in d.list_items()] == ["a.mov", "Main"]
    assert d.comp_info()["layers"][0]["source"] == "a.mov"  # the layer now uses the kept item
    with pytest.raises(ToolError, match="nothing to do"):
        d.clean_project(False, False)



# --- camera, light, audio ---

CAM = "ae.app.project.item(1).layer(1)"


def _keys_of(layer_expr, match):
    return f"{layer_expr}.groups[0].children.find(p => p.matchName === {json.dumps(match)}).keys.map(k => [k.time, k.value])"


def test_camera_push_in_and_pan(ae):
    d.create_comp("Main", 1920, 1080, duration=10)
    out = d.camera_move("push_in", amount=500, duration=2, start=1)
    assert (out["camera"], out["from"], out["to"]) == ("Camera", 1, 3)
    assert out["position"] == [[960, 540, -1777.8], [960, 540, -1277.8]]  # 500 px toward the point of interest
    assert ae.inspect(_keys_of(CAM, "ADBE Anchor Point")) == []  # the point of interest stays
    assert ae.inspect(f"{CAM}.groups[0].children[1].keys.map(k => k.outType)") == [6613, 6613]  # eased
    d.camera_move("pan_right", amount=300, duration=1, start=5, ease=False, camera="Camera")
    assert ae.inspect(_keys_of(CAM, "ADBE Anchor Point")) == [[5, [960, 540, 0]], [6, [1260, 540, 0]]]
    pos = ae.inspect(_keys_of(CAM, "ADBE Position"))
    assert pos[-2:] == [[5, [960, 540, -1277.8]], [6, [1260, 540, -1277.8]]]  # continues from where the push ended
    pulled = d.camera_move("pull_out", amount=100, start=7, duration=1, camera="Camera")
    assert pulled["position"][1][2] == pytest.approx(-1377.8)
    crane = d.camera_move("crane_up", amount=200, start=8.5, duration=1, camera="Camera")
    assert crane["position"][1][1] == 340  # up is -y


def test_camera_orbit(ae):
    d.create_comp("Main", 1920, 1080, duration=10)
    out = d.camera_move("orbit", duration=4)
    assert out == {"camera": "Camera", "move": "orbit", "rig": "Camera Orbit", "from": 0, "to": 4, "degrees": 90}
    rig = "ae.app.project.item(1).layer(1)"
    assert ae.inspect(f"[{rig}.threeDLayer, {rig}.groups[0].children[1]._value]") == [True, [960, 540, 0]]
    assert ae.inspect(_keys_of(rig, "ADBE Rotate Y")) == [[0, 0], [4, 90]]
    assert [l["parent"] for l in d.comp_info()["layers"]] == [None, 1]  # the camera hangs on the rig


def test_camera_errors(ae):
    d.create_comp("Main")
    d.add_layer("null", name="Rig")
    with pytest.raises(ToolError, match="Rig is not a camera"):
        d.camera_move("push_in", camera="Rig")
    for kwargs, msg in [({"move": "zoom"}, "move must be"), ({"move": "orbit", "duration": 0}, "duration"),
                        ({"move": "pan_left", "amount": -5}, "amount")]:
        with pytest.raises(ToolError, match=msg):
            d.camera_move(**kwargs)
    d.add_layer("camera")
    ae.inspect("(ae.app.project.item(1).layer(1).groups[0].children[1]._value = [960, 540, 0], 1)")
    with pytest.raises(ToolError, match="no direction"):
        d.camera_move("push_in", camera="Camera")


def test_set_light(ae):
    d.create_comp("Main")
    d.add_layer("light", name="Key")
    out = d.set_light("Key", type="spot", intensity=150, color=[1, 0.9, 0.8], cone_angle=60, cone_feather=40,
                      shadows=True)
    assert out == {"light": "Key", "set": {"intensity": 150, "color": [1, 0.9, 0.8, 1], "coneAngle": 60,
                                           "coneFeather": 40, "shadows": 1}}
    with pytest.raises(ToolError, match="this light type has no coneAngle"):
        d.set_light("Key", type="point", cone_angle=30)
    with pytest.raises(ToolError, match="no shadows"):
        d.set_light("Key", type="ambient", shadows=True)
    d.add_layer("null", name="Rig")
    with pytest.raises(ToolError, match="Rig is not a light"):
        d.set_light("Rig", intensity=10)
    for kwargs, msg in [({"type": "neon"}, "type must be"), ({"cone_angle": 0}, "cone_angle"),
                        ({"cone_feather": 120}, "cone_feather")]:
        with pytest.raises(ToolError, match=msg):
            d.set_light("Key", **kwargs)


def _music(ae, tmp_path, name="song.wav"):
    f = tmp_path / name
    f.write_bytes(b"x")
    d.import_file(str(f))
    d.create_comp("Main", duration=10)
    d.add_layer("item", item=name)  # 5 s in the fake


def test_audio_fade(ae, tmp_path):
    _music(ae, tmp_path)
    out = d.audio_fade(1, fade_in=1, fade_out=2, level_db=-6)
    assert out == {"layer": "song.wav", "level": -6, "fadeIn": 1, "fadeOut": 2, "keys": 4}
    assert d.get_property(1, ["ADBE Audio Group", "ADBE Audio Levels"])["keys"] == [
        {"time": 0, "value": [-48, -48]}, {"time": 1, "value": [-6, -6]}, {"time": 8, "value": [-6, -6]},
        {"time": 10, "value": [-48, -48]}]
    assert d.audio_fade(1, level_db=3)["keys"] == 0  # a level without fades: a static value
    assert d.get_property(1, ["ADBE Audio Group", "ADBE Audio Levels"])["value"] == [3, 3]
    with pytest.raises(ToolError, match="longer than the layer"):
        d.audio_fade(1, fade_in=6, fade_out=6)
    d.add_layer("solid", name="BG")
    with pytest.raises(ToolError, match="BG has no audio"):
        d.audio_fade("BG", fade_in=1)
    for kwargs, msg in [({"fade_in": -1}, "fades"), ({"level_db": -60}, "level_db")]:
        with pytest.raises(ToolError, match=msg):
            d.audio_fade(1, **kwargs)


def test_audio_react(ae, tmp_path):
    _music(ae, tmp_path)
    d.add_layer("shape", name="Logo")
    out = d.audio_react("song.wav", "Logo", "scale", amount=2, name="Beat")
    assert out["amplitude"] == "Beat" and out["error"] is None
    assert out["expression"] == ('var a = thisComp.layer("Beat").effect("Both Channels")("Slider") * 2;\n'
                                 "var v = value;\nv[0] += a;\nv[1] += a;\nv")  # x and y only, however many values
    assert [l["name"] for l in d.comp_info()["layers"]] == ["Beat", "Logo", "song.wav"]
    assert ae.inspect("ae.app.project.item(2).layer(3).selected") is True  # the command ran on the audio layer
    rot = d.audio_react("song.wav", "Logo", "rotation", amount=5, name="Beat 2")
    assert rot["expression"].endswith("value + a")
    with pytest.raises(ToolError, match="Logo has no audio"):
        d.audio_react("Logo", "Logo")
    ae.inspect("(ae.app.findMenuCommandId = () => 0, 1)")
    with pytest.raises(ToolError, match="not found in the menus"):
        d.audio_react("song.wav", "Logo")


def test_audio_react_command_does_nothing(ae, tmp_path):
    _music(ae, tmp_path)
    ae.inspect("(ae.app.executeCommand = () => {}, 1)")
    with pytest.raises(ToolError, match="added no layer"):
        d.audio_react("song.wav", "song.wav", "opacity")


# --- captions ---

SRT = """﻿1
00:00:01,000 --> 00:00:02,500
مرحبًا بكم

2
00:00:03,000 --> 00:00:05,000 X1:0 X2:100
<i>Second</i> line
and more

3
00:00:06,000 --> 00:00:05,000
backwards: skipped
"""


def test_parse_srt():
    cues = d._parse_srt(SRT)
    assert cues == [{"start": 1, "end": 2.5, "text": "مرحبًا بكم"},
                    {"start": 3, "end": 5, "text": "Second line\rand more"}]
    with pytest.raises(ToolError, match="not an SRT time"):
        d._parse_srt("1\n00:00:1 --> 00:00:02,000\nx")


def test_captions_from_srt(ae, tmp_path):
    srt = tmp_path / "ar.srt"
    srt.write_text(SRT, encoding="utf-8")
    d.create_comp("Main", 1080, 1920, duration=10)
    out = d.captions_from_srt(str(srt), size=72, color=[1, 1, 0], y=0.8, offset=0.5)
    assert out == {"comp": "Main", "captions": 2, "first": "Caption 1", "last": "Caption 2"}
    layers = d.comp_info()["layers"]
    assert [(l["name"], l["inPoint"], l["outPoint"]) for l in layers] == [("Caption 2", 3.5, 5.5),
                                                                           ("Caption 1", 1.5, 3)]
    text = d.get_property("Caption 1", "text")["value"]
    assert (text["text"], text["fontSize"], text["fillColor"]) == ("مرحبًا بكم", 72, [1, 1, 0])
    assert d.get_property("Caption 1", "position")["value"][:2] == [540, 1536]
    empty = tmp_path / "empty.srt"
    empty.write_text("nothing here", encoding="utf-8")
    with pytest.raises(ToolError, match="no subtitles"):
        d.captions_from_srt(str(empty))
    with pytest.raises(ToolError, match="cannot read"):
        d.captions_from_srt(str(tmp_path / "missing.srt"))
    with pytest.raises(ToolError, match="before 0"):
        d.captions_from_srt(str(srt), offset=-2)


# --- background rendering ---


def test_render_background(ae, tmp_path, monkeypatch):
    exe = tmp_path / "aerender"
    exe.write_text("#!/bin/sh\necho \"PROGRESS: rendering $2\"\necho 'Finished composition'\n")
    exe.chmod(0o755)
    monkeypatch.setenv("AE_RENDER", str(exe))
    d.create_comp("Main")
    with pytest.raises(ToolError, match="never saved"):
        d.render_background()
    d.save_project(str(tmp_path / "p.aep"))
    job = d.render_background()
    assert job["project"] == str(tmp_path / "p.aep")
    d._JOBS[job["job"]][0].wait(timeout=10)
    st = d.render_background_status(job["job"])
    assert (st["state"], st["exit_code"]) == ("done", 0)
    assert st["log_tail"] == [f"PROGRESS: rendering {tmp_path / 'p.aep'}", "Finished composition"]
    with pytest.raises(ToolError, match="unknown render job"):
        d.render_background_status(1)


def test_render_background_failures(ae, tmp_path, monkeypatch):
    monkeypatch.setenv("AE_RENDER", str(tmp_path / "nowhere"))
    with pytest.raises(ToolError, match="aerender not found"):
        d.render_background()
    exe = tmp_path / "aerender"
    exe.write_text("#!/bin/sh\necho 'aerender ERROR: No comps to render'\n")  # exits 0 all the same
    exe.chmod(0o755)
    monkeypatch.setenv("AE_RENDER", str(exe))
    d.create_comp("Main")
    d.save_project(str(tmp_path / "p.aep"))
    job = d.render_background()
    d._JOBS[job["job"]][0].wait(timeout=10)
    assert d.render_background_status(job["job"])["state"] == "failed"


# --- Essential Graphics ---


def test_mogrt(ae, tmp_path):
    d.create_comp("Lower Third")
    d.add_layer("text", text="Name")
    with pytest.raises(ToolError, match="add properties to Essential Graphics first"):
        d.export_mogrt(str(tmp_path / "lt.mogrt"))
    assert d.mogrt_add_property(1, "text", name="Name") == {"comp": "Lower Third", "layer": "Name",
                                                             "property": "Source Text", "name": "Name"}
    d.mogrt_add_property(1, "opacity")
    out = d.export_mogrt(str(tmp_path / "lt.mogrt"), name="LT Clean")
    assert out["template"] == "LT Clean"
    assert json.loads((tmp_path / "lt.mogrt").read_text()) == {"name": "LT Clean",
                                                               "props": [["Source Text", "Name"], ["Opacity", "Opacity"]]}
    with pytest.raises(ToolError, match=r"\.mogrt"):
        d.export_mogrt(str(tmp_path / "lt.aep"))
    ae.inspect("(delete Object.getPrototypeOf(ae.app.project.item(1).layer(1).groups[0].children[0])"
               ".addToMotionGraphicsTemplateAs, 1)")
    with pytest.raises(ToolError, match="needs After Effects 2018"):
        d.mogrt_add_property(1, "text")



# --- keyframe work, editing, variants, footage ---


def test_bake_expression(ae):
    d.create_comp("Main", duration=2, frame_rate=10)
    d.add_layer("null", name="Rig")
    d.set_layer("Rig", in_point=0.5, out_point=1)
    d.set_expression("Rig", "rotation", "wiggle(2, 30)")
    out = d.bake_expression("Rig", "rotation", step=2)
    assert out == {"property": "Rotation", "keys": 3, "from": 0.5, "to": 0.9, "expression": "disabled (kept, not deleted)"}
    got = d.get_property("Rig", "rotation")
    # the fake evaluates expressions as value + time: frames 0.5, 0.7, 0.9 of a 0 rotation
    assert [(k["time"], k["value"]) for k in got["keys"]] == [(0.5, 0.5), (0.7, 0.7), (0.9, 0.9)]
    assert got["expression"] is None  # switched off
    with pytest.raises(ToolError, match="no expression to bake"):
        d.bake_expression("Rig", "rotation")
    d.set_expression("Rig", "opacity", "time")
    with pytest.raises(ToolError, match="end must be after start"):
        d.bake_expression("Rig", "opacity", start=1, end=1)
    with pytest.raises(ToolError, match="step"):
        d.bake_expression("Rig", "opacity", step=0)


def test_ease_keyframes(ae):
    d.create_comp("Main")
    d.add_layer("null", name="Rig")
    d.set_keyframes("Rig", "scale", [{"time": t, "value": [v, v], "ease": "linear"} for t, v in ((0, 0), (1, 100), (2, 80))])
    assert d.ease_keyframes("Rig", "scale", "ease", influence=80) == {"property": "Scale", "keys": 3, "ease": "ease"}
    keys = "ae.app.project.item(1).layer(1).groups[0].children.find(p => p.name === 'Scale').keys"
    assert ae.inspect(f"{keys}.map(k => [k.outType, k.outEase.length, k.outEase[0].influence])") == [[6613, 3, 80]] * 3
    d.ease_keyframes("Rig", "scale", "hold")
    assert ae.inspect(f"{keys}.map(k => k.outType)") == [6614] * 3
    with pytest.raises(ToolError, match="no keyframes"):
        d.ease_keyframes("Rig", "opacity")
    for kwargs, msg in [({"ease": "bounce"}, "ease must be"), ({"influence": 0}, "influence")]:
        with pytest.raises(ToolError, match=msg):
            d.ease_keyframes("Rig", "scale", **kwargs)


def test_copy_keyframes(ae):
    d.create_comp("Main")
    for n in ("A", "B", "C"):
        d.add_layer("null", name=n)
    d.set_keyframes("A", "opacity", [{"time": 0, "value": 0, "ease": "linear"},
                                      {"time": 1, "value": 100, "ease": "ease", "influence": 60}])
    d.set_keyframes("C", "opacity", [{"time": 5, "value": 50}])  # replaced
    out = d.copy_keyframes("A", "opacity", ["B", "C"], offset=0.25)
    assert out == {"property": "Opacity", "from": "A", "to": ["B", "C"], "keys": 2}
    assert [k["time"] for k in d.get_property("B", "opacity")["keys"]] == [0.25, 1.25]
    assert [k["time"] for k in d.get_property("C", "opacity")["keys"]] == [0.5, 1.5]  # cascades
    c = "ae.app.project.item(1).layer(1).groups[0].children.find(p => p.name === 'Opacity').keys"
    assert ae.inspect(f"{c}.map(k => k.outType)") == [6612, 6613]
    assert ae.inspect(f"{c}[1].inEase[0].influence") == 60
    with pytest.raises(ToolError, match="own target"):
        d.copy_keyframes("A", "opacity", ["A"])
    with pytest.raises(ToolError, match="no keyframes to copy"):
        d.copy_keyframes("B", "rotation", ["C"])
    with pytest.raises(ToolError, match="no targets"):
        d.copy_keyframes("A", "opacity", [])


def test_stagger_layers(ae):
    d.create_comp("Main", duration=10)
    for n in ("A", "B", "C"):
        d.add_layer("null", name=n)
    d.set_layer("B", start_time=3)
    d.set_keyframes("C", "opacity", [{"time": 0, "value": 0}, {"time": 1, "value": 100}])
    out = d.stagger_layers(["A", "B", "C"], offset=0.2, start=1)
    assert [(r["layer"], r["inPoint"]) for r in out["layers"]] == [("A", 1), ("B", 1.2), ("C", 1.4)]
    assert d.stagger_layers(["C", "A"], offset=0.5)["layers"] == [{"layer": "C", "inPoint": 1.4},
                                                                  {"layer": "A", "inPoint": 1.9}]
    with pytest.raises(ToolError, match="no layers"):
        d.stagger_layers([])
    d.set_layer("B", in_point=4)  # trimmed: its in point is past its start time
    assert d.stagger_layers(["A", "B"], offset=1, start=6)["layers"] == [{"layer": "A", "inPoint": 6},
                                                                         {"layer": "B", "inPoint": 7}]


def test_split_layer(ae):
    d.create_comp("Main", duration=10)
    d.add_layer("solid", name="Shot")
    d.set_keyframes("Shot", "opacity", [{"time": 0, "value": 0}, {"time": 8, "value": 100}])
    out = d.split_layer("Shot", 4, name="Shot B")
    assert out == {"first": {"index": 2, "name": "Shot", "outPoint": 4}, "second": {"index": 1, "name": "Shot B",
                                                                                    "inPoint": 4}}
    assert d.get_property("Shot B", "opacity")["keys"] == d.get_property("Shot", "opacity")["keys"]
    with pytest.raises(ToolError, match=r"inside the layer \(0-4 s\)"):
        d.split_layer("Shot", 4)


def test_trim_comp(ae):
    d.create_comp("Main", duration=20)
    d.add_layer("null", name="A")
    d.add_layer("null", name="B")
    d.set_layer("A", start_time=3)          # 3-23: out past the comp end
    d.set_layer("B", start_time=2, out_point=8)
    out = d.trim_comp()
    assert out == {"comp": "Main", "duration": 18, "shiftedBy": 2}
    assert [(l["name"], l["inPoint"]) for l in d.comp_info()["layers"]] == [("B", 0), ("A", 1)]
    d.set_comp(work_area=[2, 5])
    assert d.trim_comp(to="work_area") == {"comp": "Main", "duration": 3, "shiftedBy": 2}
    d.create_comp("Empty")
    with pytest.raises(ToolError, match="no layers to trim"):
        d.trim_comp(comp="Empty")
    with pytest.raises(ToolError, match="to must be"):
        d.trim_comp(to="markers")


def test_make_variants(ae, tmp_path):
    d.create_comp("Card")
    d.add_layer("text", text="NAME", name="Name")
    d.add_layer("text", text="ROLE", name="Role")
    d.add_layer("null", name="Rig")
    d.set_layer("Name", parent="Rig")
    out = d.make_variants([{"name": "Card Sara", "texts": {"Name": "سارة", "Role": "Editor"}},
                           {"name": "Card Omar", "texts": {"Name": "Omar"}}], comp="Card",
                          output_dir=str(tmp_path / "out"), template="High Quality")
    assert out == {"from": "Card", "variants": [{"comp": "Card Sara", "id": 2, "queued": True},
                                                {"comp": "Card Omar", "id": 3, "queued": True}]}
    assert d.get_property("Name", "text", comp="Card Sara")["value"]["text"] == "سارة"
    assert d.get_property("Role", "text", comp="Card Omar")["value"]["text"] == "ROLE"  # untouched
    assert d.get_property("Name", "text", comp="Card")["value"]["text"] == "NAME"  # the template stays
    assert d.comp_info("Card Sara")["layers"][2]["parent"] == 1  # Name (3) stays on the copy's Rig (1)
    assert ae.inspect("ae.app.project.renderQueue._items.map(r => r._om.file.fsName)") == [
        str(tmp_path / "out" / "Card Sara"), str(tmp_path / "out" / "Card Omar")]
    assert d.make_variants([{"name": "No render"}], comp="Card")["variants"][0]["queued"] is False
    with pytest.raises(ToolError, match="Rig in Card is not a text layer"):
        d.make_variants([{"name": "X", "texts": {"Rig": "x"}}], comp="Card")
    for rows, msg in [([], "no rows"), ([{"texts": {}}], "needs a"), ([{"name": "A"}, {"name": "A"}], "unique"),
                      ([{"name": "a/b"}], "file names"), ([{"name": "A", "texts": ["x"]}], "maps text")]:
        with pytest.raises(ToolError, match=msg):
            d.make_variants(rows, comp="Card")


def test_import_layered(ae, tmp_path):
    psd = tmp_path / "Poster.psd"
    psd.write_bytes(b"8BPS")
    out = d.import_layered(str(psd))
    assert out == {"id": 2, "name": "Poster", "type": "comp", "layers": 2}
    assert ae.inspect("ae.app.project.item(1).name") == "Poster Layers"
    mov = tmp_path / "clip.mov"
    mov.write_bytes(b"x")
    with pytest.raises(ToolError, match="cannot import clip.mov as comp"):
        d.import_layered(str(mov), "comp")
    assert d.import_layered(str(mov), "footage")["type"] == "footage"
    with pytest.raises(ToolError, match="mode must be"):
        d.import_layered(str(psd), "layers")
    with pytest.raises(ToolError, match="file not found"):
        d.import_layered(str(tmp_path / "gone.psd"))


def test_find_missing_footage(ae, tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    for n in ("a.mov", "b.mov", "c.mov"):
        (card / n).write_bytes(b"x")
        d.import_file(str(card / n))
    assert d.find_missing_footage() == {"missing": []}
    assert "aemcp: missing_footage" not in ae.inspect("ae.undoLog")  # a lookup adds no undo step
    for n in ("a.mov", "b.mov"):
        (card / n).unlink()
    (tmp_path / "backup" / "day1").mkdir(parents=True)
    (tmp_path / "backup" / "day1" / "A.MOV").write_bytes(b"x")  # found by name, any case
    assert [m["name"] for m in d.find_missing_footage()["missing"]] == ["a.mov", "b.mov"]
    out = d.find_missing_footage(search=str(tmp_path / "backup"))
    assert out["relinked"] == [{"id": 1, "name": "A.MOV", "missing": False}]
    assert (out["still_missing"], out["files_searched"]) == (["b.mov"], 1)
    with pytest.raises(ToolError, match="folder not found"):
        d.find_missing_footage(search=str(tmp_path / "nowhere"))


# --- motion ---

POS = "ae.app.project.item(1).layer(1).groups[0].children[1]"  # a layer's Position


def test_motion_path(ae):
    d.create_comp("Main", duration=10)
    d.add_layer("solid", name="Plane")
    out = d.motion_path("Plane", [[0, 500], [600, 200], [1200, 500], [1800, 200]], duration=3, start=1,
                        auto_orient=True)
    assert out == {"layer": "Plane", "keys": 4, "from": 1, "to": 4, "smooth": True, "constantSpeed": False,
                   "autoOrient": True}
    keys = ae.inspect(f"{POS}.keys")
    assert [k["time"] for k in keys] == [1, 2, 3, 4]
    assert keys[1]["value"] == [600, 200, 0]  # 2D points keep the layer's z
    assert [k["spatialAuto"] for k in keys] == [True] * 4  # curved through the points
    assert [k["outType"] for k in keys] == [6613, 6613, 6613, 6613]  # ends eased, inner keys Bezier
    assert [k.get("temporalContinuous", False) for k in keys] == [False, True, True, False]  # no stop inside
    assert ae.inspect("ae.app.project.item(1).layer(1).autoOrient") == 4213
    # straight lines at an even speed: flat tangents, inner keys rove, linear ends; old keys replaced
    d.motion_path("Plane", [[0, 0, 0], [100, 0, 50], [100, 100, 50]], times=[0, 1, 5], smooth=False,
                  constant_speed=True)
    keys = ae.inspect(f"{POS}.keys")
    assert [k["time"] for k in keys] == [0, 1, 5]
    assert keys[0]["tangents"] == [[0, 0, 0], [0, 0, 0]]
    assert [k.get("roving", False) for k in keys] == [False, True, False]
    assert (keys[0]["outType"], keys[2]["inType"]) == (6612, 6612)
    for kwargs, msg in [({"points": [[0, 0]]}, "at least 2 points"), ({"points": [[0], [1]]}, "at least 2 points"),
                        ({"times": [1, 1]}, "increasing"), ({"times": [0]}, "one increasing time"),
                        ({"duration": 0}, "duration")]:
        with pytest.raises(ToolError, match=msg):
            d.motion_path("Plane", **{"points": [[0, 0], [9, 9]], **kwargs})


def test_bezier_ease(ae):
    d.create_comp("Main", duration=10)
    d.add_layer("solid", name="S")
    d.set_keyframes("S", "rotation", [{"time": 0, "value": 0}, {"time": 2, "value": 100}, {"time": 4, "value": 0}])
    out = d.bezier_ease("S", "rotation", "ease")  # CSS ease: cubic-bezier(0.25, 0.1, 0.25, 1)
    assert out == {"property": "Rotation", "segments": 2, "keys": [1, 3], "curve": "ease"}
    keys = ae.inspect("ae.app.project.item(1).layer(1).groups[0].children[5].keys"
                      ".map(k => [k.inEase && Array.from(k.inEase, e => [e.speed, e.influence]),"
                      " Array.from(k.outEase, e => [e.speed, e.influence])])")
    # out of key 1: influence 25 %, speed 0.1 / 0.25 x 50 per second; into key 2: 75 %, speed 0
    assert keys[0][1] == [[20, 25]]
    assert keys[1] == [[[0, 75]], [[-20, 25]]]  # falling segment: signed speed
    assert keys[2][0] == [[0, 75]]
    # multi-dimensional: one ease per dimension, each with its own signed speed
    d.set_keyframes("S", "scale", [{"time": 0, "value": [100, 100]}, {"time": 1, "value": [200, 50]}])
    d.bezier_ease("S", "scale", [0.5, 0.5, 0.5, 0.5])
    scale = ae.inspect("Array.from(ae.app.project.item(1).layer(1).groups[0].children[2].keys[0].outEase,"
                       " e => [e.speed, e.influence])")
    assert scale == [[100, 50], [-50, 50], [0, 50]]
    # spatial: one ease, the speed along the path; overshoot refused there
    d.set_keyframes("S", "position", [{"time": 0, "value": [0, 0]}, {"time": 1, "value": [300, 400]}])
    d.bezier_ease("S", "position", [0.5, 1, 0.5, 1])
    assert ae.inspect(f"Array.from({POS}.keys[0].outEase, e => [e.speed, e.influence])") == [[1000, 50]]
    with pytest.raises(ToolError, match="overshoot"):
        d.bezier_ease("S", "position", "ease_out_back")
    d.bezier_ease("S", "rotation", "ease_out_back", keys=[1, 2])  # fine on a 1D property, one segment
    # x of 0 or 1 would make an influence of 0: kept at the 0.1 % minimum
    d.bezier_ease("S", "rotation", "ease_out", keys=[2, 3])
    assert ae.inspect("ae.app.project.item(1).layer(1).groups[0].children[5].keys[1].outEase[0].influence") == 0.1
    for kwargs, msg in [({"curve": "springy"}, "curve must be"), ({"curve": [0.5, 0, 1.5, 1]}, "x1 and x2 0-1"),
                        ({"curve": [1, 2]}, r"\[x1, y1, x2, y2\]"), ({"keys": [1]}, r"\[first, last\]")]:
        with pytest.raises(ToolError, match=msg):
            d.bezier_ease("S", "rotation", **kwargs)
    with pytest.raises(ToolError, match="within 1-3"):
        d.bezier_ease("S", "rotation", keys=[2, 5])
    with pytest.raises(ToolError, match="at least 2 keyframes"):
        d.bezier_ease("S", "opacity")


# --- tracking ---


def _png(w, h, gray, filters=(0, 1, 2, 3, 4)):
    """An 8-bit RGBA PNG, row y filtered with filters[y % len(filters)]."""
    import struct
    import zlib
    rows, prev = [], bytes(w * 4)
    for y in range(h):
        line = bytes(v for x in range(w) for v in (gray(x, y),) * 3 + (255,))
        f, out = filters[y % len(filters)], bytearray([filters[y % len(filters)]])
        for i in range(w * 4):
            a, b, c = (line[i - 4] if i >= 4 else 0), prev[i], (prev[i - 4] if i >= 4 else 0)
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = [0, a, b, (a + b) // 2, a if pa <= pb and pa <= pc else b if pb <= pc else c][f]
            out.append((line[i] - pred) & 255)
        rows.append(bytes(out))
        prev = line
    chunk = lambda k, data: struct.pack(">I", len(data)) + k + data + struct.pack(">I", zlib.crc32(k + data))  # noqa
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def test_png_gray_decodes_every_filter():
    gray = lambda x, y: (x * 37 + y * 11) % 256  # noqa: E731
    w, rows = d._png_gray(_png(23, 11, gray))
    assert w == 23 and len(rows) == 11
    assert all(rows[y][x] == pytest.approx(gray(x, y), abs=1e-6) for y in range(11) for x in range(23))
    with pytest.raises(ToolError, match="8-bit RGB or RGBA"):
        d._png_gray(_png(4, 4, gray)[:24] + bytes([16]) + _png(4, 4, gray)[25:])  # a 16-bit header


def test_match_finds_a_shifted_feature():
    # a 9 x 9 pattern hidden in a 29 x 29 window, 3 px right and 2 px up of center
    pat = lambda x, y: (x * 53 + y * 97 + x * y * 7) % 200 + 20  # noqa: E731
    feature, radius = 9, 10
    tmpl = [pat(x, y) for y in range(feature) for x in range(feature)]
    img = [[5.0] * 29 for _ in range(29)]
    for y in range(feature):
        for x in range(feature):
            img[radius - 2 + y][radius + 3 + x] = pat(x, y)
    dx, dy, score = d._match(img, tmpl, feature, radius)
    assert (round(dx), round(dy)) == (3, -2) and score > 0.99


def test_track_point_and_attach(ae, tmp_path):
    clip = tmp_path / "moving.mov"
    clip.write_bytes(b"x")
    d.import_file(str(clip))  # the fake shows a square moving from (100, 150) at 60, 20 px per second
    d.create_comp("Main", 1920, 1080, duration=5, frame_rate=25)
    d.add_layer("item", item="moving.mov")
    out = d.track_point("moving.mov", [100, 150], start=0, end=1, feature=25, search=12)
    assert (out["tracker"], out["point"], out["keys"], out["from"], out["to"]) == \
        ("aemcp track", "Track Point 1", 26, 0, 1)
    # sharp edges between whole pixels match a little less well: 0.9 there, 1 where the square lands on the grid
    assert out["min_confidence"] > 0.85 and out["lost_frames"] == 0
    assert out["end"] == [pytest.approx(160, abs=0.25), pytest.approx(170, abs=0.25)]
    pt = ("ae.app.project.item(2).layer(1).property('ADBE MTrackers').property(1).property(1)")
    attach = ae.inspect(f"{pt}.property('ADBE MTracker Pt Attach Pt').keys.map(k => [k.time, k.value])")
    for t, (x, y) in attach:
        assert (x, y) == (pytest.approx(100 + 60 * t, abs=0.25), pytest.approx(150 + 20 * t, abs=0.25))
    assert ae.inspect(f"{pt}.property('ADBE MTracker Pt Feature Size')._value") == [25, 25]
    assert "aemcp track window" not in [i["name"] for i in d.list_items()]  # the window comp is removed
    # tracking again replaces the point's keys
    assert d.track_point("moving.mov", [100, 150], start=0, end=0.2, feature=25, search=12)["keys"] == 6
    assert ae.inspect(f"{pt}.property('ADBE MTracker Pt Attach Pt').keys.length") == 6

    d.add_layer("text", text="Label")
    follow = d.attach_to_track("Label", "moving.mov")
    assert follow["follows"] == ["moving.mov", "aemcp track", "Track Point 1"]
    expr = follow["expression"]
    assert 'L.motionTracker("aemcp track")("Track Point 1").attachPoint' in expr
    assert "if (hasParent) p = parent.fromComp(p);" in expr and expr.endswith("[p[0], p[1]]")
    assert follow["error"] is None
    offset = d.attach_to_track("Label", "moving.mov", tracker=1, point="Track Point 1", keep_offset=True)
    assert ".attachPoint.valueAtTime(0)" in offset["expression"]  # aligned at the track's first key
    d.set_switches("Label", three_d=True)
    assert d.attach_to_track("Label", "moving.mov")["expression"].endswith("[p[0], p[1], value[2]]")


def test_track_errors(ae, tmp_path):
    d.create_comp("Main", duration=5)
    d.add_layer("solid", name="Plate")
    with pytest.raises(ToolError, match="no footage to track"):
        d.track_point("Plate", [10, 10])
    for kwargs, msg in [({"point": [1]}, r"\[x, y\]"), ({"feature": 30}, "odd"), ({"search": 2}, "search")]:
        with pytest.raises(ToolError, match=msg):
            d.track_point("Plate", **{"point": [10, 10], **kwargs})
    clip = tmp_path / "moving.mov"
    clip.write_bytes(b"x")
    d.import_file(str(clip))
    d.add_layer("item", item="moving.mov")
    with pytest.raises(ToolError, match="end must come after start"):
        d.track_point("moving.mov", [100, 150], start=2, end=1)
    d.time_remap("moving.mov", [{"time": 0, "source": 0}, {"time": 1, "source": 2}])
    with pytest.raises(ToolError, match="precompose"):
        d.track_point("moving.mov", [100, 150])
    assert "aemcp track window" not in [i["name"] for i in d.list_items()]
    d.add_layer("text", text="Label")
    with pytest.raises(ToolError, match="has no tracks"):
        d.attach_to_track("Label", "Plate")
    with pytest.raises(ToolError, match="its own track"):
        d.attach_to_track("Plate", "Plate")


def test_attach_errors(ae, tmp_path):
    clip = tmp_path / "moving.mov"
    clip.write_bytes(b"x")
    d.import_file(str(clip))
    d.create_comp("Main", duration=5, frame_rate=25)
    d.add_layer("item", item="moving.mov")
    d.track_point("moving.mov", [100, 150], start=0, end=0.1, feature=25, search=12)
    d.add_layer("text", text="Label")
    with pytest.raises(ToolError, match="no tracker Nope"):
        d.attach_to_track("Label", "moving.mov", tracker="Nope")
    with pytest.raises(ToolError, match="no track point 3"):
        d.attach_to_track("Label", "moving.mov", point=3)
    d.set_keyframes("Label", "position", [{"time": 0, "value": [0, 0]}])
    with pytest.raises(ToolError, match="position is animated"):
        d.attach_to_track("Label", "moving.mov")


# --- 3D ---


def test_set_camera(ae):
    d.create_comp("Main", 1920, 1080)
    d.add_layer("camera", name="Cam")
    # in front of the center, facing it straight on (After Effects 26 alone would put it at x = y = 0)
    assert ae.inspect("ae.app.project.item(1).layer(1).groups[0].children[1]._value") == [960, 540, -1777.8]
    d.add_layer("text", text="Hero")
    out = d.set_camera("Cam", focal_length=50, depth_of_field=True, f_stop=2.8, blur_level=150, focus_on="Hero")
    assert out["set"] == {"zoom": pytest.approx(2666.6667), "depthOfField": 1,
                          "aperture": pytest.approx(952.381, abs=1e-3), "blurLevel": 150, "focusOn": "Hero"}
    focus = ae.inspect("ae.app.project.item(1).layer(2).property('ADBE Camera Options Group')"
                       ".property('ADBE Camera Focus Distance')")
    assert focus["expressionEnabled"] and 'thisComp.layer("Hero")' in focus["expression"]
    assert "toWorldVec([0, 0, 1])" in focus["expression"]
    fixed = d.set_camera("Cam", focus_distance=900)  # a fixed distance replaces the autofocus
    assert fixed["set"] == {"focusDistance": 900}
    assert ae.inspect("ae.app.project.item(1).layer(2).property('ADBE Camera Options Group')"
                      ".property('ADBE Camera Focus Distance').expression") == ""
    with pytest.raises(ToolError, match="Hero is not a camera"):
        d.set_camera("Hero", focal_length=35)
    for kwargs, msg in [({"focal_length": 0}, "focal_length"), ({"focus_distance": 5, "focus_on": "Hero"}, "not both"),
                        ({"focus_distance": 0}, "focus_distance"), ({"f_stop": 100}, "f_stop"),
                        ({"blur_level": -1}, "blur_level")]:
        with pytest.raises(ToolError, match=msg):
            d.set_camera("Cam", **kwargs)


def test_set_3d_layer(ae):
    d.create_comp("Main")
    d.add_layer("text", text="LOGO")
    out = d.set_3d_layer("LOGO", extrusion=40, bevel=4, bevel_style="convex", casts_shadows=True, metal=80,
                         specular=70)
    assert out == {"layer": "LOGO", "threeD": True, "renderer": "ADBE Calder",
                   "set": {"extrusion": 40, "bevel": 4, "castsShadows": 1, "specular": 70, "metal": 80}}
    assert ae.inspect("ae.app.project.item(1).layer(1).property('ADBE Extrsn Options Group')"
                      ".property('ADBE Bevel Styles')._value") == 4
    # material only: no renderer change
    d.create_comp("Flat")
    d.add_layer("solid", name="Card", comp="Flat")
    assert d.set_3d_layer("Card", accepts_lights=False, comp="Flat")["renderer"] == "ADBE Advanced 3d"
    d.add_layer("light", name="Key", comp="Flat")
    with pytest.raises(ToolError, match="Key is a light"):
        d.set_3d_layer("Key", extrusion=5, comp="Flat")
    for kwargs, msg in [({"bevel_style": "round"}, "bevel_style"), ({"extrusion": -1}, "0 or more"),
                        ({"metal": 101}, "metal must be")]:
        with pytest.raises(ToolError, match=msg):
            d.set_3d_layer("Card", comp="Flat", **kwargs)


def test_depth_stack(ae):
    d.create_comp("Main")
    for name in ("Sky", "Hills", "Tree"):
        d.add_layer("solid", name=name)
    out = d.depth_stack(["Tree", "Hills", "Sky"], spacing=500)
    # a new camera 1777.8 px in front: 500 px further back looks 1777.8 / 2277.8 as big, scaled up to match
    assert out == {"camera": "Camera", "layers": [
        {"layer": "Tree", "z": 0, "scale": 100}, {"layer": "Hills", "z": 500, "scale": 128.12},
        {"layer": "Sky", "z": 1000, "scale": 156.25}]}
    assert ae.inspect("ae.app.project.item(1)._layers.filter(l => l.name === 'Sky')[0].threeDLayer") is True
    flat = d.depth_stack(["Tree", "Hills"], spacing=100, compensate=False, camera="Camera")
    assert [l["scale"] for l in flat["layers"]] == [100, 128.12]  # left as they were
    with pytest.raises(ToolError, match="no camera Nope"):
        d.depth_stack(["Tree", "Hills"], camera="Nope")
    with pytest.raises(ToolError, match="only visible layers"):
        d.depth_stack(["Tree", "Camera"])
    for kwargs, msg in [({"layers": ["Tree"]}, "at least 2"), ({"spacing": 0}, "spacing")]:
        with pytest.raises(ToolError, match=msg):
            d.depth_stack(**{"layers": ["Tree", "Hills"], **kwargs})


def test_script_that_did_not_run(monkeypatch):
    # with a dialog open, DoScript prints "0" and writes no result file
    monkeypatch.setattr(d, "RUNNER", lambda script, timeout: "0")
    with pytest.raises(ToolError, match="close any open dialog"):
        d.status()


def test_find_missing_footage_nothing_missing(ae, tmp_path):
    out = d.find_missing_footage(search=str(tmp_path))
    assert out == {"missing": [], "relinked": [], "still_missing": [], "files_searched": 0}
