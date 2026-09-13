"""Reactor remix routes and durable jobs, with temporary media and no provider calls."""
import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import media
from app import reactor_api
from app import reactor_credentials
from app import reactor_remix
from app import remix_catalog
from app import server


class Queue:
    def __init__(self):
        self.pending = []

    def submit(self, worker):
        self.pending.append(worker)

    def run_next(self):
        self.pending.pop(0)()


class RemixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.queue = Queue()
        key = "rk_test_only_never_valid"
        self.credential = reactor_credentials.Credential(key, reactor_api.credential_fingerprint(key), time.time() + 3600, True)
        self.cookie = reactor_credentials.cookie_header("T" * 43)
        replacements = [patch.object(server, "DATA", self.root), patch.object(server, "JOBS", {}),
                        patch.object(server, "generation_status", return_value={"ready": True, "configured": True}),
                        patch.object(reactor_credentials, "STORE", {"T" * 43: self.credential}),
                        patch.object(reactor_api, "request", side_effect=AssertionError("No external requests"))]
        replacements += [patch.object(server, name, self.queue) for name in vars(server)
                         if name == "POOL" or name.endswith("_POOL")]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        replacement = patch.object(reactor_remix, "generate", side_effect=self.generated)
        self.generate = replacement.start()
        self.addCleanup(replacement.stop)
        self.video = server.SEED
        self.location = server.folder(self.video)
        self.target = self.location / "exports/falrun"
        self.target.mkdir(parents=True)
        self.source = self.location / "source.mp4"
        self.source.write_bytes(b"original source must remain unchanged")
        self.original = self.target / "highlight.mp4"
        self.original.write_bytes(b"completed original composite fixture")
        (self.target / "fal-camera.mp4").write_bytes(b"preserved raw Fal fixture")
        self.details = {"duration": 18, "width": 1920, "height": 1080, "fps": 30,
                        "video_codec": "h264", "audio_codec": "aac", "decoded_video": True}
        self.composite = {"id": "completed-edit", "run_id": "falrun", "media": self.details,
                          "tail_seconds": 4, "lead_seconds": 8,
                          "url": f"/media/{self.video}/exports/falrun/highlight.mp4"}
        self.project = {"id": self.video, "title": "Saved demo", "source_file": "source.mp4",
                        "media": {"duration": 100, "width": 1920, "height": 1080, "fps": 30},
                        "candidates": [], "exports": [], "generations": [{
                            "id": "falrun", "provider": "Fal", "status": "complete", "request_id": "paid-original",
                            "url": f"/media/{self.video}/exports/falrun/fal-camera.mp4",
                            "composites": [self.composite]}]}
        media.write_json(server.manifest(self.video), self.project)

    def generated(self, source, output, prompt, *, on_progress=None, credential_key=None, credential_owner=None):
        self.assertEqual(credential_key, self.credential.key)
        self.assertEqual(credential_owner, self.credential.fingerprint)
        self.assertEqual(Path(source).resolve(), self.original.resolve())
        self.assertTrue(prompt.strip())
        self.assertNotEqual(Path(output).resolve(), self.original.resolve())
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"new Reactor remix fixture")
        if on_progress:
            on_progress({"stage": "recording", "frames_received": 30, "frames_expected": 540})
        return {"path": str(output), "media": self.details, "source_media": self.details,
                "model": remix_catalog.MODEL, "session_id": "test-session",
                "timeline_preservation_verified": False, "original_audio_preserved": True}

    def request(self, path, value=None):
        handler = object.__new__(server.Handler)
        handler.command = "GET" if value is None else "POST"
        handler.path = path
        body = b"" if value is None else json.dumps(value).encode()
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json", "Cookie": self.cookie}
        handler.rfile = io.BytesIO(body)
        handler.trusted = Mock(return_value=True)
        handler.send_json = Mock()
        if value is None:
            handler.do_GET()
        else:
            handler.do_POST()
        call = handler.send_json.call_args
        self.assertIsNotNone(call)
        return call.args[0], call.args[1] if len(call.args) > 1 else 200

    def remix(self, **kwargs):
        return server.start_remix(self.video, self.composite["id"], "day-to-night", credential=self.credential, **kwargs)

    def saved(self, identifier):
        return next(item for item in server.load_project(self.video).get("remixes", []) if item["id"] == identifier)

    def test_presets_get_is_read_only_and_exposes_no_credentials(self):
        before = server.manifest(self.video).read_bytes()
        value, status = self.request("/api/remix-presets")
        self.assertEqual(status, 200)
        self.assertEqual(value["provider"], "Reactor")
        self.assertEqual(value["model"], "X2")
        self.assertIsInstance(value["configured"], bool)
        self.assertTrue(any(preset["id"] == "day-to-night" for preset in value["presets"]))
        self.assertTrue(all({"id", "label", "prompt"}.issubset(preset) for preset in value["presets"]))
        self.assertNotIn("rk_test_only_never_valid", json.dumps(value))
        self.assertEqual(server.manifest(self.video).read_bytes(), before)
        self.assertEqual(self.queue.pending, [])
        self.generate.assert_not_called()

    def test_post_queues_durable_remix_and_completed_output_appears_without_changing_original(self):
        original_project = copy.deepcopy(server.load_project(self.video))
        source_bytes, composite_bytes = self.source.read_bytes(), self.original.read_bytes()
        result, status = self.request("/api/remix", {"video_id": self.video,
                                     "composite_id": self.composite["id"], "preset_id": "day-to-night"})
        self.assertEqual(status, 200)
        self.assertEqual(self.saved(result["remix_id"])["status"], "queued")
        job = server.get_job(result["job_id"])
        self.assertEqual(job["remix_id"], result["remix_id"])
        self.assertEqual(job["video_id"], self.video)
        self.assertNotIn("run_id", job)
        self.generate.assert_not_called()
        self.queue.run_next()
        self.generate.assert_called_once()
        saved = self.saved(result["remix_id"])
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["composite_id"], self.composite["id"])
        self.assertEqual(saved["media"], self.details)
        self.assertTrue(saved["audio_preserved"])
        self.assertFalse(saved["timeline_preservation_verified"])
        self.assertTrue(saved["url"].startswith(f"/media/{self.video}/"))
        self.assertNotEqual(saved["url"], self.composite["url"])
        state, _ = self.request("/api/state")
        published = next(project for project in state["projects"] if project["id"] == self.video)
        self.assertIn(saved, published["remixes"])
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertEqual(self.original.read_bytes(), composite_bytes)
        for name in ("media", "generations", "candidates"):
            self.assertEqual(server.load_project(self.video)[name], original_project[name])
        server.JOBS.clear()
        self.assertEqual(server.get_job(result["job_id"])["status"], "complete")

    def test_repeated_active_and_completed_request_reuses_one_job_and_provider_call(self):
        first = self.remix()
        repeated = self.remix()
        self.assertEqual((first["job_id"], first["remix_id"]), (repeated["job_id"], repeated["remix_id"]))
        self.assertEqual(len(self.queue.pending), 1)
        self.queue.run_next()
        completed = self.remix()
        self.assertEqual((first["job_id"], first["remix_id"]), (completed["job_id"], completed["remix_id"]))
        self.assertEqual(self.queue.pending, [])
        self.assertEqual(len(server.load_project(self.video)["remixes"]), 1)
        self.generate.assert_called_once()

    def test_only_an_existing_completed_composite_can_be_selected(self):
        for identifier in ("missing", "falrun", "../../source.mp4"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                server.start_remix(self.video, identifier, "day-to-night", credential=self.credential)
        for status in ("queued", "running", "failed"):
            changed = copy.deepcopy(self.project)
            changed["generations"][0]["status"] = status
            media.write_json(server.manifest(self.video), changed)
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.remix()
        self.assertEqual(self.queue.pending, [])
        self.generate.assert_not_called()

    def test_composite_remote_cross_project_and_traversal_urls_are_rejected_before_queueing(self):
        for url in ("https://remote.example/video.mp4", "/media/8o40mSS05iE/exports/falrun/highlight.mp4",
                    f"/media/{self.video}/exports/falrun/../../source.mp4",
                    f"/media/{self.video}/exports/falrun/%2e%2e/%2e%2e/source.mp4"):
            changed = copy.deepcopy(self.project)
            changed["generations"][0]["composites"][0]["url"] = url
            media.write_json(server.manifest(self.video), changed)
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.remix()
        self.assertEqual(self.queue.pending, [])
        self.generate.assert_not_called()

    def test_symlink_outside_selected_exports_is_not_accepted_as_a_composite(self):
        self.original.unlink()
        self.original.symlink_to(self.source)
        with self.assertRaises(ValueError):
            self.remix()
        self.assertEqual(self.queue.pending, [])
        self.generate.assert_not_called()

    def test_symlinked_exports_root_cannot_rebind_a_project_to_external_files(self):
        exports = self.location / "exports"
        outside = self.root / "external-exports"
        exports.rename(outside)
        exports.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.remix()
        self.assertEqual(self.queue.pending, [])
        self.generate.assert_not_called()

    def test_changed_prompt_creates_separate_remix_but_same_trimmed_prompt_reuses_it(self):
        first = self.remix()
        second = self.remix(prompt="  Turn the background into clay.  ")
        repeated = self.remix(prompt="Turn the background into clay.")
        self.assertNotEqual(first["remix_id"], second["remix_id"])
        self.assertEqual(second["remix_id"], repeated["remix_id"])
        self.assertEqual(len(self.queue.pending), 2)
        self.queue.run_next()
        self.queue.run_next()
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(self.generate.call_args.args[2], "Turn the background into clay.")
        self.assertEqual(len(server.load_project(self.video)["remixes"]), 2)

    def test_unexpected_provider_diagnostics_are_not_persisted_to_public_state(self):
        secret = "rk_provider_secret_only_test"
        self.generate.side_effect = ValueError("Provider returned " + secret + " https://private.example/video")
        result = self.remix()
        self.queue.run_next()
        public = json.dumps(self.saved(result["remix_id"])) + json.dumps(server.get_job(result["job_id"]))
        self.assertNotIn(secret, public)
        self.assertNotIn("https://private.example", public)
        self.assertEqual(self.saved(result["remix_id"])["status"], "failed")

    def test_composite_must_belong_to_its_completed_parent_run(self):
        changed = copy.deepcopy(self.project)
        changed["generations"][0]["id"] = "different-run"
        media.write_json(server.manifest(self.video), changed)
        with self.assertRaises(ValueError):
            self.remix()
        self.assertEqual(self.queue.pending, [])
        self.generate.assert_not_called()

    def test_changed_source_between_queue_and_worker_fails_before_provider_call(self):
        result = self.remix()
        self.original.write_bytes(b"changed after selection")
        self.queue.run_next()
        self.assertEqual(self.saved(result["remix_id"])["status"], "failed")
        self.assertEqual(server.get_job(result["job_id"])["status"], "failed")
        self.generate.assert_not_called()

    def test_completed_remix_receipt_repairs_interrupted_job_after_restart_without_provider_call(self):
        result = self.remix()
        self.queue.run_next()
        completed = self.saved(result["remix_id"])
        project = server.load_project(self.video)
        project["remixes"][0].update(status="running", error="stale restart error")
        media.write_json(server.manifest(self.video), project)
        job = server.get_job(result["job_id"])
        job.update(status="running", error="stale restart error")
        server.save_job(job)
        self.generate.reset_mock()
        server.reconcile_jobs()
        repaired = server.get_job(result["job_id"])
        self.assertEqual(repaired["status"], "complete")
        self.assertNotIn("error", repaired)
        self.assertEqual(self.saved(result["remix_id"])["status"], completed["status"])
        self.assertNotIn("error", self.saved(result["remix_id"]))
        self.generate.assert_not_called()
        self.assertEqual(self.queue.pending, [])

    def test_provider_failure_is_durable_and_original_completed_edit_is_unchanged(self):
        before = copy.deepcopy(server.load_project(self.video))
        self.generate.side_effect = reactor_remix.RemixError("The test remix failed. No automatic retry was submitted.")
        result = self.remix()
        self.queue.run_next()
        saved = self.saved(result["remix_id"])
        self.assertEqual(saved["status"], "failed")
        self.assertTrue(saved.get("error") or saved.get("message"))
        server.JOBS.clear()
        job = server.get_job(result["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job.get("error"))
        self.assertEqual(server.load_project(self.video)["generations"], before["generations"])
        self.assertEqual(self.original.read_bytes(), b"completed original composite fixture")
        self.generate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
