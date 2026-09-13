# 360° World Model Clips

Turn a moment from a video into a camera orbit, then return to the original action.
Choose a frame, choose a path and download the finished edit.

**[Download ZIP](https://github.com/ojslabs/360-world-model-clips/archive/refs/heads/main.zip)** ·
[Quick start](#quick-start) · [How it works](docs/how-it-works.md) ·
[Setup and Docker](docs/setup.md) · [MIT license](LICENSE)

![A generated camera orbit around a bicycle rider above an F1 car](docs/assets/f1-orbit.gif)

An Around example from a saved F1 edit. Preview: 640 × 360; generation: 1080P.
[Source and attribution](docs/assets/README.md).

## Quick start

You need **Python 3.12**, **Node.js 22+**, **FFmpeg with FFprobe**, and your own
**Fal API key** for generation. [Install prerequisites](docs/setup.md#prerequisites).
Reactor is optional and uses a separate key. Provider usage is paid through your
accounts; no credits are included.

**[Download the ZIP](https://github.com/ojslabs/360-world-model-clips/archive/refs/heads/main.zip)**,
extract it and open the extracted folder in your terminal. Run:

```sh
python3.12 bootstrap.py --run
```

This installs pinned dependencies, checks the media tools, builds the UI and
starts the app. [Git clone, Linux and Docker options](docs/setup.md).

Open **<http://127.0.0.1:8476>**, paste your key into **Your Fal account** and
click **Connect Fal**. Keys stay in server memory for your browser connection,
for up to eight hours. Disconnect, expiry or a restart clears the connection.
[Credentials and privacy](docs/setup.md#run-locally).

## Make an edit

### 1. Pick a video

Search or paste a YouTube link, preview the video, then import it.

![The selected F1 video open in the preview dialog](docs/assets/pick-video.jpg)

### 2. Save the frame

Scrub to your moment, step forwards or backwards and click **Use this frame**.

![The populated F1 source player with frame selection controls](docs/assets/pick-frame.jpg)

### 3. Generate and download

Choose a path, generate the edit, then review and download it in Outputs.

![A completed F1 edit in Outputs with its download control](docs/assets/review-edit.jpg)

The output shown is an earlier 18-second F1 edit. New edits use:

| Before | Orbit | After |
| --- | --- | --- |
| 4 seconds of source | 6 seconds generated | Up to 10 seconds of source |

The 16:9 MP4 lasts up to 20 seconds. The source's own music, speech and background
sound slow to 0.75x during the orbit and return to normal afterwards. Silent
videos stay silent. Download individual clips or combine finished clips of the
same resolution into a reel. Earlier edits remain available.

## Choose a path

![Around, Over and under, and Diagonal camera paths](docs/assets/orbit-paths.svg)

**Around** is a level loop. **Over & under** requests a vertical loop.
**Diagonal** requests a loop tilted 45 degrees. These paths are relative to the
saved view. Over & under is experimental and may flip at the top or bottom;
Diagonal approximates its tilted circle with keyframes.
[Path details and the fixed prompt](docs/how-it-works.md#camera-paths).

Generation requests Fal's highest supported **1080P** setting. A **4K export**
preserves the source and reference resolution and enlarges the generated orbit.
The first and last orbit frames are matched locally. Subjects can still move
within the generated scene; matching endpoints do not prove a frozen scene or
an accurate full turn. [Result limits](docs/how-it-works.md#result-limits).

## Optional Reactor remixes

Connect your own key in **Your Reactor account** to try a live Day/Night preview
or save a separate remix of a finished clip. Live toggles use the same session;
**Stop** ends it, with a five-minute limit. Original edits remain unchanged.
[Reactor setup and limits](docs/setup.md#optional-reactor-remixes).

## Repository

| Folder | Contents |
| --- | --- |
| [app/](app/) | Python service, provider adapters and video processing |
| [ui/](ui/) | Browser interface and bundled live client |
| [config/](config/) | Shared generation preset |
| [scripts/](scripts/) | Build, setup and maintenance tools |
| [tests/](tests/) | Python and JavaScript checks |
| [docs/](docs/) | Setup, workflow and example attribution |
| [vendor/](vendor/) | Included report-kit assets and licenses |

[How it works](docs/how-it-works.md) · [Development and checks](docs/setup.md#checks) ·
[Contributing](CONTRIBUTING.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)

The code is MIT licensed. Rights to the [F1 example footage](docs/assets/README.md)
are separate. Keep credentials and private recordings out of issues.
