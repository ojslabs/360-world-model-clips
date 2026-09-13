# Contributing

Bug reports, example clips and small pull requests are welcome. A useful report
includes the step that failed, what you expected, your operating system and the
error shown in the app. Share only footage you can make public, and remove keys
and private account details from logs or screenshots.

## Get the code

```sh
git clone --depth 1 https://github.com/ojslabs/360-world-model-clips.git
cd 360-world-model-clips
python3.12 bootstrap.py
.venv/bin/python server.py
```

The shallow clone contains all current files. Run `git fetch --unshallow` if you
later need the full commit history. For a copy without Git, use
[Download ZIP](https://github.com/ojslabs/360-world-model-clips/archive/refs/heads/main.zip).
See [setup](docs/setup.md) for prerequisites and Docker.

## Find the part you want to change

| Folder | What lives there |
| --- | --- |
| [app/](app/) | Python server, provider connections and video/audio processing |
| [ui/](ui/) | Browser interface and bundled Reactor live client |
| [config/](config/) | Fixed model prompt and camera preset |
| [tests/](tests/) | Python and browser behavior tests |
| [scripts/](scripts/) | Interface build, environment checks and container smoke check |
| [docs/](docs/) | Setup, process, product brief and example attribution |
| [vendor/](vendor/) | Included HTML report kit |

The root `server.py` starts `app.server`. Imports use the `app` package;
application data stays at the checkout's `.runtime/`, or at `FOOTBALL_DATA_DIR`
when set. Keys belong to temporary browser connections. Keep these boundaries
when moving code.

## Check a change

From the repository root, with bootstrap complete:

```sh
.venv/bin/python -m scripts.doctor --model
.venv/bin/python -m unittest discover -s tests/python -t . -v -p 'test_*.py'
node --test tests/js/*.js ui/live-client/test_controller.mjs
node --check ui/app.js
.venv/bin/python -m scripts.build_ui
```

The tests mock Fal and Reactor calls. Media checks use local fixtures and need
FFmpeg, including its 4K encoder support. Real generation is a separate, explicit
test charged to the connected provider account.

Keep changes focused and explain what changed and how you checked it in the pull
request. Read [AGENTS.md](AGENTS.md) for frame, audio, credential and recovery
invariants. The [live-client guide](ui/live-client/README.md) covers rebuilding
the pinned browser SDK bundle.
