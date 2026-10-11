---
name: ae-editing
description: Edit footage creatively in After Effects through the aftereffects MCP server - cutting to the beat of music, montages, transitions (crossfade, dips, push, whip, zoom, spin), speed ramps, freeze frames, trimming and sequencing. Use when asked to cut, edit, make a montage or a music video, add transitions, or speed up or slow down shots in After Effects (تقطيع، مونتاج، قص على الإيقاع، انتقالات، تسريع وتبطيء).
---

# Creative editing in After Effects

Tools: `markers_from_audio`, `beat_cut`, `transition`, `speed_ramp`, `time_remap`,
`sequence_layers`, `split_layer`, `set_layer`, `trim_comp`, `export_frame`.

## Cutting to music

1. Import the song and the clips (`import_file`) and put them in one comp
   (`add_layer` kind item).
2. `markers_from_audio` on the song: comp markers on the beats, and the tempo.
3. `beat_cut(["clip a", "clip b", "clip c"], every=2)` cuts on every second beat, cycling
   through the clips. Each clip resumes where its last cut ended, so a long take tells its
   story across the montage. `skip` jumps ahead inside a clip between its cuts.
   - Fast energy: `every=1`. Calm: `every=4`. For a chorus, run `beat_cut` again over that
     section only (`start`, `end`).
   - Without markers: `bpm=` or explicit `beats=[...]`.
4. The original layers are turned off; the cuts are new layers named "<clip> cut <n>".

## Transitions

`transition(from_layer, to_layer, kind, duration)` moves the second layer so the two
overlap, and puts it on top.

| kind | use |
|---|---|
| `crossfade` | time passing, soft mood changes |
| `dip_to_black` / `dip_to_white` | chapter breaks, flashbacks, flashes |
| `push_<dir>` | graphic, energetic, a continuous move |
| `whip_<dir>` | fast action, travel: a push with motion blur |
| `zoom` | punchy, social video |
| `spin` | playful |

Durations: 0.2-0.4 s for whips and zooms, 0.5-1 s for crossfades, about 1 s for dips.
Most cuts should stay hard cuts; transitions are punctuation.

## Speed

- `speed_ramp(layer, [{"time": 2, "speed": 30}, {"time": 3.5, "speed": 100}])` slows to
  30 % at 2 s and recovers at 3.5 s, easing over `ramp` seconds (0.3 default; 0 = instant).
- Speed 0 is a freeze frame; a negative speed plays backwards.
- It fails when the clip would run out of footage, and names the time: trim the layer
  (`set_layer` out_point) or lower the speeds.
- Hit-and-slow: a ramp down to 20-30 % on the impact, then back to 100-200 %.

## Check

`export_frame` at the cut points and in the middle of each ramp. Look for black frames
(a clip ran out) and for jumps where a cut should flow.
