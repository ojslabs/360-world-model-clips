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

The default edit is 8 seconds of original action, 6 seconds of orbit, then
4 seconds of resumed original video and audio. The result is 16:9 at 1080p or 4K.
Fal output is requested at 1080P; 4K delivery retains native source and reference
detail while enlarging generated frames. The exact provider request is owned by
`orbit_preset.json`.

Local reference editing makes the first and last orbit frames identical and
resumes on the next source frame. These checks establish edit boundaries, not
the quality of generated motion. Frozen subjects, camera motion and the visible
handoff still require review.

Orbit audio uses checked source atmosphere or an explicit source-specific
slow-motion loop. The latter repeats the 1.75 seconds immediately before the
freeze with speed/pitch ramps from 1x to 0.75x and back, then resumes original
audio in sync. It does not claim speech separation.

Individual outputs and combined reels remain in persistent local storage.
Historical edits and native provider artifacts are retained. Job recovery does
not automatically repeat paid requests. This is a shared workspace, not a
tenant-isolated hosted product.
