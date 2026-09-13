# Tests

Run from the repository root after [setup](../docs/setup.md):

```sh
.venv/bin/python -m unittest discover -s tests/python -t . -v
node --test tests/js/*.js ui/live-client/test_controller.mjs
node --check ui/app.js
.venv/bin/python -m scripts.build_ui
```

Python tests live in `tests/python`; browser behavior tests live in `tests/js`.
The live client keeps its controller test beside that module. Tests use local
fixtures and mocked providers, including real FFmpeg checks for decoding, audio,
4K output and matching reference frames. They make no paid provider calls.

`test_end_to_end.py` follows one edit through the real HTTP API: import, frame
selection, browser credential connection, generation, audio and video assembly,
then full and partial MP4 download. It replaces remote video transport and model
responses with local fixtures. FFmpeg, file storage and job recovery stay real.

For a focused check, use its package path:

```sh
.venv/bin/python -m unittest -v tests.python.test_orbit_paths
node --test tests/js/test_ui_orbit_paths.js
```

[CI](../.github/workflows/ci.yml) also builds the Docker image and runs
`python scripts/container_smoke.py --image world-model-clips:ci` against a fresh
data volume. That check covers startup, shared-host authentication and the empty
editor without provider keys.

For published commits, CI also runs `scripts.install_smoke` on fresh x86 and ARM
Linux runners. It downloads the public installer and source, builds the image,
checks login and both blank provider connections, runs the end-to-end fixture
inside the installed image, then verifies that a restart preserves data. Python,
Node, FFmpeg and Git are absent from the installer's host PATH. Docker remains
available, as required by the [quick start](../README.md#quick-start).

To check an already published commit on a machine with Docker:

```sh
python3 -m scripts.install_smoke --ref FULL_COMMIT_SHA
```

These checks verify installation and application behavior. They do not spend
provider credits or verify the appearance of a new model generation.
