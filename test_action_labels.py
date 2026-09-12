"""Visual evidence, immutable references and strict deadlines without external calls."""
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import action_labels
import fal_camera
import media


ANSWER = json.dumps({"label": "Player jumping", "confidence": "high", "evidence": [
    {"frame": 1, "observation": "Player bends knees."},
    {"frame": 3, "observation": "Both feet are off the ground."}]})


class ActionLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.source = Path(cls.temporary.name) / "source.mp4"
        media.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                   "color=red:size=320x180:rate=30,drawbox=color=blue:t=fill:enable='gte(t,3.5)'",
                   "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", cls.source])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = self.root / "reference.png"
        Image.new("RGB", (320, 180), "lime").save(self.reference)
        self.real_infer = action_labels._infer
        replacement = patch.object(action_labels, "_infer", side_effect=AssertionError("No external request allowed"))
        self.infer = replacement.start()
        self.addCleanup(replacement.stop)

    def label(self, **kwargs):
        return action_labels.label_moment(self.source, 3, self.root / "cache", frame_path=self.reference, **kwargs)

    def test_three_frame_evidence_uses_immutable_middle_and_caches_same_request(self):
        def observe(sheet, deadline):
            with Image.open(io.BytesIO(sheet)) as picture:
                for point, channel in (((320, 180), 0), ((960, 180), 1), ((320, 540), 2)):
                    pixel = picture.getpixel(point)
                    self.assertEqual(pixel.index(max(pixel)), channel)
            self.assertGreater(deadline, time.monotonic())
            return ANSWER
        self.infer.side_effect = observe
        first = self.label()
        again = self.label()
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["label"], "Player jumping")
        self.assertEqual(first["frame_times"], [2.2, 3, 3.8])
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["cached"])
        self.infer.assert_called_once()
        Image.new("RGB", (320, 180), "lime").save(self.reference)
        self.assertEqual(self.label()["id"], first["id"])
        self.infer.assert_called_once()

    def test_changed_reference_has_separate_identity(self):
        self.infer.side_effect = None
        self.infer.return_value = ANSWER
        first = self.label()
        Image.new("RGB", (320, 180), "yellow").save(self.reference)
        changed = self.label()
        self.assertNotEqual(first["id"], changed["id"])
        self.assertNotEqual(first["frame_sha256"], changed["frame_sha256"])
        self.assertEqual(self.infer.call_count, 2)

    def test_uncertain_or_single_frame_answer_cannot_become_a_recognized_action(self):
        for confidence, evidence in (("low", [{"frame": 1, "observation": "Feet are hidden."}]),
                                     ("high", [{"frame": 2, "observation": "Person visible."}])):
            result = action_labels._parse(json.dumps({"label": "Scores a goal", "confidence": confidence,
                                                      "evidence": evidence}))
            self.assertEqual(result["status"], "uncertain")
            self.assertEqual(result["label"], "Action not identified")

    def test_bad_model_response_is_cached_without_a_second_request(self):
        self.infer.side_effect = None
        self.infer.return_value = "An unstructured description."
        first, again = self.label(), self.label()
        self.assertEqual(first["status"], "uncertain")
        self.assertEqual(again["label"], "Action not identified")
        self.infer.assert_called_once()

    def test_known_frame_aliases_are_normalized_without_guessing_schema_or_numbers(self):
        for name in ("frame", "frame number", "frame_number"):
            value = json.loads(ANSWER)
            for item in value["evidence"]:
                item[name] = item.pop("frame")
            self.assertEqual(action_labels._parse(json.dumps(value))["evidence"], json.loads(ANSWER)["evidence"])
        for invalid in ({"index": 1}, {"frame number": "1"}, {"frame_number": 1.0},
                        {"frame": True}, {"frame": float("nan")}, {"frame": 0},
                        {"frame": 1, "frame number": 2}, {"frame": 1, "frame_number": "1"}):
            with self.subTest(invalid=invalid):
                value = json.loads(ANSWER)
                value["evidence"][0] = {**invalid, "observation": "Player bends knees."}
                self.assertEqual(action_labels._parse(json.dumps(value))["status"], "uncertain")
        for malformed in ([], {"label": "Player jumping", "confidence": "high", "evidence": {"frame": 1}}):
            with self.assertRaises(ValueError):
                action_labels._parse(json.dumps(malformed))

    def test_cached_uncertain_response_is_reparsed_locally_without_new_request(self):
        response = json.loads(ANSWER)
        for item in response["evidence"]:
            item["frame number"] = item.pop("frame")
        self.infer.side_effect = None
        self.infer.return_value = json.dumps(response)
        # Reproduce the earlier parser's discarded evidence while preserving its response.
        with patch.object(action_labels, "_parse", return_value={"status": "uncertain",
                "label": "Action not identified", "confidence": "low", "evidence": []}):
            previous = self.label()
        receipt_path = self.root / "cache" / (previous["id"] + ".json")
        saved = media.read_json(receipt_path)
        saved["message"] = "The visual check did not identify this action. No retry was submitted."
        media.write_json(receipt_path, saved)
        self.infer.reset_mock()
        self.infer.side_effect = AssertionError("A parser repair must never submit another request")
        with patch.object(action_labels, "_contact_sheet", side_effect=AssertionError("Reuse saved response")), \
                patch.object(fal_camera, "api_key", side_effect=AssertionError("No credentials needed")):
            recovered = self.label()
        self.infer.assert_not_called()
        self.assertTrue(recovered["cached"])
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual(recovered["evidence"], json.loads(ANSWER)["evidence"])
        for field in ("id", "source_time", "frame_sha256", "created_at", "elapsed_seconds", "request_submitted"):
            self.assertEqual(recovered[field], previous[field])
        self.assertNotIn("message", recovered)
        repaired_bytes = receipt_path.read_bytes()
        self.assertEqual(self.label()["label"], "Player jumping")
        self.assertEqual(receipt_path.read_bytes(), repaired_bytes)

    def test_cached_unknown_schema_remains_uncertain_without_retry_or_receipt_rewrite(self):
        self.infer.side_effect = None
        self.infer.return_value = json.dumps({"description": "Person jumping", "evidence": [1, 3]})
        first = self.label()
        receipt_path = self.root / "cache" / (first["id"] + ".json")
        before = receipt_path.read_bytes()
        again = self.label()
        self.assertEqual(again["status"], "uncertain")
        self.assertEqual(receipt_path.read_bytes(), before)
        self.infer.assert_called_once()

    def test_concurrent_same_moment_has_one_request(self):
        entered, finish = threading.Event(), threading.Event()
        def infer(*_args):
            entered.set()
            self.assertTrue(finish.wait(3))
            return ANSWER
        self.infer.side_effect = infer
        worker = threading.Thread(target=self.label)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            pending = self.label()
            self.assertEqual(pending["status"], "running")
            self.infer.assert_called_once()
        finally:
            finish.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.label()["status"], "complete")
        self.infer.assert_called_once()

    def test_extraction_timeout_returns_uncertain_without_inference(self):
        with patch.object(action_labels, "_contact_sheet", side_effect=subprocess.TimeoutExpired("ffmpeg", .1)):
            result = self.label(timeout=.1)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["label"], "Action not identified")
        self.infer.assert_not_called()

    def test_http_subprocess_is_killed_at_the_shared_deadline(self):
        started = time.monotonic()
        with patch.object(fal_camera, "api_key", return_value="test-only-key"), \
             patch.object(action_labels, "HTTP_WORKER", "import time; time.sleep(2)"):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.real_infer(b"fixture image", started + .15)
        self.assertLess(time.monotonic() - started, .8)

    def test_credentials_and_image_are_only_sent_on_private_standard_input(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"output": ANSWER}).encode(), b"")
        with patch.object(fal_camera, "api_key", return_value="test-only-key"), \
             patch.object(action_labels.subprocess, "run", return_value=response) as process:
            self.assertEqual(self.real_infer(b"fixture image", time.monotonic() + 2), ANSWER)
        self.assertNotIn("test-only-key", repr(process.call_args.args))
        self.assertNotIn("fixture image", repr(process.call_args.args))
        request = json.loads(process.call_args.kwargs["input"])
        self.assertEqual(request["key"], "test-only-key")
        self.assertEqual(request["payload"]["model"], "google/gemini-2.5-flash")
        self.assertEqual(len(request["payload"]["image_urls"]), 1)
        self.assertFalse(request["payload"]["enable_web_search"])
        self.assertLessEqual(process.call_args.kwargs["timeout"], 2)


if __name__ == "__main__":
    unittest.main()
