# 360° World Model Clips

Pick a frame from a video and move the camera around that moment. This app calls
Fal's H3 Max camera-controls model, then places the generated orbit between the
original action before and after your selection.

![A generated camera orbit around a bicycle rider above an F1 car](docs/assets/f1-orbit.gif)

Six seconds from an actual saved edit. Source: Red Bull's
[Jumping Over A Moving F1 Car (world first)](https://www.youtube.com/watch?v=8o40mSS05iE&t=586).
The preview is reduced to 640 × 360; the app requested Fal's 1080P output.
[Example details and attribution](docs/assets/README.md).

[Run it locally](#run-it-locally) · [Setup and Docker](docs/setup.md) ·
[Exact prompt and custom camera path](orbit_preset.json)

## Make your first edit

Open the app and connect your own Fal key in the main page. The key field starts
blank. Search for a YouTube video or paste its link, import it, then scrub to the
moment you want. The frame controls let you move forwards or backwards before
clicking **Use this frame**.

Click **Generate** when the frame is right. The finished edit appears in Outputs:

| Original action | Generated orbit | Original action resumes |
| --- | --- | --- |
| 8 seconds before your frame | 6 seconds around the frozen reference | 4 seconds after your frame |

You get an 18-second, 16:9 MP4 with audio. Download clips individually or combine
finished clips of the same resolution into a reel. Previous edits stay available.
Search, playback and choosing a frame do not submit a video generation request.

## How the H3 Max orbit works

The custom part is the camera path in [orbit_preset.json](orbit_preset.json).
The actual Fal endpoint is
[`minimax/h3-max/camera-controls`](https://fal.ai/models/minimax/h3-max/camera-controls),
which Fal names H3 Max Camera Controls / Multi Angle. The app uses that published
endpoint directly.

1. The importer keeps the highest available source streams and their resolution.
   If your browser needs another format, the app makes a separate preview. The
   original remains the source for frame extraction and export. Titles containing
   the whole word `football` also receive caption and audio-activity suggestions;
   other videos open in manual selection without that analysis.

2. **Use this frame** saves the selected source timestamp. Generation takes a
   copy of the exact source frame as a PNG and saves the request beside it. The
   moving video is used later for the edit; Fal receives the still image.

3. The server sends that image with the preset's fixed prompt and ten camera
   keyframes. The prompt asks the scene to stay frozen while the camera moves.
   The requested path holds its starting angle for 0.2 seconds, eases around one full turn
   by 5.3 seconds, and holds the final angle for the remaining 0.7 seconds.
   Elevation stays at zero and distance stays at one. Every new run uses this
   same preset; it does not depend on an assistant rewriting the request.

4. Fal processes the queued request. The app shows progress, saves the provider's
   response and downloads the original generated video. It retains the complete
   returned timeline when adjusting the delivery to six seconds at 30 fps.
   The saved request ID lets recovery check the existing run without silently
   purchasing another generation.

5. A local edit inserts the saved PNG at the first and last orbit frames, with
   a direct cut and no added fade or hold. The app checks that those two decoded
   frames still match in the finished MP4.

6. FFmpeg joins the source lead-in, orbit and source continuation. Both picture
   and original audio resume on the next source frame. In the F1 example, the
   engine sound underneath the orbit comes from the 1.75 seconds just before the
   selected moment. Its speed and pitch ease down to 0.75×, then return to normal
   as the original action resumes.

![Four views from the same F1 orbit: reference, first quarter, halfway and third quarter](docs/assets/f1-sequence.jpg)

These views come from one output at 0, 1.5, 3 and 4.5 seconds into its orbit.

### What to expect from the result

The preset requests six seconds, `1080P` output and `balanced` prompt expansion.
Fal may expand the submitted text before generation. Its
[schema](https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=minimax/h3-max/camera-controls)
describes 1080P as latent refinement from a 768P source. For a 4K source, the app's
4K export keeps the original source and reference detail, then enlarges the
generated frames to fit.

The schema accepts one reference image and has no ending-image input. The matching
endpoints come from the local edit above. H3 can still change a rider's pose,
shift the background or return with different framing. A matching first and last
frame does not prove that the generated subject stayed frozen or that the
intervening camera move is physically accurate.

For other videos, orbit audio uses checked source atmosphere. A source-specific
override can use the same slow-motion audio treatment as the F1 demo. The
[audio setup](docs/setup.md#orbit-audio) explains both paths.

## Run it locally

You need Python 3.12, Node.js 22 or newer, and FFmpeg with FFprobe. On macOS:

```sh
brew install python@3.12 ffmpeg node
git clone https://github.com/ojslabs/360-world-model-clips.git
cd 360-world-model-clips
python3.12 bootstrap.py
.venv/bin/python server.py
```

Open <http://127.0.0.1:8476> and paste your own Fal key into **Your Fal account**.
Click **Connect Fal**. The server holds the key in memory for your browser session,
for up to eight hours. It is not saved to disk or preloaded into the page.
Disconnect to remove it; reconnect after a server restart. Fal usage is charged
to your account.

Bootstrap installs the pinned Python dependencies, checks the media tools,
downloads the verified speech model and builds the interface. See
[setup](docs/setup.md) for Linux, Docker and shared-host configuration.

## Optional live Day/Night preview

Step 4 connects a finished clip to Reactor X2. Start the live preview and switch
Day or Night while the same session streams edited frames back to the player.
It stops after five minutes, or when you click Stop. The optional source audio is
not synchronized to the delayed picture. Saved file remixes are also available
under **Saved remixes**.

Reactor needs its own server-side key and credits. It is separate from the Fal
orbit generation and keeps your original finished clips. See
[Reactor setup](docs/setup.md#optional-reactor-remixes).

If you find this useful, star the repository. Examples, bug reports and small
pull requests are welcome, especially clips that expose a bad camera return.
Keep credentials and private recordings out of issues.

The code is [MIT licensed](LICENSE). The F1 demonstration uses third-party footage;
its rights are separate from the code license. See the
[example attribution](docs/assets/README.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
