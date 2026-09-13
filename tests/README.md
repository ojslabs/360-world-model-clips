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

For a focused check, use its package path:

```sh
.venv/bin/python -m unittest -v tests.python.test_orbit_paths
node --test tests/js/test_ui_orbit_paths.js
```

[CI](../.github/workflows/ci.yml) also builds the Docker image and runs
`python scripts/container_smoke.py --image world-model-clips:ci` against a fresh
data volume. That check covers startup, shared-host authentication and the empty
editor without provider keys.
