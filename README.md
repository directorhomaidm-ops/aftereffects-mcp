# aftereffects-mcp

An [MCP](https://modelcontextprotocol.io) server that drives Adobe After Effects: compositions, layers, properties, keyframes with easing, expressions, effects, masks, vector shapes, text animation and expression presets, precomposing, layer order and switches, track mattes, markers, time remapping, layer placement, project organization, camera moves, lights, audio fades and audio-reactive animation, captions from SRT, Motion Graphics templates, import, the render queue (in After Effects or in the background through aerender) and frame export.

## Use

Requirements: macOS, After Effects 2026 (other versions: set `AE_APP`), [uv](https://docs.astral.sh/uv/).

1. In After Effects, turn on **Settings > Scripting & Expressions > Allow Scripts to Write Files and Access Network**.
2. Register the server with Claude Code:

   ```bash
   claude mcp add aftereffects -- uv run /path/to/aftereffects-mcp/aftereffects_mcp.py
   ```

3. Keep After Effects open with no dialog showing: a modal dialog blocks every script until it is closed.

## How it works

Each tool runs one command of `jsx/aemcp.jsx` inside After Effects. The server writes the script to a temporary UTF-8 file and has After Effects read and run it through AppleScript's `DoScript` (`osascript`), so text in any language survives. The JSON result comes back through a result file (After Effects 2026's `DoScript` prints `0` on stdout whatever the script returns), or on stdout when the file is missing. `export_frame` waits until After Effects has finished writing the PNG, since `saveFrameToPng` returns before the file is complete. Every change runs inside one undo group (`aemcp: <command>`), so Cmd+Z undoes a whole tool call.

`jsx/aemcp.jsx` is plain ECMAScript 3, the dialect of ExtendScript: it has its own JSON writer and no ES5 built-ins.

## Tools

Comps are addressed by name or id (default: the active comp), layers by 1-based index or name. Times are in seconds.

| Tool | Parameters | Returns |
|---|---|---|
| `status` | — | `{version, project, items, activeComp}` |
| `list_items` | — | `[{id, name, type, folder, width, height, duration, frameRate, file}]` |
| `create_comp` | `name`, `width = 1920`, `height = 1080`, `duration = 10`, `frame_rate = 30`, `pixel_aspect = 1`, `bg_color` | comp info; the new comp becomes active |
| `comp_info` | `comp` | `{id, name, width, height, duration, frameRate, layers: [{index, name, type, inPoint, outPoint, startTime, parent, effects, source}]}` |
| `add_layer` | `kind` (text, solid, shape, null, adjustment, camera, light, item), `comp`, `name`, `text`, `color`, `item` | layer info |
| `delete_layer` | `layer`, `comp` | `{deleted, layers}` |
| `set_layer` | `layer`, `comp`, `name`, `start_time`, `in_point`, `out_point`, `enabled`, `parent`, `unparent` | layer info |
| `set_text` | `layer`, `text`, `font` (PostScript name), `size`, `color`, `justification`, `comp` | `{property, value}` |
| `get_property` | `layer`, `property`, `comp` | `{property, matchName, value, keys, expression}` |
| `set_property` | `layer`, `property`, `value`, `comp` | `{property, value}`; refused on animated properties |
| `set_keyframes` | `layer`, `property`, `keys: [{time, value, ease, influence}]`, `clear = False`, `comp` | `{property, keys, times}` |
| `set_expression` | `layer`, `property`, `expression` (`""` removes), `comp` | `{property, expression, enabled, error, value}` |
| `add_effect` | `layer`, `effect` (display or match name), `settings: {parameter: value}`, `name`, `comp` | `{layer, effect, matchName, set}` |
| `list_effects` | `query` | `[{name, matchName, category}]` |
| `import_file` | `path`, `sequence = False`, `folder` | `{id, name, type}` |
| `add_to_render_queue` | `output`, `comp`, `template` (output module template name) | `{comp, index, output}` |
| `render` | `timeout = 1800` | `{rendered, items: [{comp, status, output}]}` |
| `export_frame` | `path` (.png), `time = 0`, `comp` | `{comp, time, path}` |
| `save_project` | `path` (.aep) | `{project}` |
| `add_mask` | `layer`, `shape` (rect, ellipse, path), `rect: [x, y, w, h]`, `points`, `mode`, `feather`, `expansion`, `opacity`, `inverted`, `name`, `comp` | `{layer, mask, masks, mode, vertices}` |
| `add_shape` | `shape` (rect, ellipse, star, polygon), `size = [200, 200]`, `fill`, `stroke`, `stroke_width = 4`, `roundness`, `points = 5`, `position`, `layer` (existing shape layer), `layer_name`, `name`, `comp` | `{layer, index, group, shape}` |
| `animate_text` | `layer`, `preset` (fade_in, typewriter, slide_up, scale_in, blur_in), `duration = 1`, `start`, `comp` | `{layer, animator, preset, from, to, keys}` |
| `precompose` | `layers`, `name`, `move_attributes = True`, `comp` | `{comp, precomp, id, precompLayers, layer}` |
| `duplicate_layer` | `layer`, `name`, `comp` | layer info of the copy (right above the original) |
| `move_layer` | `layer`, `to` (top, bottom, before, after), `other`, `comp` | `{layer, index}` |
| `set_switches` | `layer`, `blending`, `three_d`, `motion_blur`, `shy`, `solo`, `locked`, `matte` (alpha, alpha_inverted, luma, luma_inverted, none), `matte_layer`, `comp` | `{layer, threeD, motionBlur, shy, solo, locked, matteLayer}` |
| `add_marker` | `time`, `comment`, `duration`, `layer` (default: the comp), `comp` | `{target, time, markers}` |
| `set_comp` | `comp`, `name`, `width`, `height`, `duration`, `frame_rate`, `bg_color`, `work_area: [start, end]`, `motion_blur`, `shutter_angle` (0-720) | comp settings with the work area and motion blur |
| `render_templates` | — | `{outputModules, renderSettings}` template names |
| `expression_preset` | `layer`, `property`, `preset` (wiggle, loop_cycle, loop_pingpong, loop_continue, bounce, spin, blink), `params`, `comp` | `set_expression` result + preset and params |
| `property_tree` | `layer`, `depth = 3`, `comp` | `{layer, properties: [{name, matchName, value, keys, children}]}` |
| `sequence_layers` | `layers` (in order), `start = 0`, `overlap = 0` (negative: gap), `comp` | `{layers: [{layer, inPoint, outPoint}], end}` |
| `time_remap` | `layer`, `keys: [{time, source, hold}]`, `smooth = True`, `comp` | `{layer, keys, timeRemap}` |
| `fit_to_comp` | `layer`, `mode` (fill, fit, width, height, stretch), `comp` | `{layer, mode, scale, size}` |
| `center_anchor` | `layer`, `comp` | `{layer, anchor, position}` |
| `null_control` | `layers`, `name = "Control"`, `comp` | `{control, index, children}` |
| `apply_preset` | `layer`, `path` (.ffx), `comp` | layer info |
| `replace_footage` | `item`, `path`, `sequence = False` | `{id, name, file}` |
| `create_folder` | `name`, `parent` | `{id, name, parent}` |
| `move_items` | `items`, `folder` | `{folder, moved}` |
| `clean_project` | `remove_unused = True`, `consolidate = True` | `{consolidated, removedUnused, items: [before, after]}` |
| `camera_move` | `move` (push_in, pull_out, pan_left, pan_right, crane_up, crane_down, orbit), `amount` (px, or degrees for orbit), `duration = 3`, `start = 0`, `ease = True`, `camera`, `comp` | `{camera, move, from, to, position}` or the orbit rig |
| `set_light` | `layer`, `type` (parallel, spot, point, ambient), `intensity`, `color`, `cone_angle`, `cone_feather`, `shadows`, `comp` | `{light, set}` |
| `audio_fade` | `layer`, `fade_in`, `fade_out` (s), `level_db = 0`, `comp` | `{layer, level, fadeIn, fadeOut, keys}` |
| `audio_react` | `audio` (layer with sound), `layer`, `property = "scale"`, `amount = 1`, `name = "Audio Amplitude"`, `comp` | `{amplitude, target, property, expression, error}` |
| `captions_from_srt` | `path`, `comp`, `size = 60`, `font`, `color`, `y = 0.85`, `prefix = "Caption"`, `offset = 0` | `{comp, captions, first, last}` |
| `render_background` | — | `{job, project, log}` |
| `render_background_status` | `job` | `{job, state, exit_code, log_tail}` |
| `mogrt_add_property` | `layer`, `property`, `name`, `comp` | `{comp, layer, property, name}` |
| `export_mogrt` | `path` (.mogrt), `name`, `comp` | `{comp, template, path}` |
| `bake_expression` | `layer`, `property`, `step = 1` (frames), `start`, `end`, `comp` | `{property, keys, from, to, expression}` |
| `ease_keyframes` | `layer`, `property`, `ease = "ease"`, `influence = 33.33`, `comp` | `{property, keys, ease}` |
| `copy_keyframes` | `layer`, `property`, `targets`, `offset = 0` (cascading), `comp` | `{property, from, to, keys}` |
| `stagger_layers` | `layers`, `offset = 0.1`, `start`, `comp` | `{layers: [{layer, inPoint}]}` |
| `split_layer` | `layer`, `time`, `name`, `comp` | `{first, second}` |
| `trim_comp` | `to` (layers, work_area), `comp` | `{comp, duration, shiftedBy}` |
| `make_variants` | `rows: [{name, texts: {layer: text}}]`, `comp`, `output_dir`, `template` | `{from, variants: [{comp, id, queued}]}` |
| `import_layered` | `path` (.psd, .ai), `mode` (comp_cropped, comp, footage) | `{id, name, type, layers}` |
| `find_missing_footage` | `search` (folder), `max_files = 200000` | `{missing, relinked, still_missing, files_searched}` |
| `trim_paths` | `layer`, `duration = 1`, `start`, `erase = False`, `offset`, `ease = True`, `comp` | `{layer, trim, animated, from, to}` |
| `shape_repeater` | `layer`, `copies = 5`, `offset = [100, 0]`, `scale = 100`, `rotation`, `end_opacity = 100`, `comp` | `{layer, repeater, copies}` |
| `text_style` | `layer`, `tracking`, `leading`, `stroke_color`, `stroke_width`, `all_caps`, `comp` | `{property, value}` |
| `add_paragraph` | `text`, `box = [800, 400]`, `name`, `comp` | layer info |
| `set_motion_blur` | `on = True`, `layers = "all"` (or a list, or None), `shutter_angle`, `shutter_phase`, `comp` | `{comp, motionBlur, shutterAngle, shutterPhase, layers}` |
| `markers_from_audio` | `layer` (WAV audio), `sensitivity = 1.5`, `min_gap = 0.25`, `max_markers = 300`, `comment = "beat"`, `comp` | `{comp, added, markers, bpm_estimate, first}` |
| `sequence_to_markers` | `layers`, `trim = True`, `comp` | `{layers: [{layer, inPoint, outPoint}]}` |
| `reduce_project` | `comps` | `{kept, removed, items: [before, after]}` |
| `render_queue_list` | — | `[{index, comp, status, output}]` |
| `clear_render_queue` | `all_items = False` | `{removed, left}` |
| `motion_path` | `layer`, `points: [[x, y(, z)], ...]`, `duration = 2`, `start = 0`, `times`, `smooth = True`, `ease = True`, `constant_speed = False`, `auto_orient = False`, `comp` | `{layer, keys, from, to, smooth, constantSpeed, autoOrient}` |
| `bezier_ease` | `layer`, `property`, `curve` (a named easing or `[x1, y1, x2, y2]`), `keys: [first, last]`, `comp` | `{property, segments, keys, curve}` |
| `track_point` | `layer`, `point: [x, y]` (layer pixels), `start`, `end`, `feature = 31`, `search = 40`, `tracker = "aemcp track"`, `name = "Track Point 1"`, `comp` | `{layer, tracker, point, keys, from, to, start, end, min_confidence, lost_frames, first_lost}` |
| `attach_to_track` | `layer` (follower), `tracked`, `tracker`, `point`, `keep_offset = False`, `comp` | `{layer, follows, expression, error}` |
| `set_camera` | `layer`, `focal_length` (mm), `depth_of_field`, `focus_distance`, `focus_on` (layer: autofocus), `f_stop`, `blur_level`, `comp` | `{camera, set, error}` |
| `set_3d_layer` | `layer`, `extrusion`, `bevel`, `bevel_style` (angular, concave, convex, none), `casts_shadows`, `accepts_shadows`, `accepts_lights`, `specular`, `shininess`, `metal`, `reflection`, `comp` | `{layer, threeD, renderer, set}` |
| `depth_stack` | `layers` (nearest first), `spacing = 500`, `compensate = True`, `camera`, `comp` | `{camera, layers: [{layer, z, scale}]}` |
| `run_jsx` | `code` | the script's value |

Notes:

- `property` is a short name (`anchor`, `position`, `scale`, `rotation`, `opacity`, `text`) or a path of display or match names: `["Effects", "Gaussian Blur", "Blurriness"]`, `["ADBE Transform Group", "ADBE Position"]`. A wrong name lists what is there.
- Easing: `ease` (both sides, default), `ease_in` (slows into the key), `ease_out` (slows out of it), `linear`, `hold`; `influence` 0.1-100 (default 33.33). Spatial properties such as position take one ease per side, others one per dimension; the server handles that.
- `render` keeps After Effects busy until the queue is done. A render queue item whose setup fails (e.g. an unknown template) is removed again, so it cannot stop the next render.
- Masks are in layer pixels from the layer's top left. A mask's properties animate like any other: `set_keyframes(layer, ["Masks", "Spot", "Mask Feather"], ...)`.
- `add_shape` puts each shape in its own group; colors are `[r, g, b]` 0-1 (the alpha the shape properties need is added). Without `layer` it makes a new shape layer.
- `animate_text` adds a text animator with a range selector whose start runs 0 to 100 %, revealing characters in order; `typewriter` uses hard steps. Calling it again stacks another animator.
- `set_switches` applies `locked` last, since a locked layer takes no more changes. Track mattes use `Layer.setTrackMatte` (After Effects 2023+).
- `render_templates` lists the exact template names to pass to `add_to_render_queue(template=...)`; names depend on the installed templates and the application language.
- `expression_preset` defaults: wiggle `freq 2, amp 30`; bounce `amp 0.05, freq 4, decay 8` (an overshoot after each keyframe); spin `speed 90` deg/s; blink `freq 2`. The loop presets repeat a property's keyframes after the last one.
- `property_tree` shows the real names under a layer (they differ between layer types and application languages); match names are the same in every language.
- `sequence_layers` moves each layer's start time so its in point follows the previous out point; trims are kept.
- `time_remap` replaces the keys After Effects adds when time remapping is turned on (at the in and out points). It needs footage or a precomp.
- `fit_to_comp` and `center_anchor` measure text and shapes with `sourceRectAtTime`; `center_anchor` ignores rotation when keeping the layer in place.
- `null_control` parents the layers to a new null at their average position; parenting keeps each one where it is.
- `clean_project` consolidates duplicate footage (same file) first, then removes footage no comp uses.
- `camera_move` creates a camera when none is named and continues from the camera's position at `start`. Push and pull move along the line to the point of interest; pans and cranes move camera and point of interest together; orbit parents the camera to a 3D null at the point of interest and turns its Y rotation. Layers need their 3D switch (`set_switches(three_d=True)`) to show depth.
- `audio_react` runs Animation > Keyframe Assistant > Convert Audio to Keyframes (found by its English menu name) and links the property to the "Both Channels" slider: x and y move for scale and position, z stays.
- `captions_from_srt` makes one centered text layer per cue; tags such as `<i>` are dropped, line breaks kept.
- `render_background` saves the project, then runs `aerender -project` on it (next to the application, or `AE_RENDER`), so After Effects stays usable. The job lives as long as the server; aerender can exit 0 after an error, so its log decides the state.
- `mogrt_add_property` uses `addToMotionGraphicsTemplateAs`; `export_mogrt` writes the comp's template for Premiere Pro.
- `bake_expression` samples the expression's result and switches the expression off (it stays, to switch back on).
- `copy_keyframes` keeps each key's interpolation and easing; its offset cascades (target 1: offset, target 2: 2 x offset...).
- `make_variants` duplicates the template comp per row and replaces the named text layers' text; with `output_dir` each copy is queued with its name as the file name. Render them with `render` or `render_background`.
- `find_missing_footage(search=...)` matches missing files by name (any case) under a folder and relinks them with `FootageItem.replace`.
- `trim_paths` and `shape_repeater` add the operator at the top of the shape layer's contents, so they act on every group in it.
- `markers_from_audio` reads the layer's WAV file here (sharp rises in loudness in 10 ms steps), keeps the beats inside the layer's trim and converts them to comp time; `sequence_to_markers` then lays layers on those markers and, with `trim`, cuts each at the next one.
- `reduce_project` is After Effects' File > Dependencies > Reduce Project: everything the named comps do not use is removed.
- `motion_path` replaces the position keyframes. Inner points keep their speed (continuous Bezier); with `constant_speed` they rove, so After Effects spreads their timing for an even speed. `auto_orient` sets Auto-Orient Along Path.
- `bezier_ease` turns a CSS `cubic-bezier(x1, y1, x2, y2)` into temporal ease on every segment: the outgoing handle gets influence `x1` and speed `y1 / x1` x the segment's average speed, the incoming one influence `1 - x2` and speed `(1 - y2) / (1 - x2)` x average (signed per dimension; spatial properties use the speed along the path). The live check samples the result against the curve. Named curves: `ease`, `ease_in`, `ease_out`, `ease_in_out` (CSS) and `ease_in` / `ease_out` / `ease_in_out` + `_sine`, `_quad`, `_cubic`, `_quart`, `_quint`, `_expo`, `_circ`, `_back` (easings.net). Overshoot (`_back`) does not work on position: spatial motion stays on its path.
- `track_point` does its own tracking, since After Effects' Tracker analysis cannot be scripted: each frame it renders a window of the layer's source around where the feature should be (a temporary comp, removed afterwards), and matches the first frame's feature there (normalized cross-correlation, coarse to fine, sub-pixel). It follows position only, with a fixed template: a feature that turns or grows a lot is lost (low confidence). The result is written as a regular tracker on the layer, editable in the Tracker panel. About 0.2 s a frame; time-remapped layers need precomposing first.
- `attach_to_track` works with any tracker on the layer, including ones made in the Tracker panel or Mocha. It sets an expression: the attach point, through the tracked layer's transform, into the follower's parent if it has one.
- Stabilizing and camera tracking: `add_effect(layer, "Warp Stabilizer")` and `add_effect(layer, "3D Camera Tracker")` apply them; After Effects analyzes in the background. Creating the camera from a camera track is a button in the effect's panel.
- `set_camera` converts `focal_length` for a 36 mm film back across the comp width, and `f_stop` to the aperture in pixels (zoom / f-stop). `focus_on` sets an expression on Focus Distance: the target's distance along the camera's line of sight.
- New cameras (`add_layer`, `camera_move`, `depth_stack`) are placed in front of the comp center facing it: After Effects 26 itself puts them at x = y = 0.
- `set_3d_layer` switches the comp to the Advanced 3D renderer when extruding or beveling (Classic 3D cannot extrude).
- `depth_stack` sets z = 0, spacing, 2 x spacing... and multiplies each layer's scale by (d + z) / d, d being the camera's distance to z = 0, so every layer looks as it did; a background meant to be panned across should be larger than the frame.
- `run_jsx` runs any ExtendScript inside one undo group; use it for what the other tools do not cover.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AE_APP` | `Adobe After Effects 2026` | The application name AppleScript talks to |
| `AE_RENDER` | `/Applications/<AE_APP>/aerender` | aerender for `render_background` |
| `AE_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` (JSON lines on stderr) |

## Live check

`scripts/live_check.py` runs the tools against your After Effects and writes a report. Open a new, empty project first:

```bash
uv run scripts/live_check.py
```

It fills that project with test comps, saves it as `aemcp_check.aep` in a new temporary folder, and writes `report.md` there.

## Development

```bash
uv run --with pytest --with 'mcp>=2' pytest -q
```

The tests run the real `jsx/aemcp.jsx` in Node (`tests/host.js`) against a fake After Effects object model (`tests/fake_ae.js`) that follows After Effects' rules where they matter. The script context has the ES5 built-ins ExtendScript lacks removed, so library code that would fail inside After Effects fails the tests too. Node 18+ is needed.
