"""Mocked Fal queue lifecycles with real local MP4 decoding and no external calls."""
import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from PIL import Image

from app import fal_camera
from app import media


FAKE_KEY = "test-only-fal-key:never-valid"
FAKE_TOKEN = "test-only-cdn-signature"
REQUEST_ID = "test-request-123"
QUEUE = "https://queue.fal.run"
MODEL = "minimax/h3-max/camera-controls"
STATUS = f"{QUEUE}/minimax/h3-max/requests/{REQUEST_ID}/status"
RESULT = f"{QUEUE}/minimax/h3-max/requests/{REQUEST_ID}"
VIDEO = "https://v3.fal.media/files/test/orbit.mp4?signature=" + FAKE_TOKEN


class Response(io.BytesIO):
    def __init__(self, value, content_type="application/json"):
        raw = json.dumps(value).encode() if isinstance(value, dict) else value
        super().__init__(raw)
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(raw))}


class FalCameraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture_dir.cleanup)
        path = Path(cls.fixture_dir.name) / "video.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=red:size=1920x1080:rate=10", "-t", "6", "-an",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
                       check=True, capture_output=True, timeout=30)
        cls.video_bytes = path.read_bytes()
        native = Path(cls.fixture_dir.name) / "wide-native.mp4"
        picture = ("color=red:size=1890x1080:rate=24,"
                   "drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='gte(n,152)',"
                   "drawbox=x=0:y=0:w=45:h=ih:color=lime:t=fill")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", picture,
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "6.592",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(native)],
                       check=True, capture_output=True, timeout=30)
        cls.native_bytes = native.read_bytes()
        small = Path(cls.fixture_dir.name) / "legacy-native.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(native), "-vf", "scale=168:96",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy", str(small)],
                       check=True, capture_output=True, timeout=30)
        cls.legacy_native_bytes = small.read_bytes()
        lower = Path(cls.fixture_dir.name) / "lower-resolution.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=blue:size=1344x768:rate=24", "-t", "6", "-an",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(lower)],
                       check=True, capture_output=True, timeout=30)
        cls.lower_resolution_bytes = lower.read_bytes()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.frame = self.root / "frame.png"
        Image.new("RGB", (160, 90), "white").save(self.frame)
        self.output = self.root / "run"
        self.prompt = "Hold the football scene frozen while the camera completes its supplied orbit."
        self.calls = []
        self.opener = Mock()
        self.opener.open.side_effect = AssertionError("Unexpected network operation")
        for replacement in (patch.object(fal_camera, "ROOT", self.root),
                            patch.dict(os.environ, {"FAL_KEY": FAKE_KEY, "PATH": os.environ["PATH"]}, clear=True),
                            patch.object(fal_camera, "build_opener", return_value=self.opener),
                            patch.object(fal_camera.time, "sleep")):
            replacement.start()
            self.addCleanup(replacement.stop)

    def receipt(self):
        return media.read_json(self.output / "fal-request.json")

    def lifecycle(self, states=None, video_url=VIDEO, video_bytes=None, timings=None):
        states = iter(states or ["IN_QUEUE", "IN_PROGRESS", "COMPLETED"])

        def exchange(request, timeout):
            self.calls.append(request)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 30)
            if request.full_url.startswith(QUEUE):
                self.assertEqual(request.get_header("Authorization"), "Key " + FAKE_KEY)
            if request.get_method() == "POST":
                self.assertEqual(request.full_url, f"{QUEUE}/{MODEL}")
                self.assertEqual(self.receipt()["stage"], "submitting")
                self.assertEqual(request.get_header("X-fal-no-retry"), "1")
                return Response({"request_id": REQUEST_ID, "status_url": STATUS, "response_url": RESULT})
            if request.full_url == STATUS:
                self.assertEqual(self.receipt()["request_id"], REQUEST_ID)
                return Response({"status": next(states), "logs": [{"message": FAKE_KEY}]})
            if request.full_url == RESULT:
                return Response({"video": {"url": video_url}, "expanded_prompt": FAKE_TOKEN,
                                 "timings": timings})
            if request.full_url == video_url:
                self.assertIsNone(request.get_header("Authorization"))
                self.assertIsNone(request.get_header("Cookie"))
                self.assertNotIn(FAKE_KEY, repr(request.header_items()))
                return Response(self.video_bytes if video_bytes is None else video_bytes, "video/mp4")
            self.fail("Unexpected mocked HTTP URL")
        self.opener.open.side_effect = exchange

    def test_queue_lifecycle_submits_exact_camera_request_and_decodes_silent_video(self):
        self.lifecycle()
        result = fal_camera.generate(self.frame, self.output, self.prompt)
        posted = json.loads(self.calls[0].data)
        image_url = posted["image_url"]
        self.assertEqual(base64.b64decode(image_url.split(",", 1)[1]), self.frame.read_bytes())
        expected = media.orbit_request(image_url)
        expected.update(prompt=self.prompt, duration=6)
        self.assertEqual(posted, expected)
        self.assertEqual(posted["resolution"], "1080P")
        trajectory = posted["camera_trajectory"]
        self.assertEqual(trajectory[0]["azimuth"], 0)
        self.assertEqual(trajectory[-1]["azimuth"], 360)
        self.assertLess(trajectory[-2]["time"], trajectory[-1]["time"])
        self.assertAlmostEqual((1 - trajectory[-1]["time"]) * posted["duration"], .7)
        self.assertNotIn("seed", posted)
        self.assertEqual(sum(r.get_method() == "POST" for r in self.calls), 1)
        self.assertEqual(result["request_id"], REQUEST_ID)
        self.assertEqual(Path(result["native_path"]).read_bytes(), self.video_bytes)
        self.assertTrue(result["media"]["decoded_video"])
        self.assertTrue(result["resolution_check"]["verified"])
        self.assertEqual((result["media"]["width"], result["media"]["height"]), (1920, 1080))
        self.assertIsNone(result["media"]["audio_codec"])
        self.assertEqual(result["capture_method"], "provider_video_normalized")
        self.assertEqual(result["audio"], "none")
        self.assertTrue(fal_camera.status()["authenticated"])
        with patch.dict(os.environ, {"FAL_KEY": "a-different-test-key"}):
            self.assertFalse(fal_camera.status()["authenticated"])
        self.assertAlmostEqual(result["media"]["duration"], 6, places=1)
        saved = (self.output / "fal-request.json").read_text()
        for public in (saved, json.dumps(result)):
            for private in (FAKE_KEY, FAKE_TOKEN, VIDEO, image_url):
                self.assertNotIn(private, public)
        self.assertEqual(self.receipt()["stage"], "complete")
        progress = result["download_progress"]
        self.assertTrue(progress["complete"])
        self.assertEqual(progress["downloaded_bytes"], len(self.video_bytes))
        self.assertEqual(progress["total_bytes"], len(self.video_bytes))
        self.assertEqual(progress["percent"], 100)
        self.assertEqual(self.receipt()["download_progress"], progress)
        self.opener.open.reset_mock()
        repeated = fal_camera.generate(self.frame, self.output, self.prompt)
        self.opener.open.assert_not_called()
        self.assertEqual(repeated["request_id"], REQUEST_ID)

    def test_active_generation_polls_faster_without_resubmitting(self):
        self.lifecycle(["IN_QUEUE", "IN_PROGRESS", "IN_PROGRESS", "COMPLETED"])
        finish_local = fal_camera._finish_local
        intervals = []

        def finish(*args, **kwargs):
            # Capture queue waiting before subprocess timeout waits share time.sleep.
            intervals.extend(call.args[0] for call in sleep.call_args_list)
            return finish_local(*args, **kwargs)

        with patch.object(fal_camera.time, "sleep") as sleep:
            with patch.object(fal_camera, "_finish_local", side_effect=finish):
                result = fal_camera.generate(self.frame, self.output, self.prompt)
        self.assertEqual(intervals, [3, 1, 1])
        self.assertEqual(sum(request.get_method() == "POST" for request in self.calls), 1)
        self.assertEqual(result["request_id"], REQUEST_ID)
        self.assertEqual(self.receipt()["stage"], "complete")

    def assert_normalized_native(self, result, dimensions=(1920, 1080), legacy=False):
        self.assertEqual(Path(result["native_path"]).read_bytes(),
                         self.legacy_native_bytes if legacy else self.native_bytes)
        self.assertEqual(result["native_media"]["width"], 168 if legacy else 1890)
        self.assertEqual(result["native_media"]["height"], 96 if legacy else 1080)
        self.assertGreater(result["native_media"]["duration"], 6.5)
        self.assertEqual(result["native_media"]["audio_codec"], "aac")
        self.assertEqual((result["media"]["width"], result["media"]["height"]), dimensions)
        self.assertEqual(result["media"]["duration"], 6)
        self.assertEqual(result["media"]["fps"], 30)
        self.assertIsNone(result["media"]["audio_codec"])
        self.assertEqual(result["normalization"]["retime_method"], "uniform_full_video")
        self.assertLess(result["normalization"]["retime_scale"], 1)
        # Only the final native quarter-second is blue. Trimming six seconds from
        # the unretimed video would lose it. The green edge proves no horizontal crop.
        frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", "5.966666667", "-i", result["path"],
                                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                               check=True, capture_output=True, timeout=30).stdout
        with Image.open(io.BytesIO(frame)) as picture:
            center = picture.getpixel((picture.width // 2, picture.height // 2))
            edge = picture.getpixel((round(picture.width / 64), picture.height // 2))
            border = picture.getpixel((2, picture.height // 2))
        self.assertGreater(center[2], 180)
        self.assertLess(center[0], 50)
        self.assertGreater(edge[1], 180)
        self.assertLess(max(border), 20)

    def test_native_near_wide_audio_video_is_preserved_and_retimed_with_final_scene(self):
        self.lifecycle(video_bytes=self.native_bytes)
        result = fal_camera.generate(self.frame, self.output, self.prompt)
        self.assert_normalized_native(result)
        self.assertEqual(sum(r.get_method() == "POST" for r in self.calls), 1)

    def test_lower_resolution_paid_result_is_preserved_without_upscale_or_resubmit(self):
        self.lifecycle(["COMPLETED"], video_bytes=self.lower_resolution_bytes)
        with self.assertRaisesRegex(fal_camera.FalResolutionError, "1344x768.*1080P") as raised:
            fal_camera.generate(self.frame, self.output, self.prompt)
        native = self.output / "fal-camera-native.mp4"
        self.assertEqual(native.read_bytes(), self.lower_resolution_bytes)
        self.assertFalse((self.output / "fal-camera.mp4").exists())
        self.assertFalse(self.receipt()["resolution_check"]["verified"])
        self.assertEqual(self.receipt()["request_id"], REQUEST_ID)
        self.assertTrue(self.receipt()["native_media"]["decoded_video"])
        self.assertEqual(sum(request.get_method() == "POST" for request in self.calls), 1)
        for private in (FAKE_KEY, FAKE_TOKEN, VIDEO):
            self.assertNotIn(private, str(raised.exception))
        self.opener.open.reset_mock()
        self.opener.open.side_effect = AssertionError("Existing lower-resolution result must remain local")
        with patch.object(fal_camera, "api_key", side_effect=AssertionError("Local recovery needs no key")):
            with self.assertRaises(fal_camera.FalResolutionError):
                fal_camera.recover_local(self.output)
        with self.assertRaises(fal_camera.FalResolutionError):
            fal_camera.generate(self.frame, self.output, self.prompt, resume_only=True)
        self.opener.open.assert_not_called()
        self.assertEqual(native.read_bytes(), self.lower_resolution_bytes)

    def test_legacy_completed_download_recovers_without_keys_or_http(self):
        self.lifecycle(["COMPLETED"], video_bytes=self.legacy_native_bytes)
        legacy_preset = json.loads(json.dumps(media.ORBIT_PRESET))
        legacy_preset["id"] = "fal-h3-max-frozen-orbit-v4"
        legacy_preset["input"]["resolution"] = "768P"
        with patch.object(media, "ORBIT_PRESET", legacy_preset), \
                patch.object(fal_camera, "_finish_local", side_effect=fal_camera.FalError("interrupted locally")):
            with self.assertRaisesRegex(fal_camera.FalError, "interrupted locally"):
                fal_camera.generate(self.frame, self.output, self.prompt)
        # Reproduce the previous adapter's filename and completed-but-unverified receipt.
        (self.output / "fal-camera-native.mp4").replace(self.output / "fal-camera.mp4")
        legacy = self.receipt()
        legacy.update(stage="completed")
        media.write_json(self.output / "fal-request.json", legacy)
        self.assertEqual(self.receipt()["stage"], "completed")
        self.opener.open.reset_mock()
        self.opener.open.side_effect = AssertionError("Recovery must not use HTTP")
        with patch.object(fal_camera, "api_key", side_effect=AssertionError("Recovery must not read credentials")):
            result = fal_camera.recover_local(self.output)
        self.assert_normalized_native(result, dimensions=(1280, 720), legacy=True)
        self.assertEqual(self.receipt()["parameters"]["resolution"], "768P")
        self.opener.open.assert_not_called()
        self.assertEqual(result["request_id"], REQUEST_ID)
        self.assertEqual(self.receipt()["stage"], "complete")
        cached_bytes = Path(result["path"]).read_bytes()
        repeated = fal_camera.generate(self.frame, self.output, self.prompt, resume_only=True)
        self.opener.open.assert_not_called()
        self.assertEqual(repeated["request_id"], REQUEST_ID)
        self.assertEqual(Path(repeated["path"]).read_bytes(), cached_bytes)
        self.assertEqual(repeated["normalization"]["height"], 720)

    def test_measured_stages_keep_queue_wait_and_survive_local_recovery(self):
        clock = [1000.0]
        events = []
        self.lifecycle(["IN_QUEUE", "IN_QUEUE", "IN_PROGRESS", "COMPLETED"],
                       timings={"inference": 42.25, "preprocessing": 6, "debug": FAKE_KEY,
                                "api_key": 123, "nan": float("nan"), "infinite": float("inf"),
                                "negative": -1, "flag": True, "nested": {"secret": FAKE_KEY}})
        exchange = self.opener.open.side_effect

        def timed_exchange(request, timeout):
            response = exchange(request, timeout)
            clock[0] += 2 if request.get_method() == "POST" else 5
            return response

        def observe(stage, details):
            self.assertEqual(self.receipt()["stage"], stage)
            events.append((stage, details))
            # Observers may disconnect or fail. This cannot abort or repeat a job.
            raise RuntimeError("observer disconnected")

        self.opener.open.side_effect = timed_exchange
        with patch.object(fal_camera.time, "time", side_effect=lambda: clock[0]):
            result = fal_camera.generate(self.frame, self.output, self.prompt, on_progress=observe)
        self.assertEqual(result["provider_timings"], {"inference": 42.25, "preprocessing": 6})
        self.assertEqual(result["stage_timings"]["submitting"], 2)
        self.assertEqual(result["stage_timings"]["queued"], 15)
        self.assertEqual(result["stage_timings"]["in_progress"], 5)
        self.assertEqual(result["stage_timings"]["completed"], 5)
        self.assertEqual(result["stage_timings"]["downloading"], 5)
        stages = [item["stage"] for item in result["stage_transitions"]]
        self.assertEqual(stages, ["submitting", "queued", "in_progress", "completed",
                                  "downloading", "normalizing", "complete"])
        self.assertEqual(events[-1][0], "complete")
        download_events = [details["download_progress"] for stage, details in events
                           if stage == "downloading" and details["download_progress"]]
        self.assertEqual(download_events[0]["downloaded_bytes"], 0)
        self.assertTrue(download_events[-1]["complete"])
        self.assertNotIn(FAKE_KEY, json.dumps(events))
        self.assertEqual(sum(r.get_method() == "POST" for r in self.calls), 1)
        self.opener.open.reset_mock()
        with patch.object(fal_camera, "api_key", side_effect=AssertionError("No key read on recovery")):
            recovered = fal_camera.recover_local(self.output)
        self.assertEqual(recovered["provider_timings"], result["provider_timings"])
        self.assertEqual(recovered["stage_timings"]["queued"], 15)
        self.assertNotIn("complete", recovered["stage_timings"])
        self.opener.open.assert_not_called()

    def test_downloaded_receipt_recovers_with_no_remote_or_key_calls(self):
        self.lifecycle(["COMPLETED"])
        with patch.object(fal_camera, "_finish_local", side_effect=fal_camera.FalError("local stop")):
            with self.assertRaisesRegex(fal_camera.FalError, "local stop"):
                fal_camera.generate(self.frame, self.output, self.prompt)
        self.assertEqual(self.receipt()["stage"], "downloading")
        self.assertTrue((self.output / "fal-camera-native.mp4").is_file())
        self.opener.open.reset_mock()
        with patch.object(fal_camera, "api_key", side_effect=AssertionError("No key read")):
            result = fal_camera.recover_local(self.output)
        self.assertEqual(result["media"]["duration"], 6)
        self.opener.open.assert_not_called()

    def test_poll_failure_resumes_saved_id_without_another_submit(self):
        self.lifecycle(["COMPLETED"])
        normal_exchange = self.opener.open.side_effect

        def fail_poll(request, timeout):
            if request.full_url == STATUS:
                self.assertEqual(self.receipt()["request_id"], REQUEST_ID)
                raise URLError("provider diagnostic " + FAKE_KEY)
            return normal_exchange(request, timeout)

        self.opener.open.side_effect = fail_poll
        with self.assertRaises(fal_camera.FalError) as raised:
            fal_camera.generate(self.frame, self.output, self.prompt)
        self.assertNotIn(FAKE_KEY, str(raised.exception))
        self.opener.open.side_effect = normal_exchange
        result = fal_camera.generate(self.frame, self.output, self.prompt, resume_only=True)
        self.assertEqual(result["request_id"], REQUEST_ID)
        self.assertEqual(sum(r.get_method() == "POST" for r in self.calls), 1)

    def test_ambiguous_submit_cannot_be_automatically_repeated(self):
        self.opener.open.side_effect = TimeoutError(FAKE_KEY)
        with self.assertRaises(fal_camera.FalError):
            fal_camera.generate(self.frame, self.output, self.prompt)
        self.assertEqual(self.receipt()["stage"], "submitting")
        with self.assertRaisesRegex(fal_camera.FalError, "no acknowledged request ID"):
            fal_camera.generate(self.frame, self.output, self.prompt)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_resume_only_without_receipt_cannot_enter_submission_branch(self):
        with self.assertRaisesRegex(fal_camera.FalError, "Resume cannot submit"):
            fal_camera.generate(self.frame, self.output, self.prompt, resume_only=True)
        self.opener.open.assert_not_called()
        self.assertFalse((self.output / "fal-request.json").exists())

    def test_resume_only_rejects_changed_snapshot_without_any_http(self):
        self.lifecycle(["COMPLETED"])
        with patch.object(fal_camera, "_finish_local", side_effect=fal_camera.FalError("local stop")):
            with self.assertRaises(fal_camera.FalError):
                fal_camera.generate(self.frame, self.output, self.prompt)
        self.opener.open.reset_mock()
        Image.new("RGB", (160, 90), "black").save(self.frame)
        with self.assertRaisesRegex(fal_camera.FalError, "different saved frame or prompt"):
            fal_camera.generate(self.frame, self.output, self.prompt, resume_only=True)
        self.opener.open.assert_not_called()

    def test_bad_queue_redirect_location_is_rejected_after_id_is_persisted(self):
        self.opener.open.return_value = Response({"request_id": REQUEST_ID,
                                                  "status_url": "https://evil.example/?key=" + FAKE_KEY})
        self.opener.open.side_effect = None
        with self.assertRaisesRegex(fal_camera.FalError, "invalid queue location") as raised:
            fal_camera.generate(self.frame, self.output, self.prompt)
        self.assertEqual(self.receipt()["request_id"], REQUEST_ID)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertNotIn(FAKE_KEY, str(raised.exception))
        self.assertNotIn(FAKE_KEY, (self.output / "fal-request.json").read_text())

    def test_download_rejects_local_plaintext_credential_and_lookalike_urls(self):
        for url in ("http://v3.fal.media/video.mp4", "https://127.0.0.1/video.mp4",
                    "https://v3.fal.media.evil.example/video.mp4",
                    "https://user:password@v3.fal.media/video.mp4"):
            with self.subTest(url=url), self.assertRaisesRegex(fal_camera.FalError, "unsupported"):
                fal_camera._download(url, self.root / "result.mp4", float("inf"))
        self.opener.open.assert_not_called()

    def test_provider_errors_never_expose_response_bodies_or_signed_urls(self):
        self.opener.open.side_effect = HTTPError(
            VIDEO, 401, FAKE_KEY, {}, io.BytesIO(json.dumps({"key": FAKE_KEY, "jwt": FAKE_TOKEN}).encode()))
        with self.assertRaises(fal_camera.FalError) as raised:
            fal_camera.generate(self.frame, self.output, self.prompt)
        public = str(raised.exception) + (self.output / "fal-request.json").read_text()
        for private in (FAKE_KEY, FAKE_TOKEN, VIDEO):
            self.assertNotIn(private, public)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_total_poll_wait_is_bounded_and_keeps_request_id(self):
        now = [100.0]
        self.lifecycle(["IN_QUEUE"])
        exchange = self.opener.open.side_effect

        def delayed(request, timeout):
            response = exchange(request, timeout)
            if request.full_url == STATUS:
                now[0] += 1000
            return response

        self.opener.open.side_effect = delayed
        with patch.object(fal_camera.time, "monotonic", side_effect=lambda: now[0]):
            with self.assertRaisesRegex(fal_camera.FalError, "not resubmitted"):
                fal_camera.generate(self.frame, self.output, self.prompt)
        self.assertEqual(self.receipt()["request_id"], REQUEST_ID)
        self.assertEqual(sum(r.get_method() == "POST" for r in self.calls), 1)

    def test_preflight_checks_reject_bad_frames_and_duration_before_submission(self):
        for seconds in (True, 4, 16, 6.5):
            with self.subTest(seconds=seconds), self.assertRaises(fal_camera.FalError):
                fal_camera.generate(self.frame, self.output, self.prompt, seconds)
        Image.new("RGB", (100, 100)).save(self.frame)
        with self.assertRaisesRegex(fal_camera.FalError, "16:9 PNG"):
            fal_camera.generate(self.frame, self.output, self.prompt)
        self.opener.open.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_status_and_key_setup_are_server_only_and_do_not_check_paid_access(self):
        with patch.dict(os.environ, {}, clear=True):
            missing = fal_camera.status()
            self.assertFalse(missing["configured"])
            (self.root / ".env.local").write_text("FAL_KEY='" + FAKE_KEY + "'\n")
            self.assertEqual(fal_camera.api_key(), FAKE_KEY)
            configured = fal_camera.status()
        self.assertTrue(configured["ready"])
        self.assertTrue(configured["exact_camera_controls"])
        self.assertFalse(configured["authenticated"])
        self.assertNotIn(FAKE_KEY, json.dumps(configured))
        self.opener.open.assert_not_called()

    def test_v5_requests_1080p_and_preserves_fal_eased_example_with_equal_start_end_distance(self):
        preset = media.ORBIT_PRESET
        self.assertEqual(preset["id"], "fal-h3-max-frozen-orbit-v5")
        request = media.orbit_request("https://example.test/reference.png")
        self.assertEqual(request["prompt_expansion_mode"], "balanced")
        self.assertEqual(request["duration"], 6)
        self.assertEqual(request["resolution"], "1080P")
        self.assertNotIn("seed", request)
        trajectory = request["camera_trajectory"]
        # Independent contract values from the supplied Fal Python example.
        self.assertEqual([p["time"] for p in trajectory], [
            0, .03333333333333333, .1395833333333333, .24583333333333332,
            .3520833333333333, .4583333333333333, .5645833333333333,
            .6708333333333333, .7770833333333332, .8833333333333333])
        self.assertEqual([p["azimuth"] for p in trajectory],
                         [0, 0, 15.46875, 56.25, 113.90625, 180,
                          246.09375, 303.75, 344.53125, 360])
        self.assertAlmostEqual(trajectory[1]["time"] * request["duration"], .2)
        self.assertAlmostEqual(trajectory[-1]["time"] * request["duration"], 5.3)
        self.assertTrue(all(a["time"] < b["time"] for a, b in zip(trajectory, trajectory[1:])))
        self.assertTrue(all(0 <= p["time"] <= 1 and p["elevation"] == 0 and p["distance"] == 1
                            for p in trajectory))
        self.assertNotIn("ending_frame", request)
        self.assertNotIn("end_image_url", request)

    def test_expanded_prompt_preserves_wording_but_redacts_credentials_and_urls(self):
        text = ("The frozen scene stays rigid. " + FAKE_KEY + " " + FAKE_TOKEN
                + " https://v3.fal.media/private?signature=secret eyJheader.payload.signature")
        cleaned = fal_camera._expanded_prompt(text, FAKE_KEY, VIDEO)
        self.assertIn("The frozen scene stays rigid.", cleaned)
        for secret in (FAKE_KEY, FAKE_TOKEN, "https://", "eyJheader", "signature=secret"):
            self.assertNotIn(secret, cleaned)
        self.assertIsNone(fal_camera._expanded_prompt(None, FAKE_KEY, VIDEO))


class DownloadProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.target = Path(temporary.name) / "native.mp4"
        self.clock = [100.0]
        self.events = []

    def download(self, chunks, length, *, observer=None):
        chunks = iter(chunks)
        response = Response(b"", "video/mp4")
        if length is None:
            response.headers.pop("Content-Length")
        else:
            response.headers["Content-Length"] = str(length)

        def read(size):
            self.assertEqual(size, 1024 * 1024)
            self.clock[0] += .1
            chunk = next(chunks, b"")
            if chunk is None:
                raise URLError("private diagnostic " + FAKE_TOKEN)
            return chunk

        response.read = read
        opener = Mock()
        opener.open.return_value = response
        with patch.object(fal_camera, "build_opener", return_value=opener), \
                patch.object(fal_camera.time, "monotonic", side_effect=lambda: self.clock[0]):
            fal_camera._download(VIDEO, self.target, 110, on_progress=observer or self.events.append)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.get_header("Authorization"))

    def test_chunk_progress_is_measured_throttled_and_finishes_after_atomic_delivery(self):
        chunks = [bytes([number]) * 32 for number in range(9)]

        def observe(progress):
            if progress["complete"]:
                self.assertEqual(self.target.read_bytes(), b"".join(chunks))
            self.events.append(progress)

        self.download(chunks, 288, observer=observe)
        self.assertEqual([item["downloaded_bytes"] for item in self.events], [0, 128, 256, 288])
        self.assertAlmostEqual(self.events[1]["percent"], 44.44)
        self.assertEqual(self.events[1]["speed_bytes_per_second"], 320)
        self.assertEqual(self.events[1]["eta_seconds"], .5)
        self.assertTrue(self.events[-1]["complete"])
        self.assertEqual(self.events[-1]["percent"], 100)
        self.assertEqual(self.events[-1]["eta_seconds"], 0)
        self.assertFalse(self.target.with_suffix(".mp4.part").exists())
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.events))
        self.assertNotIn("https://", json.dumps(self.events))

    def test_unknown_size_has_bytes_but_no_invented_percentage_or_eta(self):
        self.download([b"abc", b"def"], None)
        self.assertEqual(self.target.read_bytes(), b"abcdef")
        self.assertEqual(self.events[-1]["downloaded_bytes"], 6)
        for progress in self.events:
            self.assertIsNone(progress["total_bytes"])
            self.assertIsNone(progress["percent"])
            self.assertIsNone(progress["eta_seconds"])
        self.assertTrue(self.events[-1]["complete"])

    def test_truncated_declared_download_is_not_marked_complete_or_installed(self):
        with self.assertRaisesRegex(fal_camera.FalError, "declared size"):
            self.download([b"partial"], 100)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_suffix(".mp4.part").exists())
        self.assertFalse(any(item["complete"] for item in self.events))

    def test_failed_stream_keeps_only_safe_partial_progress(self):
        with self.assertRaisesRegex(fal_camera.FalError, "not resubmitted") as raised:
            self.download([b"part"] * 4 + [None], 100)
        self.assertEqual(self.events[-1]["downloaded_bytes"], 16)
        self.assertNotIn(FAKE_TOKEN, str(raised.exception))
        self.assertFalse(any(item["complete"] for item in self.events))
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_suffix(".mp4.part").exists())

    def test_disconnected_progress_observer_does_not_discard_paid_video(self):
        self.download([b"complete"], 8, observer=Mock(side_effect=RuntimeError("closed observer")))
        self.assertEqual(self.target.read_bytes(), b"complete")


if __name__ == "__main__":
    unittest.main()
