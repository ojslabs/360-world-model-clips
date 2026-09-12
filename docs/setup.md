# Setup and development

Import a YouTube video, choose the exact frame, generate an H3 Max camera orbit
through Fal, and export it between the surrounding source action.

- Full-source playback and frame-by-frame selection.
- Highest available source resolution, with separate browser-compatible previews.
- An 18-second edit: 8 seconds before the freeze, 6 seconds of orbit, 4 seconds after.
- 1080p or 4K output. The 4K source and reference retain their detail; Fal's 1080p
  generated interior is enlarged to fit.
- Matching local reference endpoints, original action/audio resumption, individual
  outputs and combined reels of matching resolution.
- Saved jobs and local recovery without automatically repeating paid requests.

The exact Fal prompt and camera path live in [orbit_preset.json](../orbit_preset.json).
Local endpoint equality does not prove that generated subjects remain frozen or
that every camera turn looks correct. Review the result before sharing it.

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

Set `FAL_KEY` in your server environment, or put it in `.env.local`:

```dotenv
FAL_KEY=your-own-fal-key
```

Keep that file private. Then run:

```sh
.venv/bin/python server.py
```

Open <http://127.0.0.1:8476>. Import and review work locally; generating an orbit
and requesting a visual action label use your Fal account. API keys stay on the
server. The code includes no credits, sample recordings or existing outputs.

## Use

1. Search for a video or paste its YouTube link, then import it.
2. Scrub the full video and click **Use this frame** on the desired moment.
3. Generate the orbit, review the completed edit, and download it.

### Optional Reactor remixes

Add `REACTOR_API_KEY` to the server environment or private `.env.local` file to
enable Reactor X2 edits of finished highlights. Shared Docker deployments can
include it in the private `.env` file. It is separate from `FAL_KEY`; neither key
belongs in the browser, repository or image build arguments.

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

Titles containing the whole word `football`, ignoring case, receive suggestions
from caption cues and audio energy. Other titles skip captions and audio analysis
and open manual selection. A loud audio peak is not a confirmed crowd detection.
Download progress reports the current video or audio stream, followed by named
verification and preview stages. Estimates remain unknown when yt-dlp omits them.

Default orbit audio uses checked stereo source atmosphere. If a source cannot
provide suitable atmosphere, assembly asks for a configured recording; no borrowed
fallback recording is shipped. A source can explicitly use its own pre-freeze
audio by adding an entry to the data directory's `audio-overrides.json`:

```json
{"YOUR_VIDEO_ID": "source_slow_motion"}
```

This loops 1.75 seconds immediately before the freeze, easing speed and pitch
from 1x to 0.75x and back over a 6.5-second bed. It does not remove speech.
Original source audio remains synchronized before and after the orbit.
The included F1 example (`8o40mSS05iE`) already uses this override through
`config/audio-overrides.json`; your data-directory settings take precedence.

## Docker

The Dockerfile installs Python 3.12, Node.js 24, FFmpeg with fast AV1 decoding, pinned
Python dependencies and the verified speech model. Model/tool files live outside
the writable `/data` volume. It runs as a non-root user.

Copy `.env.example` to `.env` and fill in your Fal key, a password of at least
16 characters, and the browser origin. For a local protected Docker check use
`PUBLIC_ORIGIN=http://localhost:8476`; open that exact address after starting it.

```sh
docker build -t world-model-clips .
docker run --rm --env-file .env \
  -p 127.0.0.1:8476:8476 \
  -v world-model-clips-data:/data \
  world-model-clips
```

For a shared host, supply `FAL_KEY`, `DEMO_PASSWORD` and the exact HTTPS
`PUBLIC_ORIGIN` in the private environment file. Terminate HTTPS at a reverse
proxy forwarding to port 8476. The shared password gates the same workspace and
Fal account; this is not a multi-user tenant service. Public hosting requirements
are enforced by the server configuration.

Run the image build and container checks in the deployment environment before
publishing a host. Docker was not available on the development machine.

## Configuration

| Variable | Purpose |
| --- | --- |
| `FAL_KEY` | Server-side Fal API key |
| `REACTOR_API_KEY` | Optional server-side Reactor key for X2 remixes |
| `FOOTBALL_DATA_DIR` | Persistent sources, jobs and exports; defaults to `.runtime` |
| `FOOTBALL_YTDLP` | Optional yt-dlp executable path |
| `FOOTBALL_FFMPEG` | Optional absolute FFmpeg path; FFprobe remains on PATH |
| `FOOTBALL_VAD_MODEL` | Optional path to the pinned Silero ONNX model |
| `HOST`, `PORT` | Listen address and port |
| `DEMO_PASSWORD` | Password for a shared deployment |
| `PUBLIC_ORIGIN` | Exact HTTPS browser origin for a shared deployment |

Keep persistent data separate from the source checkout. Imports preserve original
streams and previous edits. Stopping waits for active workers; restarting does
not submit another generation for an ambiguous or failed request.

## Checks

```sh
.venv/bin/python doctor.py --model
.venv/bin/python -m unittest discover -v -p 'test_*.py'
node --test test_ui_recovery.js test_ui_remix.js test_ui_live.js
.venv/bin/python build_ui.py
```

Tests use local fixtures and mocked provider calls, without paid Fal or Reactor requests.
Real media tests need the listed tools and include 4K encoding.

Licensed under [MIT](../LICENSE). See [third-party notices](../THIRD_PARTY_NOTICES.md)
for installed tools and the speech-model license. Video rights and provider
accounts are separate from this code license.
