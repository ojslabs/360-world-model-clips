# Documentation visuals

[orbit-paths.svg](orbit-paths.svg) is an original diagram of the three requested
camera-path planes. It is a schematic, not a generated result. The F1 previews
below use the original Around path; there are no generated Over & under or
Diagonal examples in this documentation yet.

## Editor walkthrough

These screenshots show the running editor populated with the saved F1 example
below. They were captured in Chrome from a separate copy of that project, with
no provider key connected and no new generation. The controls and video are the
actual interface; each image is cropped to the step it explains.

| Screenshot | What it shows |
| --- | --- |
| [pick-video.jpg](pick-video.jpg) | The selected video open in the preview dialog |
| [pick-frame.jpg](pick-frame.jpg) | The saved source frame at 9:46.36, with scrubber and frame controls |
| [review-edit.jpg](review-edit.jpg) | Move choices and the completed 18-second edit, paused during its orbit |

The screenshots are reduced JPEG captures for the README. Existing edits retain
their original timing, so this result shows an 8-second lead and 4-second tail;
the generation controls show the current 4-second lead and up-to-10-second tail.

## F1 example

The README previews come from a previously completed 18-second edit of Red Bull's
[Jumping Over A Moving F1 Car (world first)](https://www.youtube.com/watch?v=8o40mSS05iE),
published on the [Red Bull YouTube channel](https://www.youtube.com/channel/UCblfuW_4rakIf2h6aqANefA).
The selected reference is at **586.36 seconds (9:46.36)** in that source.

| File | What it shows | Display format |
| --- | --- | --- |
| [f1-orbit.gif](f1-orbit.gif) | The complete six-second orbit section of the saved edit | 640 × 360, 10 fps, no audio |
| [f1-sequence.jpg](f1-sequence.jpg) | Orbit frames at 0, 1.5, 3 and 4.5 seconds, read left to right then top to bottom | Four 480 × 270 panels |

The edit used [preset v5](../../config/orbit_preset.json) and Fal's
[`minimax/h3-max/camera-controls`](https://fal.ai/models/minimax/h3-max/camera-controls)
endpoint, requesting `1080P` and six seconds. The returned video had 1920 × 1080
frames at 24 fps and a 6.583333-second picture track. Local processing retimed its
complete timeline to six seconds at 30 fps, enlarged the generated frames for
the 3840 × 2160 delivery, and inserted the saved source reference at both
endpoints. That historical edit lasts 18 seconds, with an 8-second lead, 6-second
orbit and 4-second tail. New edits use a 4-second lead, 6-second orbit and up to
10 seconds of continuation; these preview files have not been regenerated.

The GIF and still were made with FFmpeg from that finished edit. Their smaller
size and frame rate are for the README. They show generated scene changes as
well as camera motion; the local endpoint check does not verify pose preservation
throughout the orbit. No new model request was made to create these previews.

The original video, native provider files, full exports and runtime receipts are
not included. Only the reduced previews and editor screenshots listed here were
selected for publication. The underlying footage and visible marks belong to their respective
rights holders; the repository's MIT code license does not grant rights to that
third-party material. This is an independent software demonstration, with no
claim of endorsement by Red Bull, Formula 1 or Fal.

These commands reproduce the previews from the historical 8/6/4 layout. They read
`highlight.mp4` and write new documentation files. For a new 4/6/up-to-10 edit,
the orbit starts at 4 seconds: use `-ss 4` and select frames 120, 165, 210 and 255
in the second command instead.

```sh
ffmpeg -v error -ss 8 -i highlight.mp4 -t 6 \
  -filter_complex 'fps=10,scale=640:360:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=4' \
  -an -loop 0 f1-orbit.gif

ffmpeg -v error -i highlight.mp4 \
  -vf "select='eq(n,240)+eq(n,285)+eq(n,330)+eq(n,375)',scale=480:270:flags=lanczos,tile=2x2" \
  -frames:v 1 -q:v 2 f1-sequence.jpg
```
