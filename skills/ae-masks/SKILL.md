---
name: ae-masks
description: Masks in After Effects through the aftereffects MCP server - animated reveals and wipes (iris, split, box), feathered vignettes and holes, hand-keyframed masks (rotoscoping), mask modes, track mattes. Use when asked to mask, reveal, wipe, cut out, isolate part of a shot, or animate a mask in After Effects (ماسك، قناع، كشف، ماسكات متحركة، عزل جزء من اللقطة).
---

# Masks in After Effects

Tools: `add_mask`, `set_mask`, `animate_mask`, `reveal_mask`, `set_switches` (track mattes),
`property_tree`, `export_frame`.

Mask coordinates are layer pixels from the layer's top left (text and shapes: their own box).

## Reveals

`reveal_mask(layer, style, duration, start, feather)`:

- `wipe_right`, `wipe_left`, `wipe_down`, `wipe_up`: an edge crosses the layer.
- `split_vertical`, `split_horizontal`: open from the center line.
- `box`: grow from the center. `iris`: a growing circle.
- `reverse=True` hides instead; `feather` softens the edge (20-60 px reads as cinematic).
- Over existing masks, the reveal intersects them, so it limits what they show.

## Static masks

- `add_mask(layer, "ellipse", rect=[x, y, w, h], feather=150, inverted=True)` darkens around
  a subject on a black solid: a vignette or a spotlight.
- `add_mask` with `mode="subtract"` cuts a hole; `mode="intersect"` limits to the overlap.
- `set_mask` changes a mask later: mode, feather, expansion (negative shrinks), opacity,
  inverted, name.

## Animated masks and roto

`animate_mask(layer, mask, keys)` keyframes a mask by hand:

```
animate_mask("Shot", "Window", [
  {"time": 0, "points": [[100, 200], [400, 180], [420, 600], [90, 620]]},
  {"time": 1, "points": [[160, 210], [470, 190], [480, 610], [150, 640]], "feather": 6},
])
```

- Keep the same number of points on every key so the shape morphs instead of jumping.
- For roto: keys every 5-10 frames first, then add keys where the edge drifts. Check with
  `export_frame` between the keys.
- Feather 2-6 px for roto edges; animate `expansion` to choke or spread the edge.

## Track mattes

To show one layer through another (text filled with video, a shape window):
`set_switches(fill_layer, matte="alpha", matte_layer="Shape")`. Also `luma`, and inverted
versions. The matte layer stops rendering on its own.
