# Product brief

360° World Model Clips imports a YouTube source, lets a user save an exact frame,
calls Fal H3 Max camera-controls, and assembles the generated orbit between the
surrounding original action. It runs locally or as a password-protected shared
workspace. Each browser connects its own Fal key through a blank field on the
main page. The server keeps that key in memory for up to eight hours, removes it
on disconnect or restart, and never substitutes a host owner's key for a browser
request. Media and project files remain shared on a shared host.

The editing flow is source, frame and result. Search, source playback and manual
selection do not trigger video generation. Titles containing `football` receive
caption/audio suggestions; other titles skip that processing.

The default edit is 4 seconds of original action, 6 seconds of orbit, then up to
10 seconds of resumed original video and audio, at most 20 seconds total. Require
the full lead-in. The tail starts at the next source frame and uses the remaining
footage, so a selection on the last frame has no tail. The result is 16:9 at 1080p or 4K.
Fal output is requested at 1080P; 4K delivery retains native source and reference
detail while enlarging generated frames. `orbit_preset.json` owns the fixed prompt,
model settings and original Around path. `orbit_paths.py` derives the selectable
Around, Over & under and Diagonal requests. The vertical and diagonal choices
still need visual review of generated results. Rerun uses the current choice on
the same saved frame; recovery uses the original run's stored path and parameters.

Local reference editing makes the first and last orbit frames identical and
resumes on the next source frame. These checks establish edit boundaries, not
the quality of generated motion. Frozen subjects, camera motion and the visible
handoff still require review.

Every new edit automatically loops the source's own 1.75 seconds of pre-freeze
audio under the orbit, with speed and pitch easing from 1x to 0.75x and back.
The complete mix stays together, including music, speech and background noise.
Original audio resumes in sync with the source picture. Silent inputs stay silent.
This local effect needs no override, speech separation, full-video analysis or
additional network request. Earlier crowd processing and source-specific settings
remain part of the saved output history.

Individual outputs and combined reels remain in persistent local storage.
Historical edits and native provider artifacts are retained. Job recovery does
not automatically repeat paid requests. This is a shared workspace, not a
tenant-isolated hosted product.
