# Working on 360° World Model Clips

This repository is self-contained. Build with `python build_ui.py`; `_report_kit`
owns the base HTML style and `ui/extra.css` owns the editor appearance. Use system
fonts and preserve the content-derived build fingerprint.

- `orbit_preset.json` is the sole source for the Fal model, prompt and camera path.
- Never commit credentials, `.env.local`, model weights, source videos, generated
  media or runtime receipts. Keep persistent state under `FOOTBALL_DATA_DIR`.
- Preserve original source bytes and timestamps. Browser previews are separate.
  Never silently lower download quality after failure.
- Only titles containing the case-insensitive whole word `football` receive
  captions and audio analysis. Other titles use full-video manual selection.
  Preserve curated candidates and saved frames on reimport.
- Derive timing from `media.DEFAULTS`: currently 8 seconds before, 6 seconds of
  orbit and 4 seconds after. Require complete source context.
- Preserve native Fal output. Retiming uses the whole timeline. Reference anchoring
  keeps one saved PNG at each endpoint; verify equality after final composition.
  Do not claim this verifies frozen generated subjects or a complete camera turn.
- Keep 4K source/reference detail until final rendering. Record enlargement of
  generated 1080p frames. Combined reels require matching dimensions.
- Preserve source audio timing. The source slow-motion override uses short local
  extraction, smooth speed ramps and no boost. Crowd processing must retain honest
  provenance and must not silently replace a failed extraction with silence.
- Generation, visual labels and import are separate operations. Recovery reads
  acknowledged requests and saved media; it never repeats an ambiguous paid POST.
- Shared-host authentication, same-origin checks, private keys and persistent
  storage belong to the server boundary. Do not expose an unlocked shared instance.
- Run `python -m unittest discover -v -p 'test_*.py'`,
  `node --test test_ui_recovery.js`, `python doctor.py --model`, and
  `python build_ui.py`. Inspect decoded media for picture/audio changes.

Keep docs limited to built behavior. Record material limits and test results
plainly. Do not add provider credentials or private user history to examples.
