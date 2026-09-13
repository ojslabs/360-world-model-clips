"""Durable jobs and restart reconciliation, using temporary state and no providers."""
import io
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen
from unittest.mock import Mock, patch

import media
import server


class Queue:
    def __init__(self):
        self.pending = []

    def submit(self, worker):
        self.pending.append(worker)


class DurableJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.queue = Queue()
        for replacement in (patch.object(server, "DATA", self.root), patch.object(server, "JOBS", {}),
                            patch.object(server, "POOL", self.queue),
                            patch.object(server, "GENERATION_POOL", self.queue),
                            patch.object(server, "INTERACTIVE_POOL", self.queue),
                            patch.object(server, "SEARCH_POOL", self.queue),
                            patch.object(server, "LABEL_POOL", self.queue)):
            replacement.start()
            self.addCleanup(replacement.stop)

    def make_run(self, status="complete", ready=True, error=None, acknowledged=False):
        video_id, run_id = server.SEED, "run123"
        location = server.folder(video_id) / "exports" / run_id
        location.mkdir(parents=True, exist_ok=True)
        run = {"id": run_id, "job_id": "job123", "video_id": video_id, "status": status,
               "provider": "Fal", "prompt": "saved immutable prompt", "tail_seconds": media.DEFAULTS["tail_seconds"],
               "freeze_time": 30, "delivery": {"anchor_reference": True},
               "url": f"/media/{video_id}/exports/{run_id}/orbit.mp4", "composites": []}
        (location / "orbit.mp4").write_bytes(b"previously verified output")
        (location / "frame.png").write_bytes(b"saved immutable frame")
        if ready:
            (location / "highlight.mp4").write_bytes(b"previously verified assembly")
            run["composites"] = [{"tail_seconds": media.DEFAULTS["tail_seconds"], "reference_anchored": True,
                                   "url": f"/media/{video_id}/exports/{run_id}/highlight.mp4"}]
        if error:
            run["error"] = error
        if acknowledged:
            media.write_json(location / "fal-request.json", {"request_id": "existing-paid-id", "seconds": 6})
        media.write_json(location / "run.json", run)
        media.write_json(server.manifest(video_id), {"id": video_id, "candidates": [], "generations": [run]})
        return video_id, run_id, location

    def test_generic_completed_job_survives_empty_memory_and_http_lookup(self):
        result = server.task("Local operation", lambda: {"answer": 42})
        job_id = result["job_id"]
        self.assertEqual(media.read_json(server.job_path(job_id))["status"], "queued")
        self.queue.pending.pop()()
        server.JOBS.clear()
        self.assertEqual(server.get_job(job_id)["result"], {"answer": 42})
        server.reconcile_jobs()
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{http.server_port}/api/jobs/{job_id}", timeout=3) as response:
                saved = json.load(response)
            self.assertEqual(saved["status"], "complete")
            self.assertEqual(saved["result"], {"answer": 42})
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=3)

    def test_incomplete_local_job_is_interrupted_without_reexecution(self):
        operation = Mock()
        job_id = server.task("Analyze source", operation)["job_id"]
        self.queue.pending.clear()
        server.reconcile_jobs()
        self.assertEqual(server.get_job(job_id)["status"], "interrupted")
        self.assertEqual(self.queue.pending, [])
        operation.assert_not_called()

    def test_complete_run_repairs_job_and_removes_stale_restart_errors(self):
        video, run_id, location = self.make_run(error="Server restarted")
        server.save_job({"id": "job123", "kind": "Generate", "status": "running",
                         "video_id": video, "run_id": run_id, "error": "stale"})
        server.reconcile_jobs()
        saved = server.get_job("job123")
        self.assertEqual(saved["status"], "complete")
        self.assertTrue(saved["delivery_ready"])
        self.assertNotIn("error", saved)
        self.assertNotIn("error", server.read_run(video, run_id))
        self.assertNotIn("error", media.read_json(location / "run.json"))
        self.assertEqual(server.run_status(video, run_id)["status"], "complete")
        self.assertEqual(self.queue.pending, [])

    def test_run_receipt_recovers_completion_written_before_project_update(self):
        video, run_id, location = self.make_run()
        project = server.load_project(video)
        project["generations"][0].update(status="running", error="old error")
        media.write_json(server.manifest(video), project)
        server.reconcile_jobs()
        self.assertEqual(server.run_status(video, run_id)["status"], "complete")
        self.assertEqual(self.queue.pending, [])

    def test_acknowledged_interrupted_run_queues_resume_only_bound_to_saved_ids(self):
        video, run_id, _ = self.make_run(status="running", ready=False, acknowledged=True)
        with patch.object(server, "execute_generation", return_value={"status": "complete"}) as execute:
            server.reconcile_jobs()
            self.assertEqual(len(self.queue.pending), 1)
            job = media.read_json(server.job_path("job123"))
            self.assertEqual((job["video_id"], job["run_id"], job["status"]), (video, run_id, "queued"))
            self.assertEqual(server.read_run(video, run_id)["request_id"], "existing-paid-id")
            self.queue.pending.pop()()
            execute.assert_called_once_with(video, run_id, resume_only=True)

    def test_unacknowledged_generation_is_not_resumed(self):
        video, run_id, _ = self.make_run(status="running", ready=False)
        with patch.object(server, "execute_generation") as execute:
            server.reconcile_jobs()
        self.assertEqual(self.queue.pending, [])
        self.assertEqual(server.run_status(video, run_id)["status"], "interrupted")
        execute.assert_not_called()

    def test_completed_orbit_waits_for_local_delivery_and_never_reenters_provider(self):
        video, run_id, _ = self.make_run(status="complete", ready=False)
        server.reconcile_jobs()
        self.assertEqual(server.run_status(video, run_id)["status"], "queued")
        self.assertFalse(server.run_status(video, run_id)["delivery_ready"])
        with patch("fal_camera.generate", side_effect=AssertionError("No generation allowed")) as generate, \
             patch.object(server, "assemble_highlight", side_effect=RuntimeError("local failure")):
            self.queue.pending.pop()()
        generate.assert_not_called()
        self.assertEqual(server.read_run(video, run_id)["status"], "complete")
        self.assertIn("Retry local assembly", server.read_run(video, run_id)["composition_error"])
        self.assertEqual(server.get_job("job123")["status"], "failed")

    def test_generation_identity_is_persisted_before_executor_can_start(self):
        video = server.SEED
        location = server.folder(video)
        (location / "frames").mkdir(parents=True)
        (location / "frames/cut.png").write_bytes(b"saved frame")
        media.write_json(server.manifest(video), {"id": video, "media": {"duration": 900, "fps": 30},
            "candidates": [{"id": "cut", "selected": True, "freeze_time": 30,
                            "tail_seconds": media.DEFAULTS["tail_seconds"], "frame_url": "/frame.png"}]})
        with patch.object(server, "generation_status", return_value={"ready": True, "model": "test-model"}):
            result = server.start_generation(video, "fal-h3-max")
        persisted = media.read_json(server.job_path(result["job_id"]))
        run = server.read_run(video, result["run_id"])
        self.assertEqual(persisted["run_id"], result["run_id"])
        self.assertEqual(persisted["video_id"], video)
        self.assertEqual(run["job_id"], result["job_id"])
        self.assertEqual(persisted["status"], "queued")
        self.assertEqual(len(self.queue.pending), 1)

    def test_generate_route_forwards_the_active_item_without_submitting_twice(self):
        payload = {"video_id": server.SEED, "mode": "fal-h3-max", "item_id": "active-cut"}
        body = json.dumps(payload).encode()
        handler = object.__new__(server.Handler)
        handler.path = "/api/generate"
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(body)
        handler.trusted = Mock(return_value=True)
        handler.send_json = Mock()
        credential = server.fal_credentials.Credential("offline-test-browser-key", "test-owner", 9999999999)
        with patch.object(server.fal_credentials, "require", return_value=credential), \
                patch.object(server, "start_generation", return_value={"job_id": "job", "run_id": "run"}) as start:
            handler.do_POST()
        start.assert_called_once_with(server.SEED, "fal-h3-max", None, item_id="active-cut", credential=credential)
        handler.send_json.assert_called_once_with({"job_id": "job", "run_id": "run"})

    def test_process_lock_prevents_a_second_server_until_workers_release_ownership(self):
        first = server.acquire_server_lock()
        try:
            with self.assertRaisesRegex(RuntimeError, "finishing its active jobs"):
                server.acquire_server_lock()
        finally:
            first.close()
        server.acquire_server_lock().close()


if __name__ == "__main__":
    unittest.main()
