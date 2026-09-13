"""Detect a present but unsupported JavaScript runtime before YouTube import."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts import doctor
from app import media


class DoctorTests(unittest.TestCase):
    def test_installed_node_must_meet_the_pinned_downloader_requirement(self):
        for version, expected in (("v20.19.0", False), ("v22.0.0", True), ("v24.16.0", True)):
            with self.subTest(version=version):
                def run(command, **kwargs):
                    value = version if command[-1] == "--version" else "ffmpeg test fixture"
                    return subprocess.CompletedProcess(command, 0, value, "")
                with patch.dict(os.environ, {"FOOTBALL_FFMPEG": sys.executable,
                                             "FOOTBALL_YTDLP": sys.executable}), \
                     patch.object(doctor.shutil, "which", return_value=sys.executable), \
                     patch.object(doctor.subprocess, "run", side_effect=run), \
                     patch.object(media, "run", return_value=b"libdav1d libx264 zscale"):
                    result = doctor.check_tools()
                self.assertEqual(result["ok"], expected)
                if not expected:
                    self.assertIn("Node.js 22.0.0", " ".join(result["errors"]))


if __name__ == "__main__":
    unittest.main()
