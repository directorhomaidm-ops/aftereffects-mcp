---
name: ae-explainers
description: Explainer, tutorial and social videos in After Effects through the aftereffects MCP server - arrows that draw on, spotlights, labels with background boxes, timers and countdowns, number counters, word-by-word text, picture-in-picture, split screens, blurring faces and plates, audio visualizers. Use when asked to point at something, annotate, label, add a countdown, show two shots side by side, add a webcam corner, hide a face or a license plate, or make text appear word by word in After Effects (شرح، أسهم، تعليق توضيحي، عداد تنازلي، تقسيم الشاشة، تمويه الوجوه، نص كلمة بكلمة).
---

# Explainers in After Effects

Tools: `draw_arrow`, `spotlight`, `text_box`, `timer`, `number_counter`, `split_text`,
`stagger_layers`, `animate_text`, `picture_in_picture`, `split_screen`, `blur_region`,
`audio_visualizer`, `animate_mask`, `export_frame`.

Coordinates are comp pixels from the top left. Read a frame first (`export_frame`) to find
where things are.

## Pointing at something

1. `spotlight([x, y, w, h])` darkens everything but the area (fades in over 0.5 s).
2. `draw_arrow(from_point, to_point, curve=120)` draws on toward it. Start the arrow outside
   the subject and stop just short of it. A small curve (80-200 px) reads friendlier than a
   straight line.
3. A label: `add_layer` text, then `text_box(label)` for a background that keeps fitting
   when the words change.
4. To follow a moving subject, animate the spotlight's "Light" mask with `animate_mask`.

## Text that builds

- Word by word: `split_text(layer, "words")`, then `stagger_layers` on the new layers
  (0.08-0.15 s apart) and `animate_text` or keyframes for each pop.
- Lines one after another: `split_text(layer, "lines")`.
- Numbers: `number_counter` for values (money, percent); `timer` for clocks and countdowns
  (`timer(10, 0)` counts down from 10 in real time).

## Layouts

- Webcam or guest in a corner: `picture_in_picture(layer, "bottom_right", size=0.28,
  radius=16, border=4)`.
- Before and after, interviews: `split_screen([a, b], "columns", gap=8)`; 3-4 sources:
  `"grid"`.

## Privacy

`blur_region([x, y, w, h], kind="mosaic", amount=24)` hides faces, plates and screens for
the time given (`start`, `end`). For a moving subject, keyframe its "Region" mask with
`animate_mask`, or parent the layer to a tracked null.

## Music

`audio_visualizer(audio_layer, "spectrum")` for bars, `"waveform"` for a line. Put a logo or
title above it; keep `height` to a quarter of the frame.

## Check

`export_frame` at the moment each element is fully on. Labels must not cover what the arrow
points at, and text needs contrast against what is behind it (that is what `text_box` is
for).
