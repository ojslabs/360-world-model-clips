# Setup and development

The [README](../README.md) walks through the edit and shows a saved F1 example.
These instructions cover installation, credentials and the optional services.

## Run locally

Use Python 3.12, Node.js 22 or newer, FFmpeg and FFprobe. FFmpeg needs `libdav1d`, `libx264`
and `zscale`; `doctor.py` checks them. On macOS with Homebrew:

```sh
brew install python@3.12 ffmpeg node
python3.12 bootstrap.py
```

Bootstrap creates `.venv`, installs pinned dependencies, downloads and verifies
the pinned Silero speech model, and builds the included HTML interface. No sibling
repository, commercial font or JavaScript bundler is needed.

Start the server:

```sh
.venv/bin/python server.py
```

Open <http://127.0.0.1:8476>. In **Your Fal account**, paste your own key and click
**Connect Fal**. The field starts blank. Import and review work locally;
generating an orbit and requesting a visual action label use your Fal account.

The browser sends the key to this app's server, which holds it only in memory.
An HttpOnly cookie identifies your browser's connection without containing the
key. The connection expires after eight hours; **Disconnect** removes it, and a
server restart clears all such connections. The app does not put the key into
local storage, runtime files or output receipts. Browser requests never fall
back to a server owner's `FAL_KEY`.

The account panel also requests the credit balance with the connected key. Fal's
billing endpoint requires an admin key; an inference key may lack permission to
read it. Loading, denied access or a failed balance read does not block generation.
The request runs in the background, with one check in flight per connection and
a 60-second memory cache. Disconnecting, replacing the key, signing out or letting
the connection expire removes its balance. Late responses cannot restore it.
No billing response is saved to disk or fetched using the host owner's key.

Disconnecting prevents new requests through that connection. Work already
submitted continues with the credential captured when it started. If a restart
interrupts an acknowledged generation, reconnect the same key to check and
recover that request. The app does not purchase a replacement.

On a shared host, this means trusting its operator with your key while connected.
Run your own copy if you prefer to keep that boundary on your computer. The repo
includes small [F1 documentation previews](assets/README.md), with separate
attribution. It includes no provider credits or full source recordings.

On Linux, install Python 3.12, Node.js 22 or newer, FFmpeg and FFprobe with your
distribution's package manager, then run the same bootstrap and server commands.
FFmpeg builds vary; `doctor.py` must pass before importing AV1 sources. Docker is
another option if your distribution's packages lack the required codecs.

## Use

1. Connect your Fal account, then search for a video or paste its YouTube link and import it.
2. Scrub the full video and click **Use this frame** on the desired moment.
3. Choose **Around**, **Over & under** or **Diagonal**, generate the orbit, then review and download the completed edit.

New edits use four seconds before the selected frame, six seconds of orbit and
up to ten seconds of source continuation. Choose a frame with the full four-second
lead available. The continuation starts on the next source frame and shortens at
the end of the video, including a zero-length tail at its final frame. An edit is
at most 20 seconds long; earlier outputs retain their saved timing.

Around is the original horizontal path. The experimental Over & under choice requests a vertical loop
relative to the source camera; Diagonal uses a 45-degree inclined loop. These
choices share the same fixed prompt, six-second request, `1080P` output and
distance. [orbit_paths.py](../orbit_paths.py) owns their definitions, based on
[orbit_preset.json](../orbit_preset.json).

Fal's elevation field is bounded to ±90 degrees. The vertical path changes
azimuth at its poles with paired keyframes at the same camera position; the API
has no roll or up-vector field to guarantee the orientation between them.
Diagonal uses inclined-circle keyframes, but spherical interpolation can deviate
between those positions. Check generated results before relying on either move.
The existing F1 previews show Around only.

Rerunning an output uses its immutable saved frame with the currently selected
path. It creates a separate generation. Recovery of an interrupted request uses
the path already saved with that request. To drive the same selection through the
API, `POST /api/generate` accepts `orbit_path` with `around`, `over-under` or
`diagonal`; `GET /api/state` returns the catalogue in `orbit_paths`.

## Optional Reactor remixes

Add `REACTOR_API_KEY` to the server environment or private `.env.local` file to
enable Reactor X2 edits of finished highlights. Shared Docker deployments can
include it in the private `.env` file. This key is separate from each visitor's
Fal connection. Keep it out of the repository, browser bundle and image build
arguments. Anyone with access to the shared app can use the configured Reactor
account's credits.

Step 4 offers a live preview. Choose one finished clip, click **Start live preview**,
then switch **Day** and **Night** while Reactor streams its edited video back.
The same session receives each new prompt. **Stop** terminates the session; a
five-minute server limit caps interrupted sessions. Source audio is muted by
default and can be enabled separately; it is not synchronized to the delayed
Reactor picture. The original finished clips are unchanged.

Browser SDK assets are bundled locally. Their source, pinned versions and rebuild
instructions are documented in [Live video client](../ui/live-client/README.md).

Under **Saved remixes**, choose a completed highlight and an effect such as day-to-night, jelly world,
claymation or neon, then review or edit its prompt before starting the remix.
Reactor writes a separate result and retains the original clip and its audio.
Repeat requests for the same unchanged clip and prompt reuse the active or
completed result. Failed or interrupted sessions are not automatically repeated.

X2 works at its native output resolution, not the original 4K resolution. Its
generated action and camera framing can change; preserving output duration does
not prove exact alignment with the source frames. Inspect each result before use.
The pinned `reactor-sdk==1.5.1` is installed by bootstrap and Docker. Its Linux
wheels require glibc 2.34 or newer; the Debian trixie image satisfies that floor.
No microphone or speaker device dependency is needed.

## Import behavior

Titles containing the whole word `football`, ignoring case, receive suggestions
from caption cues and audio energy. Other titles skip captions and audio analysis
and open manual selection. A loud audio peak is not a confirmed crowd detection.
Download progress reports the current video or audio stream, followed by named
verification and preview stages. Estimates remain unknown when yt-dlp omits them.

## Orbit audio

Every new edit uses the source's own audio automatically. The app loops the
1.75 seconds immediately before the freeze into a 6.5-second bed, with smooth
joins. Playback speed and pitch ease from 1x to 0.75x over 0.75 seconds, then
return to 1x over the final 0.75 seconds. The bed overlaps the orbit boundaries
for the audio fades; it does not extend the six-second orbit.

Music, speech and background noise all receive this effect. Original source audio
stays synchronized before and after the orbit. Silent inputs remain silent, and
generated model audio is discarded. The effect uses local audio processing with
no crowd separation, full-video analysis or additional network call. No override
file is needed.

The F1 documentation previews came from an earlier 18-second edit that enabled
this treatment through a source-specific override. Those media files and their
attribution remain unchanged. New output receipts identify the current audio
method separately from earlier crowd and source-specific edits.

## Docker

The Dockerfile installs Python 3.12, Node.js 24, FFmpeg with fast AV1 decoding, pinned
Python dependencies and the verified speech model. Model/tool files live outside
the writable `/data` volume. It runs as a non-root user.

Copy `.env.example` to `.env` and fill in a password of at least 16 characters
and the browser origin. Users connect their own Fal key in the page after login.
For a local protected Docker check use
`PUBLIC_ORIGIN=http://localhost:8476`; open that exact address after starting it.

```sh
docker build -t world-model-clips .
docker run --rm --env-file .env \
  -p 127.0.0.1:8476:8476 \
  -v world-model-clips-data:/data \
  world-model-clips
```

For a shared host, supply `DEMO_PASSWORD` and the exact HTTPS
`PUBLIC_ORIGIN` in the private environment file. Terminate HTTPS at a reverse
proxy forwarding to port 8476. The shared password gates the same workspace and
saved media. Fal credentials are separate per browser connection, but project
files and outputs are still shared. This is not a tenant-isolated service.
Public hosting requirements are enforced by the server configuration.

CI builds the image and checks a protected container with a fresh data volume.
Run those checks in your deployment environment as well before publishing a host.

## Configuration

| Variable | Purpose |
| --- | --- |
| `REACTOR_API_KEY` | Optional server-side Reactor key for X2 remixes |
| `FOOTBALL_DATA_DIR` | Persistent sources, jobs and exports; defaults to `.runtime` |
| `FOOTBALL_YTDLP` | Optional yt-dlp executable path |
| `FOOTBALL_FFMPEG` | Optional absolute FFmpeg path; uses its sibling FFprobe when present, otherwise PATH |
| `FOOTBALL_VAD_MODEL` | Optional path to the pinned Silero ONNX model |
| `HOST`, `PORT` | Listen address and port |
| `DEMO_PASSWORD` | Password for a shared deployment |
| `PUBLIC_ORIGIN` | Exact HTTPS browser origin for a shared deployment |

Keep persistent data separate from the source checkout. Imports preserve original
streams and previous edits. Stopping waits for active workers; restarting does
not submit another generation for an ambiguous or failed request.

Advanced direct Python use of `fal_camera.py` can read `FAL_KEY` from the process
environment or private `.env.local`. That adapter fallback is unavailable to
browser HTTP requests. It is deliberately absent from `.env.example`.

The optional CLI uses `FAL_KEY` explicitly exported in its own terminal process.
It creates a temporary connection, submits the selected saved frame, then removes
that connection. It cannot borrow the browser or server owner's key. With the
local server running and your key already exported:

```sh
.venv/bin/python orbit.py run --video-id YOUR_VIDEO_ID
.venv/bin/python orbit.py status JOB_ID --wait
```

`status` only reads an existing job and needs no key. A shared server with demo
authentication uses the signed-in browser interface instead of this local CLI.

## Checks

```sh
.venv/bin/python doctor.py --model
.venv/bin/python -m unittest discover -v -p 'test_*.py'
node --test test_ui_recovery.js test_ui_remix.js test_ui_live.js test_ui_credentials.js test_ui_orbit_paths.js
node --test ui/live-client/test_controller.mjs
.venv/bin/python build_ui.py
```

Tests use local fixtures and mocked provider calls, without paid Fal or Reactor requests.
Real media tests need the listed tools and include 4K encoding.
For just the camera geometry and selection checks, run
`.venv/bin/python -m unittest -v test_orbit_paths` and
`node --test test_ui_orbit_paths.js`. These checks validate requested positions
and application behavior; they make no paid calls or generated-motion assessment.

Licensed under [MIT](../LICENSE). See [third-party notices](../THIRD_PARTY_NOTICES.md)
for installed tools and the speech-model license. Video rights and provider
accounts are separate from this code license.
