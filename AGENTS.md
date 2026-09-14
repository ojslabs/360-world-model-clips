# Working on 360° World Model Clips

This repository is self-contained. Build with `python -m scripts.build_ui`; `vendor/reportkit`
owns the base HTML style and `ui/extra.css` owns the editor appearance. Use system
fonts and preserve the content-derived build fingerprint.

Python application code lives in `app/`, tools in `scripts/`, and regression tests
in `tests/python/` and `tests/js/`. `app.runtime_paths.PROJECT_ROOT` owns the
checkout root. Moving a module must never relocate `.runtime`, `.env.local`,
`ui/` or the provider preset. Keep root `server.py` and `bootstrap.py` as the
documented entry points. Product behavior is recorded in [the brief](docs/BRIEF.md).

- `scripts/install.sh` owns the primary Docker-backed installation. It requires
  Docker already installed and running; do not claim it installs Docker, supports
  untested platforms or makes native Python/Node/FFmpeg unnecessary outside that
  container. Download source without changing unrelated checkouts, preserve owned
  data volumes, and report readiness only after HTTP checks pass. The installer
  must not preload provider keys or make paid model requests. Keep native
  `bootstrap.py --run` available as the secondary setup path.
- `config/orbit_preset.json` owns the Fal model, fixed prompt and original Around path.
  `app/orbit_paths.py` derives the Around / Over & under / Diagonal choices. Keep
  duration, resolution and distance consistent with the base preset. Describe
  these as requested trajectories; position keyframes do not verify generated
  motion or pole orientation. Never replace a saved run's path during recovery.
- Never commit credentials, `.env.local`, model weights, source videos, generated
  media or runtime receipts. Keep persistent state under `FOOTBALL_DATA_DIR`.
  The approved documentation visuals are `docs/assets/world-model-preview.png`,
  `docs/assets/make-an-edit.png`, `docs/assets/f1-over-under.gif` and the original
  `docs/assets/orbit-paths.svg` diagram. They use real source/generated frames and
  actual editor captures; do not retouch model output or invent interface states.
  Attribution lives in `docs/assets/README.md`.
  Keep raw source recordings and full generated exports out of Git.
- Bind every browser-paid Fal call, including visual labels, to its ephemeral
  credential. Capture it in the worker and persist only its owner fingerprint in
  both app and provider receipts. Recovery must reject another key and never use
  the server environment for browser-origin runs. Direct CLI run requires its own
  process `FAL_KEY`; status remains read-only.
- Keep account usage as a link to the [Fal usage dashboard](https://fal.ai/dashboard/usage-billing).
  Do not fetch or display billing balances or usage totals in this app.
- Reactor live tokens and saved remixes require the browser's separate eight-hour
  Reactor credential. Capture its key in queued work and persist only its owner
  fingerprint; include that owner in remix cache identity. Keep both providers'
  cookies independent. Revoke Reactor credentials on disconnect, expiry and
  login/logout. Never use the owner's environment key for a browser request or
  write Reactor keys to runtime files, subprocess arguments or logs.
- Preserve original source bytes and timestamps. Browser previews are separate.
  Never silently lower download quality after failure.
- Only titles containing the case-insensitive whole word `football` receive
  captions and audio analysis. Other titles use full-video manual selection.
  Preserve curated candidates and saved frames on reimport.
- Derive timing from `app.media.DEFAULTS`: currently 4 seconds before, 6 seconds of
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
  connected browser's captured credential. `app/remix_catalog.py` owns model and preset
  prompts. Preserve original composites and audio, confine inputs to the owning
  completed run, and deduplicate by source bytes, prompt, model and credential
  owner. Keep separate remix IDs and durable job records; recovery must repair both
  without repeating a session.
  X2 output is native model resolution; do not claim source-frame alignment from
  matching duration or advertise it as 4K preservation.
- Shared-host authentication, same-origin checks, private keys and persistent
  storage belong to the server boundary. Do not expose an unlocked shared instance.
  Browser Fal and Reactor keys start blank and live only in server memory for that
  browser's separate connections, for up to eight hours. Use opaque HttpOnly cookies;
  never write keys to disk or fall back to an owner's environment key for HTTP requests.
- Run `python -m unittest discover -s tests/python -t . -v -p 'test_*.py'`,
  `node --test tests/js/*.js`,
  `node --test ui/live-client/test_controller.mjs`, `python -m scripts.doctor --model`, and
  `python -m scripts.build_ui`. Inspect decoded media for picture/audio changes.
  Focused camera geometry: `python -m unittest -v tests.python.test_orbit_paths`.
  Distinguish mocked provider responses, real local FFmpeg fixtures, browser
  workflows and clean-container installation tests when reporting verification.
  None of these alone verifies a paid model's output or every host platform.

Keep docs limited to built behavior. Record material limits and test results
plainly. Do not add provider credentials or private user history to examples.

Live preview uses one browser-owned session with server-scoped credentials. Keep
readiness polling separate from paid creation. Stop calls disconnect(false), and
the server session cap remains five minutes. Original source audio is optional
and is not synchronized to the delayed live output.
