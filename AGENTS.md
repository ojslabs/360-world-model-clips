# Working on 360° World Model Clips

This repository is self-contained. Build with `python build_ui.py`; `_report_kit`
owns the base HTML style and `ui/extra.css` owns the editor appearance. Use system
fonts and preserve the content-derived build fingerprint.

- `orbit_preset.json` owns the Fal model, fixed prompt and original Around path.
  `orbit_paths.py` derives the Around / Over & under / Diagonal choices. Keep
  duration, resolution and distance consistent with the base preset. Describe
  these as requested trajectories; position keyframes do not verify generated
  motion or pole orientation. Never replace a saved run's path during recovery.
- Never commit credentials, `.env.local`, model weights, source videos, generated
  media or runtime receipts. Keep persistent state under `FOOTBALL_DATA_DIR`.
  The explicitly approved documentation exceptions are `docs/assets/f1-orbit.gif`
  and `docs/assets/f1-sequence.jpg`, with attribution in `docs/assets/README.md`.
  Keep raw source recordings and full generated exports out of Git.
- Bind every browser-paid Fal call, including visual labels, to its ephemeral
  credential. Capture it in the worker and persist only its owner fingerprint in
  both app and provider receipts. Recovery must reject another key and never use
  the server environment for browser-origin runs. Direct CLI run requires its own
  process `FAL_KEY`; status remains read-only.
- Preserve original source bytes and timestamps. Browser previews are separate.
  Never silently lower download quality after failure.
- Only titles containing the case-insensitive whole word `football` receive
  captions and audio analysis. Other titles use full-video manual selection.
  Preserve curated candidates and saved frames on reimport.
- Derive timing from `media.DEFAULTS`: currently 4 seconds before, 6 seconds of
  orbit and up to 10 seconds after, at most 20 seconds total. Require the full lead;
  cap the tail at the remaining video from the next source frame. A selection on
  the last frame has no tail. Preserve historical output timing and metadata.
- Preserve native Fal output. Retiming uses the whole timeline. Reference anchoring
  keeps one saved PNG at each endpoint; verify equality after final composition.
  Do not claim this verifies frozen generated subjects or a complete camera turn.
- Keep 4K source/reference detail until final rendering. Record enlargement of
  generated 1080p frames. Combined reels require matching dimensions.
- Every new output automatically loops the source's own pre-freeze audio mix under
  the orbit. Ease speed and pitch from 1x to 0.75x to 1x; retain music, speech and
  background noise without volume boost. Preserve source audio timing outside the
  orbit. Genuine silence and a verified missing audio stream stay silent; pad clean
  audio EOF and report probe/decode failures. No source override, speech separation,
  full-video analysis or provider call is needed for this local effect. Keep earlier
  audio methods and their receipts intact when producing a revised output.
- Generation, visual labels and import are separate operations. Recovery reads
  acknowledged requests and saved media; it never repeats an ambiguous paid POST.
- Reactor X2 remixes are an optional operation on completed highlights, using the
  server-only `REACTOR_API_KEY`. `remix_catalog.py` owns model and preset prompts.
  Preserve original composites and audio, confine inputs to the owning completed
  run, and deduplicate by source bytes, prompt and model. Keep separate remix IDs
  and durable job records; recovery must repair both without repeating a session.
  X2 output is native model resolution; do not claim source-frame alignment from
  matching duration or advertise it as 4K preservation.
- Shared-host authentication, same-origin checks, private keys and persistent
  storage belong to the server boundary. Do not expose an unlocked shared instance.
  Browser Fal keys start blank and live only in server memory for that browser's
  connection, for up to eight hours. Use an opaque HttpOnly cookie; never write
  keys to disk or fall back to an owner's environment key for HTTP requests.
- Run `python -m unittest discover -v -p 'test_*.py'`,
  `node --test test_ui_recovery.js test_ui_remix.js test_ui_live.js test_ui_credentials.js test_ui_orbit_paths.js`,
  `node --test ui/live-client/test_controller.mjs`, `python doctor.py --model`, and
  `python build_ui.py`. Inspect decoded media for picture/audio changes.
  Focused camera geometry: `python -m unittest -v test_orbit_paths`.

Keep docs limited to built behavior. Record material limits and test results
plainly. Do not add provider credentials or private user history to examples.

Live preview uses one browser-owned session with server-scoped credentials. Keep
readiness polling separate from paid creation. Stop calls disconnect(false), and
the server session cap remains five minutes. Original source audio is optional
and is not synchronized to the delayed live output.
