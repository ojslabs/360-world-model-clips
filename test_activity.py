"""Independent local job queues and action-label lifecycle, without network calls."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import action_labels
import media
import server


class Queue:
    def __init__(self):
        self.pending = []

    def submit(self, worker):
        self.pending.append(worker)

    def run_next(self):
        self.pending.pop(0)()


class ActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.queues = {name: Queue() for name in ("POOL", "GENERATION_POOL", "INTERACTIVE_POOL", "SEARCH_POOL", "LABEL_POOL")}
        patches = [patch.object(server, "DATA", self.root), patch.object(server, "JOBS", {})]
        patches += [patch.object(server, name, queue) for name, queue in self.queues.items()]
        patches += [patch.object(server, "generation_status", return_value={"ready": True, "configured": True, "model": "fixture"})]
        for replacement in patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        replacement = patch.object(action_labels, "label_moment", side_effect=self.result)
        self.label = replacement.start()
        self.addCleanup(replacement.stop)
        self.video = server.SEED
        self.location = server.folder(self.video)
        (self.location / "frames").mkdir(parents=True)
        (self.location / "source.mp4").write_bytes(b"preserved source fixture")
        self.project = {"id": self.video, "source_file": "source.mp4", "media": {"duration": 100, "fps": 30},
            "candidates": [{"id": "cut1", "time": 20, "freeze_time": 20, "selected": True,
                            "tail_seconds": media.DEFAULTS["tail_seconds"], "frame_url": "/frames/cut1.png"},
                           {"id": "cut2", "time": 30, "freeze_time": 30, "selected": False}],
            "generations": [], "exports": []}
        Image.new("RGB", (160, 90), "red").save(self.location / "frames/cut1.png")
        media.write_json(server.manifest(self.video), self.project)

    @staticmethod
    def result(source, source_time, cache, **kwargs):
        return {"status": "complete", "source_time": source_time, "label": "Person jumps", "confidence": "high",
                "evidence": [{"frame": 1, "observation": "Knees bent."},
                             {"frame": 3, "observation": "Feet above the ground."}]}

    def request(self, path, value):
        body = json.dumps(value).encode()
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(body)
        handler.trusted = Mock(return_value=True)
        handler.send_json = Mock()
        handler.do_POST()
        return handler.send_json.call_args

    def test_identical_label_requests_share_one_job_and_other_moment_is_busy(self):
        first = server.start_label(self.video, "cut1", 20)
        repeated = server.start_label(self.video, "cut1", 20)
        self.assertEqual(first, repeated)
        self.assertEqual(len(self.queues["LABEL_POOL"].pending), 1)
        response = self.request("/api/label", {"video_id": self.video, "item_id": "cut2", "time": 30})
        self.assertEqual(response.args[1], 409)
        self.assertIn("already being identified", response.args[0]["error"])
        self.queues["LABEL_POOL"].run_next()
        self.label.assert_called_once()
        self.assertEqual(server.get_job(first["job_id"])["status"], "complete")

    def test_label_does_not_wait_behind_import_or_generation_jobs(self):
        imported = server.start_import(self.video, {"av1_mp4": True})
        generated = server.start_generation(self.video, "fal-h3-max", item_id="cut1")
        labeled = server.start_label(self.video, "cut1", 20)
        self.assertEqual([len(self.queues[name].pending) for name in ("POOL", "GENERATION_POOL", "LABEL_POOL")], [1, 1, 1])
        self.queues["LABEL_POOL"].run_next()
        self.assertEqual(server.get_job(labeled["job_id"])["status"], "complete")
        self.assertEqual(server.get_job(imported["job_id"])["status"], "queued")
        self.assertEqual(server.get_job(generated["job_id"])["status"], "queued")
        self.label.assert_called_once()

    def test_late_label_cannot_overwrite_a_new_selected_time(self):
        server.start_label(self.video, "cut1", 20)
        project = server.load_project(self.video)
        project["candidates"][0].update(freeze_time=21, action_label={"label": "Newer label", "source_time": 21})
        media.write_json(server.manifest(self.video), project)
        self.queues["LABEL_POOL"].run_next()
        candidate = server.load_project(self.video)["candidates"][0]
        self.assertEqual(candidate["action_label"], {"label": "Newer label", "source_time": 21})

    def test_matching_completed_run_supplies_its_verified_immutable_png(self):
        target = self.location / "exports/saved"
        target.mkdir(parents=True)
        frame = target / "frame.png"
        frame.write_bytes((self.location / "frames/cut1.png").read_bytes())
        saved = {"id": "saved", "provider": "Fal", "status": "complete", "item_id": "cut1",
                 "freeze_time": 20, "frame_sha256": hashlib.sha256(frame.read_bytes()).hexdigest()}
        self.project["generations"] = [saved]
        media.write_json(server.manifest(self.video), self.project)
        server.start_label(self.video, "cut1", 20)
        Image.new("RGB", (160, 90), "blue").save(self.location / "frames/cut1.png")
        self.queues["LABEL_POOL"].run_next()
        self.assertEqual(self.label.call_args.kwargs["frame_path"], frame)
        self.assertEqual(hashlib.sha256(frame.read_bytes()).hexdigest(), saved["frame_sha256"])
        run = media.read_json(target / "run.json")
        self.assertEqual(run["action_label"]["source_time"], 20)

    def test_candidate_reference_is_snapshotted_before_its_frame_changes(self):
        candidate = self.location / "frames/cut1.png"
        original = candidate.read_bytes()
        server.start_label(self.video, "cut1", 20)
        Image.new("RGB", (160, 90), "blue").save(candidate)
        self.queues["LABEL_POOL"].run_next()
        snapshot = self.label.call_args.kwargs["frame_path"]
        self.assertNotEqual(snapshot, candidate)
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertEqual(snapshot.parent, self.location / "action-labels/frames")

    def test_queue_wait_consumes_the_same_ten_second_budget(self):
        clock = [100.0]
        with patch.object(server.time, "monotonic", side_effect=lambda: clock[0]):
            server.start_label(self.video, "cut1", 20)
            clock[0] += 7
            self.queues["LABEL_POOL"].run_next()
        self.assertAlmostEqual(self.label.call_args.kwargs["timeout"], 3)

    def test_expired_queued_label_makes_no_provider_request(self):
        clock = [100.0]
        with patch.object(server.time, "monotonic", side_effect=lambda: clock[0]):
            started = server.start_label(self.video, "cut1", 20)
            clock[0] += 11
            self.queues["LABEL_POOL"].run_next()
        self.label.assert_not_called()
        result = server.get_job(started["job_id"])["result"]["action_label"]
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["request_submitted"])

    def test_compact_jobs_omit_results_and_unchanged_reads_preserve_updated_time(self):
        target = self.location / "exports/complete"
        target.mkdir(parents=True)
        (target / "orbit.mp4").write_bytes(b"saved video")
        run = {"id": "complete", "provider": "Reactor", "status": "complete",
               "url": f"/media/{self.video}/exports/complete/orbit.mp4"}
        self.project["generations"] = [run]
        media.write_json(server.manifest(self.video), self.project)
        server.save_job({"id": "completejob", "kind": "fixture", "status": "complete", "created_at": 1,
                         "video_id": self.video, "run_id": "complete", "result": {"stale": True}})
        repaired = server.get_job("completejob")
        before = server.job_path("completejob").read_bytes()
        for _ in range(3):
            compact = server.compact_jobs()
            self.assertNotIn("result", compact[0])
            self.assertEqual(compact[0]["updated_at"], repaired["updated_at"])
        self.assertEqual(server.job_path("completejob").read_bytes(), before)

    def test_import_dedupes_while_queued_and_progress_survives_compact_reads(self):
        started = server.start_import(self.video, {"av1_mp4": True})
        self.assertEqual(server.start_import(self.video, {"av1_mp4": True}), started)
        self.assertEqual(len(self.queues["POOL"].pending), 1)

        def imported(video_id, playback_capabilities, on_progress):
            self.assertEqual(video_id, self.video)
            self.assertTrue(playback_capabilities["av1_mp4"])
            on_progress({"phase": "downloading", "percent": 45})
            current = next(job for job in server.compact_jobs() if job["id"] == started["job_id"])
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["progress"], {"phase": "downloading", "percent": 45})
            self.assertEqual(server.start_import(self.video, playback_capabilities), started)
            return {"id": self.video}

        with patch.object(server, "import_video", side_effect=imported) as importer:
            self.queues["POOL"].run_next()
        importer.assert_called_once()
        self.assertEqual(server.get_job(started["job_id"])["status"], "complete")


if __name__ == "__main__":
    unittest.main()
