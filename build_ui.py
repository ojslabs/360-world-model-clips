"""Build the editor with the included OJS Labs HTML report kit."""
import hashlib
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from _report_kit.report_kit import build


def stamp_ui(html, script):
    """Version the delivered HTML and script together without a changing clock."""
    version = hashlib.sha256(html.encode("utf-8") + b"\0" + script).hexdigest()
    marker = f'<meta name="football-edits-build" content="{version}">'
    if "</head>" not in html or 'src="/app.js"' not in html:
        raise ValueError("The app build needs its document head and script entry point.")
    return html.replace("</head>", marker + "\n</head>", 1).replace(
        'src="/app.js"', f'src="/app.js?v={version}"'), version


def build_ui():
    body = build(ROOT / "ui", title="360° World Model Clips")
    target = ROOT / "ui" / "out" / "standalone.html"
    stamped, _ = stamp_ui(target.read_text(), (ROOT / "ui" / "app.js").read_bytes())
    descriptor, name = tempfile.mkstemp(prefix=".ui-build-", suffix=".html", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(stamped)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return body


if __name__ == "__main__":
    build_ui()
