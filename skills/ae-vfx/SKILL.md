---
name: ae-vfx
description: Visual effects in After Effects through the aftereffects MCP server - camera shake (handheld or impact), glitches, snow and rain, cinematic letterbox bars, glow, motion blur, film looks, tracking text to footage, 3D and parallax. Use when asked for VFX, effects, shake, glitch, weather, a cinematic look, or energy and impact in After Effects (مؤثرات بصرية، اهتزاز الكاميرا، جليتش، مطر، ثلج، أشرطة سينمائية).
---

# Visual effects in After Effects

Tools: `camera_shake`, `glitch`, `weather`, `letterbox`, `color_grade`, `add_effect`,
`set_motion_blur`, `track_point`, `attach_to_track`, `depth_stack`, `camera_move`,
`export_frame`.

## Recipes

- **Handheld documentary:** `camera_shake("handheld", amount=6, frequency=1.5, rotation=0.3)`.
  Low frequency (1-2) reads as breathing hands; 5-8 as running.
- **Impact / explosion / bass drop:** `camera_shake("impact", at=<hit time>, amount=40,
  frequency=12, rotation=2, decay=5)`. Pair it with a `transition` "zoom" or a flash
  (`dip_to_white` 0.2 s) on the same frame.
- **Glitch:** `glitch(start, duration=0.5, intensity=1.5)` on transitions, logo reveals and
  drops. Short bursts (0.2-0.6 s) work better than long ones.
- **Weather:** `weather("snow", amount=8000, wind=10)` or `weather("rain", amount=9000)`.
  Then grade to match: cool and lower contrast for snow, darker and desaturated for rain.
- **Cinematic frame:** `letterbox(2.39)`, or `animate=1` to slide the bars in at the start
  of a scene.
- **Film look:** `color_grade("vintage")` plus an adjustment layer with `add_effect`
  "Noise" (3-6 %) and a light `vignette`.
- **Glow and light:** `add_effect(layer, "Glow")` on bright elements; blending `add` or
  `screen` for light elements over footage.
- **Motion:** `set_motion_blur` on anything that moves fast, so it reads as filmed rather
  than animated.
- **Text in the scene:** `track_point` a feature in the footage, then `attach_to_track` the
  text so it sticks to the shot.
- **Depth:** `depth_stack` layers into 3D and `camera_move` for a parallax push-in.

## Order of a finished shot

Footage → keying or compositing → effects (weather, glow) → camera shake (adjustment) →
grade (adjustment) → letterbox on top. Effects placed under the shake shake with the
picture; anything above it (titles) stays still.

## Check

`export_frame` at the effect's peak, then render a short range for timing: `set_comp`
work_area, `add_to_render_queue`, `render`. Shakes and glitches have to be judged in motion, not on stills.
