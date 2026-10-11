---
name: ae-motion-graphics
description: Motion graphics in After Effects through the aftereffects MCP server - animating layers in and out (fades, slides, pops, blurs), lower thirds and name straps, gradient backgrounds, progress bars, neon text, title cards, social video formats. Use when asked to animate something in or out, make a lower third or title, build a background, add a progress bar, make text glow or look neon, or produce versions for Instagram, TikTok or YouTube Shorts in After Effects (موشن جرافيك، حركة دخول وخروج، شريط اسم، خلفية متدرجة، نيون، شريط تقدم، فيديو عمودي).
---

# Motion graphics in After Effects

Tools: `animate_layer`, `lower_third`, `gradient_background`, `progress_bar`, `neon`,
`animate_text`, `text_box`, `stagger_layers`, `reframe`, `set_motion_blur`, `export_frame`.

## Entrances and exits

`animate_layer(layer, style, "in")` at the layer's in point, `"out"` ending at its out point.
An in and an out on the same layer combine.

| style | feel |
|---|---|
| `fade` | calm, corporate |
| `slide_<dir>` | direction and energy; 100-300 px is enough |
| `pop` | playful, social: a scale with a 10 % overshoot |
| `scale` | clean growth from nothing |
| `blur` | soft, cinematic focus pull |
| `rotate` | quirky |

Timing: in 0.4-0.7 s, out a little faster (0.3-0.5 s). Stagger groups of layers 0.05-0.15 s
apart (`stagger_layers`) instead of animating them all at once. Turn on `set_motion_blur` for
slides and pops.

## Title cards

1. `gradient_background(["#14213D", "#7B2CBF"], angle=45)`; add `cycle=8` for a slow drift.
2. The title text (`add_layer` text, `set_text` for size and font), then
   `animate_layer(title, "slide_up", distance=80)`, or `animate_text` for letter by letter.
3. `neon(title, "#00E5FF")` for night, music and gaming looks; `flicker=0.05` for a real tube.

## Name straps

`lower_third("Name", "Role", start=2, duration=5)` builds the bar, the title and the subtitle,
with their animations, in the bottom left; `side="right"` mirrors it. Use the brand color for
`accent`. Keep the duration between 4 and 6 s: long enough to read twice.

## Progress and formats

- `progress_bar()` fills along the bottom over the comp's length: tutorials, stories, ads.
- `reframe(comp, "9:16")` for Reels, TikTok and Shorts; `"1:1"` or `"4:5"` for feeds. Use `focus`
  to keep the subject in frame, or `"blur_fill"` when everything must stay visible.

## Check

`export_frame` at a resting moment (everything on screen) and in the middle of each entrance.
Titles must stay inside a safe margin of about 5 % of the frame on every side, and in 9:16 also
clear of the bottom 15 %, where apps put their buttons.
