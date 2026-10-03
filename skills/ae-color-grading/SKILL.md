---
name: ae-color-grading
description: Color grade After Effects comps through the aftereffects MCP server - looks (teal and orange, noir, vintage, day for night...), exposure and contrast fixes, white balance, split toning, vignettes, rebranding colors. Use when asked to grade, color, give a look or mood, fix exposure or white balance, or match colors in After Effects (تلوين، تصحيح ألوان، لوك سينمائي، درجة لونية).
---

# Color grading in After Effects

Tools: `color_grade`, `replace_color`, `export_frame`, `add_effect`, `describe_comp`.

## Workflow

1. **Look first.** `describe_comp` to see the layers, then `export_frame` at a few
   representative times and read the frames before changing anything.
2. **Correct before you style.** One `color_grade` pass with no look, on an adjustment
   layer named "Correction": balance `temperature`/`tint` until neutrals are neutral, set
   `exposure` (stops) so skin and key subjects sit in the midtones, then `whites` and
   `blacks` so the picture uses the full range without clipping.
3. **Then the look.** A second `color_grade` named "Look" above it, starting from a preset:

   | look | for |
   |---|---|
   | `teal_orange` | blockbuster, skin pops against cool shadows |
   | `bleach_bypass` | war, thriller: harsh, desaturated, contrasty |
   | `noir` | black and white with crushed blacks and a vignette |
   | `warm` / `golden_hour` | nostalgia, romance, sunsets |
   | `cool` | tech, cold, clinical |
   | `vintage` | faded film, lifted blacks, warm highlights |
   | `cyberpunk` | magenta and cyan night city |
   | `matrix` | green digital cast |
   | `day_for_night` | turn daylight footage into moonlight |

   Any setting passed with the look overrides it, e.g.
   `color_grade("teal_orange", saturation=105, vignette=-0.8, name="Look")`.
4. **Split toning by hand:** `shadow_balance`, `midtone_balance`, `highlight_balance` take
   [red, green, blue] pushes from -100 to 100. Push the shadows and highlights toward
   opposite hues (shadows [-20, 0, 25] with highlights [20, 5, -20] is teal and orange).
5. **Grade one shot only:** pass `layer` instead of grading the whole comp.
6. **Check:** `export_frame` again at the same times and compare. Keep skin tones
   natural; push the look on the background, not on faces.

## Rules of thumb

- Grade on adjustment layers (the default), not on footage: the grade stays editable and
  covers every layer below it.
- Small moves: contrast 10-30, saturation 85-120, temperature ±10-30. Extreme values belong
  to stylized looks only.
- `vignette` -0.5 to -1.5 is subtle; below -2 is a stylized frame.
- Changing a brand color everywhere (shape fills, text, solids, effects) is
  `replace_color`, not a grade.
- When the user names an effect that `color_grade` does not cover (Curves, Hue/Saturation),
  use `add_effect` with `settings`, after `list_effects` to confirm its name.
