"""Build outside the original tree and require included assets and deterministic output."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class PortableBuildTests(unittest.TestCase):
    def test_copied_build_has_no_parent_repo_or_font_dependency(self):
        source = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "independent-project"
            root.mkdir()
            for directory in ("scripts", "app", "config", "vendor", "ui"):
                shutil.copytree(source / directory, root / directory,
                                ignore=shutil.ignore_patterns("__pycache__", "out", "node_modules"))
            environment = {**os.environ, "PYTHONPATH": ""}
            def build():
                subprocess.run([sys.executable, "-m", "scripts.build_ui"], cwd=root,
                               env=environment, check=True, capture_output=True)
                return (root / "ui/out/standalone.html").read_text()
            first = build()
            self.assertEqual(first, build())
            self.assertIn('name="football-edits-build"', first)
            self.assertIn('src="/app.js?v=', first)
            for absent in ("@font-face", "data:font", "Aeonik", "Suisse", "Pixelify", "/Users/", "Cortex"):
                self.assertNotIn(absent, first)
            self.assertFalse((root.parent / "reports").exists())


if __name__ == "__main__":
    unittest.main()
