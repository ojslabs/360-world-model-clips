"""Timing and persistence checks for source preparation. No network or model calls."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import media
import server


class MediaTests(unittest.TestCase):
    def test_strict_verification_rejects_decoder_errors_even_with_exit_zero(self):
        result = subprocess.CompletedProcess([], 0, b"decoded frames", b"[aac] Error decoding frame\n")
        with patch.object(media.subprocess, "run", return_value=result):
            self.assertEqual(media.run(["ffmpeg", "-v", "error"]), b"decoded frames")
            with self.assertRaisesRegex(RuntimeError, "Error decoding frame"):
                media.run(["ffmpeg", "-v", "error"], strict_errors=True)

    def _fake_ffmpeg(self, root, modern):
        binary = root / ("modern-ffmpeg" if modern else "legacy-ffmpeg")
        calls = root / "capability-calls.txt"
        option = "-fps_mode[:stream_specifier] mode" if modern else "-vsync sync video sync method"
        binary.write_text(f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                          f"if '-h' in sys.argv:\n Path({str(calls)!r}).open('a').write('help\\n')\n print({option!r})\n"
                          "else:\n print(json.dumps(sys.argv[1:]))\n")
        binary.chmod(0o755)
        return binary, calls

    def test_default_ffmpeg_and_ffprobe_commands_remain_unchanged(self):
        result = subprocess.CompletedProcess([], 0, b"ok", b"")
        with patch.dict(os.environ, {"FOOTBALL_FFMPEG": ""}), \
             patch.object(media.subprocess, "run", return_value=result) as execute:
            media.run(["ffmpeg", "-vsync", "0", "-version"])
            self.assertEqual(execute.call_args.args[0], ["ffmpeg", "-vsync", "0", "-version"])
        with patch.dict(os.environ, {"FOOTBALL_FFMPEG": "/missing/private/ffmpeg"}), \
             patch.object(media.subprocess, "run", return_value=result) as execute:
            media.run(["ffprobe", "-version"])
            self.assertEqual(execute.call_args.args[0], ["ffprobe", "-version"])

    def test_selected_ffmpeg_translates_only_supported_passthrough_and_caches_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            binary, calls = self._fake_ffmpeg(Path(directory), modern=True)
            with patch.dict(os.environ, {"FOOTBALL_FFMPEG": str(binary)}):
                for _ in range(2):
                    args = json.loads(media.run(["ffmpeg", "-vsync", "0", "-i", "source.mp4"]))
                    self.assertEqual(args, ["-fps_mode", "passthrough", "-i", "source.mp4"])
            self.assertEqual(calls.read_text().splitlines(), ["help"])

    def test_selected_legacy_ffmpeg_keeps_its_supported_vsync_option(self):
        with tempfile.TemporaryDirectory() as directory:
            binary, _ = self._fake_ffmpeg(Path(directory), modern=False)
            with patch.dict(os.environ, {"FOOTBALL_FFMPEG": str(binary)}):
                self.assertEqual(json.loads(media.run(["ffmpeg", "-vsync", "0"])), ["-vsync", "0"])

    def test_invalid_ffmpeg_configuration_cannot_fall_back_to_another_binary(self):
        for configured in ("ffmpeg", "/missing/private/ffmpeg"):
            with self.subTest(configured=configured), \
                 patch.dict(os.environ, {"FOOTBALL_FFMPEG": configured}), \
                 patch.object(media.subprocess, "run") as execute:
                with self.assertRaisesRegex(RuntimeError, "absolute path"):
                    media.run(["ffmpeg", "-version"])
                execute.assert_not_called()

    def test_audio_peak_uses_sample_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples = np.full((240, 4000), .01, dtype=np.float32)
            samples[100] = .9
            def decode(*args, **kwargs):
                samples.tofile(root / "analysis.f32")
            with patch.object(media, "run", side_effect=decode):
                events, waveform = media.audio_activity(root / "source.mp4", root)
            self.assertEqual(events[0]["time"], 25.125)
            self.assertEqual(waveform[1]["time"] - waveform[0]["time"], .25)

    def test_fractional_frame_clock_and_required_tail(self):
        fps = 30000 / 1001
        window = media.cut_window(123.456, 900, fps, 4)
        self.assertAlmostEqual(window["freeze_time"] * fps, round(123.456 * fps))
        self.assertAlmostEqual(window["final_seconds"], 18)
        self.assertAlmostEqual(window["source_seconds"], 12)
        for time in (0, 899, float("nan")):
            with self.assertRaises(ValueError):
                media.cut_window(time, 900, fps)

    def test_rolling_captions_do_not_repeat_cue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.vtt"
            path.write_text("WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nTouchdown!\n\n"
                            "00:00:12.000 --> 00:00:14.000\nTouchdown!\nWhat a finish.\n")
            entries = media.captions(path)
            self.assertEqual([e["text"] for e in entries], ["Touchdown!", "What a finish."])
            self.assertEqual(len(media.commentary_events(entries)), 1)

    def test_only_youtube_inputs(self):
        self.assertEqual(media.youtube_id("https://youtu.be/C9sL5j_iUiE?t=5"), server.SEED)
        for value in ("https://youtube.com.evil.example/watch?v=C9sL5j_iUiE",
                      "http://127.0.0.1/video", "../../etc/passwd", "https://user@youtube.com/watch?v=C9sL5j_iUiE"):
            with self.assertRaises(ValueError):
                media.youtube_id(value)

    def test_full_turn_is_not_normalized_to_zero(self):
        request = media.orbit_request("https://example.com/frame.png")
        path = [k["azimuth"] for k in request["camera_trajectory"]]
        self.assertEqual(path[0], 0)
        self.assertEqual(path[-1], 360)
        self.assertIn(180, path)
        self.assertEqual(path, sorted(path))
        self.assertLess(request["camera_trajectory"][-2]["time"], 1)
        self.assertTrue(request["enable_safety_checker"])

    def test_failed_frame_does_not_save_new_time(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "DATA", Path(directory)):
            project = {"media": {"duration": 900, "fps": 30},
                       "candidates": [{"id": "cut1", "freeze_time": 20, "selected": False, "tail_seconds": 4}]}
            media.write_json(server.manifest(server.SEED), project)
            with patch.object(media, "extract_frame", side_effect=RuntimeError("decode failed")):
                with self.assertRaises(RuntimeError):
                    server.frame(server.SEED, "cut1", 30, 4)
            self.assertEqual(server.load_project(server.SEED), project)

    def test_selection_changed_during_analysis_survives(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "DATA", Path(directory)):
            event = {"time": 30, "score": 1, "label": "Touchdown", "cue": "Touchdown", "kind": "commentary"}
            found = media.candidates([event], 900)
            media.write_json(server.manifest(server.SEED), {
                "title": "Football highlights", "media": {"duration": 900}, "candidates": found})
            def analyze_audio(*args):
                server.selection(server.SEED, found[0]["id"], {"selected": True})
                return [], []
            with patch.object(media, "commentary_events", return_value=[event]), \
                 patch.object(media, "audio_activity", side_effect=analyze_audio):
                result = server.analyze(server.SEED)
            self.assertTrue(result["candidates"][0]["selected"])


if __name__ == "__main__":
    unittest.main()
