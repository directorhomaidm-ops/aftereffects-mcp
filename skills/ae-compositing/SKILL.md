---
name: ae-compositing
description: Composite in After Effects through the aftereffects MCP server - green and blue screen keying (Keylight, Key Cleaner, spill suppression), blending modes, track mattes, matching a foreground to its background (grade, light, grain, shadows, defocus). Use when asked to key, remove a green screen, composite, blend, or put a subject into a new background in After Effects (دمج، كروما، إزالة الشاشة الخضراء، تركيب).
---

# Compositing in After Effects

Tools: `key_out`, `set_switches`, `color_grade`, `add_effect`, `add_layer`, `move_layer`,
`fit_to_comp`, `export_frame`, `run_jsx`.

## Keying a green or blue screen

1. Background at the bottom, keyed footage above it (`move_layer`).
2. `export_frame` and look at the screen: if it is uneven or not pure green, pass its
   color (hex or [r, g, b]) to `screen` instead of "green".
3. `key_out(layer, screen="green")` adds Keylight, Key Cleaner and Advanced Spill
   Suppressor, in Adobe's recommended order.
4. Refine:
   - background holes in the subject: raise `clip_white` toward 100 (or lower it to make
     the subject solid);
   - screen left in the background: raise `clip_black` (10-30);
   - hard edges: `softness` 1-3; a halo: `shrink` -1 to -3 or `choke` 1-2;
   - fine hair: keep `clean` on and raise `edge_radius`.
5. Check frames with fast motion and hair: `export_frame` at those times.

## Making it belong

A key is half the job; the subject has to sit in the plate:

- **Color:** `color_grade(layer=<subject>, ...)` to match the background: warmer or
  cooler, lift or crush the blacks to the background's black level, match saturation. Then
  one grade on top of everything to marry them.
- **Light direction:** light the subject from where the plate's light comes from: a solid
  with a large feathered `add_mask`, blending `screen` or `add` (`set_switches`), low opacity.
- **Depth:** blur the background a little (`add_effect` "Gaussian Blur", 3-10) so it reads
  as out of focus behind the subject.
- **Grain:** footage grain differs from CG and stills. An adjustment layer with
  `add_effect` "Noise" (2-5 %) over both unifies them.
- **Shadows:** a black solid with a soft elliptical mask under the subject, blending
  `multiply`, opacity 30-60 %.

## Blending

`set_switches(layer, blending="screen")` drops black (flares, fire, smoke over black);
`multiply` drops white (paper, ink); `overlay`/`soft_light` for textures; `add` for light.
