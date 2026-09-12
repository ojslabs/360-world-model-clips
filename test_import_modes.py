"""Title routing avoids analysis work without losing curated source selections."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media
import server


class ImportModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = patch.object(server, "DATA", Path(temporary.name))
        state.start()
        self.addCleanup(state.stop)
        self.video_id = server.SEED
        self.location = server.folder(self.video_id)
        self.location.mkdir(parents=True)
        (self.location / "source.mkv").write_bytes(b"preserved original")

    def project(self, title="Skateboard street run", duration=100):
        project = {"id": self.video_id, "title": title, "source_file": "source.mkv",
                   "media": {"duration": duration, "fps": 30000 / 1001},
                   "candidates": [], "waveform": [], "video_url": "/full-source.mp4"}
        media.write_json(server.manifest(self.video_id), project)
        media.write_json(self.location / "source.info.json", {"title": title})
        return project

    def test_football_requires_case_insensitive_whole_word(self):
        for title in ("American FOOTBALL highlights", "Football: week 2", "pre-football warmup"):
            self.assertTrue(server.football_title(title), title)
        for title in ("footballers", "NFL best plays", "Skateboarding", "foofootball", "", None):
            self.assertFalse(server.football_title(title), title)

    def test_nonfootball_import_skips_captions_audio_and_detection_but_keeps_verification(self):
        self.project()
        events = []
        with patch.object(media, "run", side_effect=AssertionError("No caption download")), \
             patch.object(media, "captions", side_effect=AssertionError("No caption parsing")), \
             patch.object(media, "audio_activity", side_effect=AssertionError("No full-source audio scan")), \
             patch.object(media, "candidates", side_effect=AssertionError("No cut-up detection")), \
             patch.object(server, "verify_source") as verify:
            result = server.import_video(self.video_id, playback_capabilities={"av1_mp4": True},
                                         on_progress=events.append)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(verify.call_args.kwargs["playback_capabilities"], {"av1_mp4": True})
        self.assertEqual(result["selection_mode"], "manual")
        candidate = result["candidates"][0]
        self.assertEqual((candidate["window_start"], candidate["window_end"]), (0, 100))
        self.assertEqual(candidate["signals"], [])
        self.assertGreaterEqual(candidate["freeze_time"], media.DEFAULTS["lead_seconds"])
        window = media.cut_window(candidate["freeze_time"], 100, result["media"]["fps"])
        self.assertAlmostEqual(window["freeze_time"], candidate["freeze_time"])
        self.assertEqual(events[-1]["stage"], "complete")
        self.assertTrue(all(event["elapsed_seconds"] >= 0 for event in events))
        self.assertEqual((self.location / "source.mkv").read_bytes(), b"preserved original")

    def test_manual_reimport_preserves_curated_candidates_and_saved_manual_frame(self):
        project = self.project()
        curated = {"id": "curated", "selected": True, "freeze_time": 23.023,
                   "frame_url": "/saved.png", "tail_seconds": media.DEFAULTS["tail_seconds"]}
        project["candidates"] = [curated]
        project["analysis"] = {"caption_entries": 10}
        media.write_json(server.manifest(self.video_id), project)
        first = server.manual_project(self.video_id)
        self.assertEqual(first["candidates"][0], curated)
        self.assertEqual(first["previous_analysis"], project["analysis"])
        first["candidates"][1].update(freeze_time=41.041, frame_url="/manual.png", selected=True)
        media.write_json(server.manifest(self.video_id), first)
        again = server.manual_project(self.video_id)
        self.assertEqual(again["candidates"], first["candidates"])

    def test_short_manual_video_remains_viewable_without_an_invalid_freeze_candidate(self):
        self.project(duration=media.DEFAULTS["lead_seconds"] + media.DEFAULTS["tail_seconds"])
        result = server.manual_project(self.video_id)
        self.assertEqual(result["video_url"], "/full-source.mp4")
        self.assertFalse(result["analysis"]["generation_available"])
        self.assertEqual(result["candidates"], [])

    def test_football_import_retains_caption_and_audio_analysis(self):
        self.project("American FOOTBALL replay")
        events = []
        with patch.object(server, "verify_source"), patch.object(media, "run") as captions, \
             patch.object(media, "audio_activity", return_value=([], [])) as audio:
            result = server.import_video(self.video_id, on_progress=events.append)
        self.assertIn("--write-auto-subs", captions.call_args.args[0])
        audio.assert_called_once()
        self.assertEqual(result["analysis"]["mode"], "football")
        self.assertIn("fetching_captions", [event["stage"] for event in events])
        self.assertIn("analyzing", [event["stage"] for event in events])


if __name__ == "__main__":
    unittest.main()
