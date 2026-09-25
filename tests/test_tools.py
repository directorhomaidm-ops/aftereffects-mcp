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
