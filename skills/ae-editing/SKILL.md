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

## Talking heads and voice-overs

- Jump cuts: `cut_silences(layer)` removes the pauses (quieter than -40 dB for 0.5 s) and closes
  the gaps. Too choppy: raise `min_silence` to 0.8 or `padding` to 0.2; breaths survive: raise
  `threshold_db` toward -35.
- Music under speech: `duck_audio("music", "voice", amount_db=-12)` dips the music while the
  voice talks; -8 for a light touch, -18 to make room for a quiet voice.
- Run `cut_silences` on the voice before `duck_audio` if both are needed: ducking follows the
  voice layer it is given.
- Video files need `ffmpeg` on the Mac for the sound analysis; WAV files do not.

## Photos and formats

- A photo montage: `slideshow(["a.jpg", "b.jpg", ...], duration=3, overlap=0.5)` imports the
  files, fills the frame with each, and alternates zoom-in and zoom-out Ken Burns moves.
- One still or shot: `ken_burns(layer, zoom=[100, 115], pan="left", amount=120)`.
- Vertical and square versions for social: `reframe(comp, "9:16")` (`focus` keeps the subject
  in frame), or `"blur_fill"` to show the whole picture over a blurred copy.

## Check

`export_frame` at the cut points and in the middle of each ramp. Look for black frames
(a clip ran out) and for jumps where a cut should flow.
