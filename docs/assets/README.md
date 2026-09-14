# Documentation visuals

These images compare a saved source frame with its actual generated result, then
show the controls used to make the edit. The dark backgrounds and captions are
presentation; the footage and interface are real captures. They have not been
retouched to improve the model's output.

| File | What it shows |
| --- | --- |
| [world-model-preview.png](world-model-preview.png) | The saved source frame beside a low-angle view, 3.25 seconds into the native Over & under generation |
| [make-an-edit.png](make-an-edit.png) | One guide assembled from three actual editor crops: video preview, frame selection and completed result with download |
| [f1-over-under.gif](f1-over-under.gif) | The static source frame beside the full native generated sequence, with no audio |
| [orbit-paths.svg](orbit-paths.svg) | An original schematic of the requested Around, Over & under and Diagonal planes |

The diagram describes requested positions. The photographic examples show one
completed **Over & under** request. They do not demonstrate a Diagonal result or
verify the complete path of every generated frame.

The still comparison is a 2080 × 1262 PNG. The editor guide is a 2080 × 3218 PNG,
captured at twice the browser's display resolution to preserve control text.
The motion preview is 960 × 314 at 12 fps, with a fixed source frame on the left.

## F1 source and generation

The source is Red Bull's
[Jumping Over A Moving F1 Car (world first)](https://www.youtube.com/watch?v=8o40mSS05iE),
published on the [Red Bull YouTube channel](https://www.youtube.com/channel/UCblfuW_4rakIf2h6aqANefA).
The saved reference is at **584.24 seconds (9:44.24)**. The example uses the
completed run `c01e0fa268d4` and its **Over & under** path.

The request used the fixed prompt and `1080P` setting defined by
[orbit_preset.json](../../config/orbit_preset.json), with the vertical trajectory
from [orbit_paths.py](../../app/orbit_paths.py), at Fal's
[`minimax/h3-max/camera-controls`](https://fal.ai/models/minimax/h3-max/camera-controls)
endpoint. It requested six seconds. The native response contains **158 frames,
1920 × 1080, 24 fps**, with a **6.583333-second** picture track.

The comparison still uses native time **3.25 seconds**, where the generated view
looks up at the bicycle against the sky. The motion preview follows the native
sequence, with the saved source frame held beside it. These documentation
previews are resized for the README. They preserve the generated content,
including changes to the rider, bicycle and background. No new model request
was made to create the documentation assets.

## Editor guide and saved edit

The workflow image contains actual views of the running editor and the saved
example. Its three crops show the video preview, source frame controls and
completed output. Their placement and captions form a guide, rather than one
uncropped browser screenshot. No controls or output were fabricated.

The saved finished edit retains its historical **8-second lead, 6-second orbit
and 4-second tail**, totalling **18 seconds**. The full native timeline was
retimed to six seconds at 30 fps. Local assembly enlarged the generated frames
for 3840 × 2160 delivery and inserted the saved source reference at both orbit
endpoints. Those local endpoint matches do not establish pose preservation
throughout the generated movement.

New edits use a **4-second lead, 6-second orbit and up to 10-second continuation**.
The current controls and the historical output can therefore show different
timing. Neither the saved edit nor the native provider response was regenerated
for this documentation refresh.

## Rights and included files

Only the four documentation visuals listed above are included. Original source
recordings, full native provider files, finished video exports and runtime
receipts remain outside the repository.

The underlying footage and visible marks belong to their respective rights
holders. The repository's MIT code license does not grant rights to that
third-party material. This is an independent software demonstration, with no
claim of endorsement by Red Bull, Formula 1 or Fal.
