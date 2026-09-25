# aftereffects-mcp

An [MCP](https://modelcontextprotocol.io) server that drives Adobe After Effects: compositions, layers, properties, keyframes with easing, expressions, effects, masks, vector shapes, text animation presets, precomposing, layer order and switches, track mattes, markers, import, the render queue and frame export.

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
| `set_comp` | `comp`, `name`, `width`, `height`, `duration`, `frame_rate`, `bg_color`, `work_area: [start, end]` | comp settings with the work area |
| `render_templates` | — | `{outputModules, renderSettings}` template names |
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
- `run_jsx` runs any ExtendScript inside one undo group; use it for what the other tools do not cover.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AE_APP` | `Adobe After Effects 2026` | The application name AppleScript talks to |
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
