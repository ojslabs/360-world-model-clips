import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from app import media
from app import reactor_remix as remix


class FakeCapture:
    def __init__(self, path, expected):
        self.expected_frames = expected
        self.frames = 0
        self.error = None
        self.enabled = False

    def on_frame(self, pixels, width, height, frame_id, timestamp_us, user_data):
        if self.enabled:
            self.frames += 1

    def close(self, require_complete=True):
        if require_complete and self.frames != self.expected_frames:
            raise remix.RemixError("Incomplete capture")
        return {"frames": self.frames, "width": 160, "height": 90}


class FakeReactor:
    instance = None
    fail_prompt = False

    def __init__(self, **kwargs):
        self.options = kwargs
        self.handlers = {}
        self.commands = []
        self.disconnected = False
        self.session_id = "offline-session"
        FakeReactor.instance = self

    def on(self, name, callback):
        self.handlers[name] = callback

    def track(self, name):
        self.output_track = name
        return self

    def on_raw_frame(self, callback):
        self.callback = callback

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def publish_track(self, name):
        self.input_track = name
        return self

    def push_frame(self, pixels, **kwargs):
        self.padding_sent = getattr(self, "padding_sent", 0) + 1
        if self.padding_sent == 3:
            for index in range(38):
                self.callback(pixels, 160, 90, index + 82, index * 41667, b"")

    async def send_command(self, name, data):
        self.commands.append((name, data))
        if name == "set_prompt":
            if self.fail_prompt:
                raise RuntimeError("ambiguous connection failure")
            self.handlers["message"]({"type": "generation_started", "data": {"width": 160, "height": 90}})


async def fake_feed(source, track, details, count, save):
    for index in range(count):
        track.callback(b"pixels", 160, 90, index, index * 41667, b"")
    return b"last source frame", 160, 90


async def chunked_feed(source, track, details, count, save):
    for index in range(count - 38):
        track.callback(b"pixels", 160, 90, index, index * 41667, b"")
    return b"last source frame", 160, 90


class RemixTests(unittest.TestCase):
    def session(self, root, feed=fake_feed):
        receipt = {}

        def save(stage, **fields):
            receipt.update(stage=stage, **fields)

        request = {"output": str(root / "remix.mp4"), "source": str(root / "source.mp4"),
                   "prompt": "Turn daylight into night", "source_media": {"width": 160, "height": 90},
                   "expected_frames": 120}
        with patch.dict("sys.modules", {"reactor_sdk": types.SimpleNamespace(Reactor=FakeReactor)}), \
             patch("app.reactor_api.mint_token", return_value={"jwt": "private-test-token"}) as mint, \
             patch.object(remix, "Capture", FakeCapture), patch.object(remix, "_stream_source", feed):
            asyncio.run(remix._session(request, receipt, save))
            mint.assert_called_once_with(model="xmax/x2", credential_key=remix.reactor_api.ENV_CREDENTIAL)
        return receipt

    def test_single_session_prompt_and_complete_source_backlog(self):
        with tempfile.TemporaryDirectory() as folder:
            receipt = self.session(Path(folder))
        client = FakeReactor.instance
        self.assertEqual(client.options["model_name"], "xmax/x2")
        self.assertEqual(client.input_track, "source")
        self.assertEqual(client.output_track, "main_video")
        self.assertEqual([name for name, _ in client.commands], ["set_keep_backlog", "set_prompt", "reset"])
        self.assertEqual(client.commands[0][1], {"keep_backlog": True})
        self.assertTrue(client.disconnected)
        self.assertEqual(receipt["capture"]["frames"], 120)
        self.assertNotIn("private-test-token", json.dumps(receipt))

    def test_ambiguous_command_failure_disconnects_without_resubmission(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(FakeReactor, "fail_prompt", True):
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                self.session(Path(folder))
        self.assertTrue(FakeReactor.instance.disconnected)
        self.assertEqual(sum(name == "set_prompt" for name, _ in FakeReactor.instance.commands), 1)

    def test_final_processing_block_flushes_with_repeated_source_context(self):
        with tempfile.TemporaryDirectory() as folder:
            receipt = self.session(Path(folder), chunked_feed)
        self.assertEqual(receipt["capture"]["frames"], 120)
        self.assertEqual(receipt["padding_frames_sent"], 3)
        self.assertTrue(receipt["source_padding_finished"])
        self.assertEqual(sum(name == "set_prompt" for name, _ in FakeReactor.instance.commands), 1)
        self.assertTrue(FakeReactor.instance.disconnected)

    def test_stalled_tail_terminates_and_keeps_partial_capture_without_retry(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(remix, "STALL_SECONDS", .02), \
             patch.object(FakeReactor, "push_frame", return_value=None):
            with self.assertRaisesRegex(remix.RemixError, "stopped before its final frames"):
                self.session(Path(folder), chunked_feed)
        self.assertEqual(sum(name == "set_prompt" for name, _ in FakeReactor.instance.commands), 1)
        self.assertTrue(FakeReactor.instance.disconnected)

    def test_native_capture_and_audio_are_preserved_in_separate_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / "source.mp4", root / "remix.mp4"
            media.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=160x90:r=24:d=5",
                       "-f", "lavfi", "-i", "sine=frequency=731:sample_rate=48000:duration=5",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)])
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            capture = remix.Capture(remix._native_path(output), 120)
            capture.enabled = True
            for index in range(120):
                pixels = bytes((index, 40, 90, 255)) * (160 * 90)
                capture.on_frame(pixels, 160, 90, index, index * 41667, b"")
                time.sleep(1 / remix.FPS)
            capture_info = capture.close()
            self.assertEqual(capture_info["frames"], 120)
            self.assertEqual(capture_info["local_dropped_frames"], 0)
            request = {"source": str(source), "output": str(output), "source_media": media.probe(source),
                       "expected_frames": 120}
            receipt = {}
            remix._finish(request, receipt, lambda *args, **kwargs: None)
            self.assertEqual(receipt["media"]["width"], 160)
            self.assertEqual(receipt["media"]["height"], 90)
            self.assertAlmostEqual(receipt["media"]["fps"], 24)
            self.assertTrue(receipt["original_audio_preserved"])
            self.assertTrue(receipt["decoded"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            self.assertEqual(remix._audio_hash(source), remix._audio_hash(output))

    def test_short_capture_and_resolution_change_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            capture = remix.Capture(Path(folder) / "native.mp4", 120)
            capture.enabled = True
            capture.on_frame(bytes(160 * 90 * 4), 160, 90, 1, 1, b"")
            capture.on_frame(bytes(80 * 44 * 4), 80, 44, 2, 2, b"")
            with self.assertRaisesRegex(remix.RemixError, "changed resolution"):
                capture.close()

    def test_existing_attempt_rejected_before_auth_or_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / "source.mp4", root / "remix.mp4"
            source.write_bytes(b"source")
            remix._receipt_path(output).write_text("{}")
            with patch.object(remix.subprocess, "Popen") as worker:
                with self.assertRaisesRegex(remix.RemixError, "already exists"):
                    remix.generate(source, output, "Night")
                worker.assert_not_called()

    def test_transport_fit_preserves_aspect_and_diagnostic_hides_secrets(self):
        self.assertEqual(remix._transport_size({"width": 3840, "height": 2160}), (1920, 1080))
        self.assertEqual(remix._transport_size({"width": 1080, "height": 1920}), (606, 1080))
        result = remix._safe_error(RuntimeError("rk_secret eyJheader.payload.sig https://example.test/?jwt=secret"))
        self.assertNotIn("rk_secret", result)
        self.assertNotIn("eyJ", result)
        self.assertNotIn("https://", result)

    def test_capacity_failure_is_classified_without_exposing_provider_details(self):
        for error in (RuntimeError('HTTP429 no available capacity'),
                      type('RateLimitedError', (RuntimeError,), {})('private upstream response')):
            self.assertTrue(remix._is_capacity_error(error))
        self.assertFalse(remix._is_capacity_error(RuntimeError('Connection timed out')))
        self.assertEqual(remix.CAPACITY_MESSAGE, 'Reactor is at capacity. Start again in a moment.')


if __name__ == "__main__":
    unittest.main()
