"""Offline quality selection and native-source/compatible-preview separation."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import yt_dlp

import media
import download_progress
import server


class SourceImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = patch.object(server, "DATA", self.root)
        state.start()
        self.addCleanup(state.stop)
        self.video_id = "C9sL5j_iUiE"
        self.location = server.folder(self.video_id)
        self.location.mkdir(parents=True)

    def test_real_downloader_selector_prioritizes_resolution_then_fps_across_codecs(self):
        formats = [
            {"format_id": "avc1080", "width": 1920, "height": 1080, "fps": 60,
             "vcodec": "avc1.64002a", "acodec": "none", "ext": "mp4", "preference": 100},
            {"format_id": "vp9-4k30", "width": 3840, "height": 2160, "fps": 30,
             "vcodec": "vp9", "acodec": "none", "ext": "webm"},
            {"format_id": "av1-4k60", "width": 3840, "height": 2160, "fps": 60,
             "vcodec": "av01.0.12M.08", "acodec": "none", "ext": "mp4"},
            {"format_id": "aac", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "ext": "m4a"},
            {"format_id": "audio-opus", "vcodec": "none", "acodec": "opus", "abr": 160, "ext": "webm"},
        ]
        for item in formats:
            item["url"] = "https://example.invalid/" + item["format_id"]
        with yt_dlp.YoutubeDL({"quiet": True, "format": media.SOURCE_FORMAT,
                               "format_sort": media.SOURCE_SORT.split(","),
                               "format_sort_force": True}) as downloader:
            selected = downloader.process_ie_result(
                {"id": "fixture", "title": "Offline", "formats": formats}, download=False)
        self.assertEqual(selected["format_id"], "av1-4k60+audio-opus")
        self.assertEqual((selected["width"], selected["height"], selected["fps"]), (3840, 2160, 60))

    def test_new_download_preserves_selected_original_without_format_retry(self):
        native = b"highest-quality-original"
        details = {"width": 3840, "height": 2160, "fps": 60}
        calls = []
        def downloaded(command, **kwargs):
            calls.append(command)
            target = self.location / "import-original"
            (target / "source.mkv").write_bytes(native)
            media.write_json(target / "source.info.json", {**details, "format_id": "av1-4k60+opus"})
        (self.location / "captions.en.vtt").write_text("WEBVTT\n")
        with patch.object(download_progress, "run", side_effect=downloaded), \
             patch.object(media, "probe", return_value=details), \
             patch.object(media, "verify_source_decode"), \
             patch.object(server, "verify_source"), patch.object(server, "analyze", return_value={}):
            server.import_video(self.video_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual((self.location / "source.mkv").read_bytes(), native)
        self.assertEqual(server.source_path(self.video_id).name, "source.mkv")
        self.assertEqual(media.read_json(self.location / "source-quality.json")["selection"], "best_available")
        self.assertIn("--abort-on-unavailable-fragments", calls[0])

    def test_failed_best_download_does_not_try_a_lower_resolution(self):
        with patch.object(download_progress, "run", side_effect=RuntimeError("Selected format unavailable")) as download:
            with self.assertRaisesRegex(RuntimeError, "Selected format unavailable"):
                server.import_video(self.video_id)
        self.assertEqual(download.call_count, 1)
        self.assertFalse((self.location / "source.mkv").exists())

    def test_browser_av1_capability_is_explicit_and_reaches_preview(self):
        self.assertEqual(server.playback_capability_options(None), {"av1_mp4": False})
        for value in ({"av1_mp4": "true"}, {"av1_mp4": 1}, True):
            with self.assertRaisesRegex(ValueError, "boolean"):
                server.playback_capability_options(value)
        native = self.location / "source.mkv"
        native.write_bytes(b"original")
        project = {"media": {"width": 3840, "height": 2160, "fps": 30000 / 1001}}
        preview = {"path": str(self.location / "source-preview-av1.mp4"), "media": project["media"]}
        with patch.object(server, "prepare_source_project", return_value=project), \
             patch.object(media, "browser_preview", return_value=preview) as create, \
             patch.object(server, "update_project", return_value={}):
            server.verify_source(self.video_id, playback_capabilities={"av1_mp4": True})
        create.assert_called_once_with(native, self.location, project["media"], av1_mp4=True)

    def test_completed_staging_recovers_nominal_ntsc_source_without_downloader(self):
        pending = self.location / "import-original"
        pending.mkdir()
        original = b"completed AV1 original preserved exactly"
        (pending / "source.mkv").write_bytes(original)
        media.write_json(pending / "source.info.json", {
            "format_id": "401+251", "width": 3840, "height": 2160, "fps": 30})
        (self.location / "captions.en.vtt").write_text("WEBVTT\n")
        details = {"width": 3840, "height": 2160, "fps": 30000 / 1001}
        with patch.object(media, "run", side_effect=AssertionError("No downloader call")) as downloader, \
             patch.object(media, "probe", return_value=details), patch.object(media, "verify_source_decode"), \
             patch.object(server, "verify_source"), patch.object(server, "analyze", return_value={}):
            server.import_video(self.video_id)
        downloader.assert_not_called()
        self.assertEqual((self.location / "source.mkv").read_bytes(), original)
        receipt = media.read_json(self.location / "source-quality.json")
        self.assertEqual(receipt["frame_rate_check"], {
            "selected_fps": 30, "measured_fps": 30000 / 1001, "comparison": "nominal_ntsc"})

    def test_selection_validation_accepts_ntsc_label_but_rejects_real_quality_loss(self):
        expected = {"width": 3840, "height": 2160, "fps": 30}
        for nominal in (24, 30, 60, 120):
            with self.subTest(nominal=nominal):
                receipt = media.validate_source_selection({**expected, "fps": nominal},
                    {**expected, "fps": nominal * 1000 / 1001})
                self.assertEqual(receipt["comparison"], "nominal_ntsc")
        for actual in ({**expected, "fps": 25}, {**expected, "fps": 29},
                       {**expected, "fps": 29.9}, {**expected, "width": 1920, "height": 1080}):
            with self.subTest(actual=actual), self.assertRaisesRegex(RuntimeError, "selected quality"):
                media.validate_source_selection(expected, actual)
        with self.assertRaisesRegex(RuntimeError, "frame rate"):
            media.validate_source_selection({**expected, "fps": 60}, {**expected, "fps": 30})

    def test_selected_video_stream_metadata_takes_precedence_over_merged_summary(self):
        metadata = {"fps": 30, "requested_formats": [
            {"vcodec": "none", "fps": None},
            {"vcodec": "av01", "width": 3840, "height": 2160, "fps": 60}]}
        with self.assertRaisesRegex(RuntimeError, "frame rate"):
            media.validate_source_selection(metadata, {"width": 3840, "height": 2160, "fps": 30})
        self.assertEqual(media.validate_source_selection(metadata,
            {"width": 3840, "height": 2160, "fps": 60000 / 1001})["comparison"], "nominal_ntsc")

    def test_incomplete_staging_metadata_never_replaces_original_with_new_download(self):
        pending = self.location / "import-original"
        pending.mkdir()
        cached = pending / "source.mkv"
        cached.write_bytes(b"preserved original without its receipt")
        with patch.object(media, "run", side_effect=AssertionError("No replacement download")):
            with self.assertRaisesRegex(RuntimeError, "metadata is missing"):
                server.import_video(self.video_id)
        self.assertEqual(cached.read_bytes(), b"preserved original without its receipt")

    def test_cached_legacy_source_is_never_redownloaded_or_replaced(self):
        original = self.location / "source.mp4"
        original.write_bytes(b"existing imported original")
        (self.location / "captions.en.vtt").write_text("WEBVTT\n")
        with patch.object(media, "run", side_effect=AssertionError("No downloader call")), \
             patch.object(server, "verify_source"), patch.object(server, "analyze", return_value={}):
            server.import_video(self.video_id)
        self.assertEqual(original.read_bytes(), b"existing imported original")
        self.assertEqual(server.source_path(self.video_id), original)
        self.assertFalse((self.location / "source-quality.json").exists())

    def test_project_frame_extraction_uses_native_not_preview(self):
        original = self.location / "source.mkv"
        original.write_bytes(b"4K source")
        media.write_json(server.manifest(self.video_id), {
            "id": self.video_id, "source_file": original.name, "video_url": "/source-preview.mp4",
            "media": {"duration": 100, "fps": 60, "width": 3840, "height": 2160},
            "candidates": [{"id": "cut1", "time": 20, "selected": True}]})
        def extract(source, timestamp, target):
            self.assertEqual(source, original)
            Image.new("RGB", (3840, 2160), "red").save(target)
        with patch.object(media, "extract_frame", side_effect=extract):
            server.frame(self.video_id, "cut1", 20, media.DEFAULTS["tail_seconds"])
        with Image.open(self.location / "frames/cut1.png") as frame:
            self.assertEqual(frame.size, (3840, 2160))
        self.assertEqual(server.public_project(self.video_id)["source_quality"]["selection"], "existing_unverified")


class SourceMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        cls.four_k = cls.root / "source.mkv"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                   "color=blue:size=3840x2160:rate=60", "-f", "lavfi", "-i",
                   "sine=frequency=440:sample_rate=48000", "-t", "5.1",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                   "-c:a", "libopus", cls.four_k], timeout=90)

    def test_4k_preview_and_extracted_frame_preserve_size_fps_and_original(self):
        before = hashlib.sha256(self.four_k.read_bytes()).hexdigest()
        details = media.probe(self.four_k)
        preview = media.browser_preview(self.four_k, self.root, details)
        self.assertEqual(preview["kind"], "transcode")  # Audio only; video is copied.
        self.assertEqual((preview["media"]["width"], preview["media"]["height"]), (3840, 2160))
        self.assertAlmostEqual(preview["media"]["fps"], 60, places=1)
        self.assertEqual(preview["media"]["audio_codec"], "aac")
        self.assertEqual(hashlib.sha256(self.four_k.read_bytes()).hexdigest(), before)
        target = self.root / "full-size-frame.png"
        media.extract_frame(self.four_k, 1, target)
        with Image.open(target) as image:
            self.assertEqual(image.size, (3840, 2160))
        cut = self.root / "full-size-cut.mp4"
        media.source_cut(self.four_k, {"start": 1, "source_seconds": .5}, cut)
        raw = __import__("json").loads(media.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", cut]))
        video = next(v for v in raw["streams"] if v["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"], video["avg_frame_rate"]), (3840, 2160, "60/1"))

    def test_vp9_transcode_keeps_full_dimensions_frame_rate_and_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            original = directory / "source.mkv"
            media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                       "testsrc2=size=320x180:rate=60", "-f", "lavfi", "-i", "sine=frequency=220",
                       "-t", "5.1", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
                       "-c:a", "libopus", original], timeout=90)
            before = original.read_bytes()
            result = media.browser_preview(original, directory, media.probe(original))
            self.assertEqual(result["media"]["video_codec"], "h264")
            self.assertEqual((result["media"]["width"], result["media"]["height"], result["media"]["fps"]),
                             (320, 180, 60))
            self.assertEqual(original.read_bytes(), before)
            with patch.object(media, "run", side_effect=AssertionError("No remux/transcode needed")):
                direct = media.browser_preview(Path(result["path"]), directory, result["media"])
            self.assertEqual(direct["kind"], "original")

    def test_av1_capable_browser_copies_video_and_preserves_distinct_h264_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            original = directory / "source.mkv"
            media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                       "color=blue:size=160x90:rate=24", "-f", "lavfi", "-i", "sine=frequency=220",
                       "-t", "5.1", "-c:v", "libaom-av1", "-cpu-used", "8", "-crf", "40", "-b:v", "0",
                       "-c:a", "libopus", original], timeout=90)
            before = original.read_bytes()
            details = media.probe(original)
            copied = media.browser_preview(original, directory, details, av1_mp4=True)
            self.assertEqual(Path(copied["path"]).name, "source-preview-av1.mp4")
            self.assertEqual(copied["media"]["video_codec"], "av1")
            self.assertTrue(copied["video_bitstream_preserved"])
            def video_hash(path):
                return media.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0",
                                  "-c:v", "copy", "-f", "hash", "-hash", "sha256", "-"])
            self.assertEqual(video_hash(original), video_hash(copied["path"]))
            copied_bytes = Path(copied["path"]).read_bytes()
            fallback = media.browser_preview(original, directory, details)
            self.assertEqual(Path(fallback["path"]).name, "source-preview.mp4")
            self.assertEqual(fallback["media"]["video_codec"], "h264")
            self.assertFalse(fallback["video_bitstream_preserved"])
            self.assertEqual(Path(copied["path"]).read_bytes(), copied_bytes)
            self.assertEqual(original.read_bytes(), before)
            real_run = media.run
            def no_encode(command, **kwargs):
                self.assertEqual(command[0], "ffprobe", "Cached preview must not encode again")
                return real_run(command, **kwargs)
            with patch.object(media, "run", side_effect=no_encode):
                repeated = media.browser_preview(original, directory, details, av1_mp4=True)
            self.assertEqual(repeated["path"], copied["path"])


if __name__ == "__main__":
    unittest.main()
