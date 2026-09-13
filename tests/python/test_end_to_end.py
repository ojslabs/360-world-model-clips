"""Offline HTTP import-to-download flow with real media processing."""
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from app import action_labels, download_progress, fal_camera, fal_credentials
from app import media, reactor_api, reactor_credentials, server

VIDEO = "C9sL5j_iUiE"
KEY = "test_end_to_end_fal_key_never_valid"


class Queue:
    def __init__(self):
        self.pending = []

    def submit(self, operation):
        self.pending.append(operation)

    def run_next(self):
        self.pending.pop(0)()


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.queue = Queue()
        self.cookie = ""
        replacements = [
            patch.dict(os.environ, {"DEMO_PASSWORD": "", "PUBLIC_ORIGIN": "", "FAL_KEY": "", "REACTOR_API_KEY": ""}),
            patch.object(server, "DATA", self.data), patch.object(server, "JOBS", {}),
            patch.object(fal_camera, "ROOT", self.data),
            patch.object(fal_credentials, "STORE", {}), patch.object(reactor_credentials, "STORE", {}),
            patch.object(fal_credentials, "verify_key", return_value=True),
            patch.object(fal_credentials.threading, "Timer"),
            patch.object(fal_camera, "_json_request", side_effect=AssertionError("No Fal requests")),
            patch.object(reactor_api, "request", side_effect=AssertionError("No Reactor requests")),
            patch.object(action_labels, "_infer", side_effect=AssertionError("No visual-label requests")),
        ]
        replacements += [patch.object(server, name, self.queue) for name in vars(server)
                         if name == "POOL" or name.endswith("_POOL")]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.source = self.root / "remote-source.mkv"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                   "testsrc2=size=320x180:rate=30", "-f", "lavfi", "-i",
                   "sine=frequency=440:sample_rate=48000", "-t", "10", "-c:v", "libx264",
                   "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", self.source], timeout=30)
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.provider_clip = self.root / "provider-response.mp4"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                   "color=blue:size=1920x1080:rate=30", "-t", "6", "-an", "-c:v", "libx264",
                   "-preset", "ultrafast", "-threads", "8", "-pix_fmt", "yuv420p", self.provider_clip], timeout=30)
        self.download = Mock(side_effect=self.downloaded)
        self.provider = Mock(side_effect=self.generated)
        for replacement in (patch.object(download_progress, "run", self.download),
                            patch.object(fal_camera, "generate", self.provider)):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)

    def request(self, path, *, data=None, method=None, headers=None):
        origin = f"http://127.0.0.1:{self.http.server_port}"
        method = method or ("POST" if data is not None else "GET")
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=10)
        try:
            connection.request(method, path, body=json.dumps(data).encode() if data is not None else None,
                               headers={"Origin": origin, "Cookie": self.cookie,
                                        "Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json(self, path, data=None):
        status, _, body = self.request(path, data=data)
        self.assertEqual(status, 200, body.decode())
        self.assertNotIn(KEY, body.decode())
        return json.loads(body)

    def finish(self, result):
        self.assertEqual(self.json("/api/jobs/" + result["job_id"])["status"], "queued")
        self.queue.run_next()
        job = self.json("/api/jobs/" + result["job_id"])
        self.assertEqual(job["status"], "complete", job)
        return job

    def downloaded(self, command, **options):
        # Replace only remote transport, preserving the real import implementation.
        target = server.folder(VIDEO) / "import-original"
        shutil.copyfile(self.source, target / "source.mkv")
        media.write_json(target / "source.info.json", {
            "id": VIDEO, "title": "Offline skateboard sample", "width": 320, "height": 180,
            "fps": 30, "format_id": "offline-source"})
        if options.get("on_progress"):
            options["on_progress"]({"stage": "downloading", "percent": 100})

    def generated(self, frame, target, prompt, *, seconds, credential_key, request_parameters, orbit_path):
        # The provider response is synthetic; local closure/audio/encoding stay real.
        self.assertEqual(credential_key, KEY)
        self.assertEqual(seconds, 6)
        self.assertEqual(orbit_path, "around")
        self.assertEqual(request_parameters["resolution"], "1080P")
        self.assertEqual(Path(frame).read_bytes(), self.saved_frame)
        target = Path(target)
        output = target / "fal-camera.mp4"
        shutil.copyfile(self.provider_clip, output)
        details = media.probe(output)
        return {"path": str(output), "media": details, "native_media": details,
                "request_id": "offline-provider-response", "capture_method": "provider_video", "audio": "none"}

    def test_import_save_generate_compose_and_download_preserve_the_completed_edit(self):
        self.finish(self.json("/api/import", {"video_id": VIDEO}))
        project = self.json("/api/state")["projects"][0]
        self.assertEqual(project["status"], "ready")
        self.assertEqual(project["selection_mode"], "manual")
        self.assertEqual(project["source_quality"]["selection"], "best_available")
        self.assertEqual(project["media"]["width"], 320)
        self.assertEqual(self.request(project["preview_url"])[0], 200)
        self.download.assert_called_once()
        self.provider.assert_not_called()

        item = project["candidates"][0]["id"]
        self.json("/api/select", {"video_id": VIDEO, "item_id": item,
                                 "selected": True, "freeze_time": 5})
        self.finish(self.json("/api/frame", {"video_id": VIDEO, "item_id": item, "time": 5, "tail": 10}))
        project = self.json("/api/state")["projects"][0]
        frame_url = project["candidates"][0]["frame_url"]
        status, _, self.saved_frame = self.request(frame_url)
        self.assertEqual(status, 200)
        with Image.open(io.BytesIO(self.saved_frame)) as frame:
            self.assertEqual(frame.size, (320, 180))
        self.provider.assert_not_called()

        status, headers, _ = self.request("/api/credentials/fal", data={"key": KEY})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        generation = self.json("/api/generate", {"video_id": VIDEO, "item_id": item, "mode": "fal-h3-max"})
        self.finish(generation)
        self.provider.assert_called_once()
        status = self.json(f"/api/runs/{VIDEO}/{generation['run_id']}")
        self.assertEqual(status["status"], "complete")
        self.assertTrue(status["delivery_ready"])
        run = status["result"]
        self.assertNotIn("composition_error", run)
        self.assertEqual(run["frame_sha256"], hashlib.sha256(self.saved_frame).hexdigest())
        output = run["composites"][0]
        published = self.json("/api/state")["projects"][0]["generations"][0]
        self.assertEqual(published["composites"][0]["url"], output["url"])
        self.assertEqual(output["source_window"]["resume_time"], 5 + 1 / 30)
        self.assertAlmostEqual(output["actual_tail_seconds"], 5 - 1 / 30, places=4)
        self.assertAlmostEqual(output["media"]["duration"], 15 - 1 / 30, places=4)
        self.assertTrue(output["reference_closure"]["first_last_pixel_hash_equal"])
        self.assertEqual(output["audio_provenance"]["orbit"], "source_slow_motion_loop")
        self.assertTrue(output["media"]["decoded_video"] and output["media"]["decoded_audio"])
        self.assertEqual((output["media"]["width"], output["media"]["height"]), (1920, 1080))

        status, headers, downloaded = self.request(output["url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(int(headers["Content-Length"]), len(downloaded))
        status, headers, part = self.request(output["url"], headers={"Range": "bytes=0-1023"})
        self.assertEqual(status, 206)
        self.assertEqual(part, downloaded[:1024])
        self.assertEqual(headers["Content-Range"], f"bytes 0-1023/{len(downloaded)}")
        artifact = self.root / "downloaded-highlight.mp4"
        artifact.write_bytes(downloaded)
        media.run(["ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-i", artifact,
                   "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], timeout=30)

        server.JOBS.clear()
        recovered = self.json("/api/jobs/" + generation["job_id"])
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual(self.json(f"/api/runs/{VIDEO}/{generation['run_id']}")["result"]["composites"][0]["url"], output["url"])
        self.provider.assert_called_once()
        self.assertEqual(self.queue.pending, [])
        self.assertEqual(hashlib.sha256(server.source_path(VIDEO).read_bytes()).hexdigest(), self.source_hash)
        for receipt in self.data.rglob("*.json"):
            self.assertNotIn(KEY, receipt.read_text(), str(receipt))


if __name__ == "__main__":
    unittest.main()
