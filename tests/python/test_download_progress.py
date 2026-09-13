"""Exercise yt-dlp's real offline template and a loopback-only download."""
import http.server
import json
from pathlib import Path
import tempfile
import threading
import unittest

import yt_dlp
from app import download_progress as progress
from app import server


class DownloadProgressTests(unittest.TestCase):
    def test_real_template_handles_missing_totals_and_never_exposes_urls(self):
        with yt_dlp.YoutubeDL({"quiet": True, "outtmpl_na_placeholder": "null"}) as downloader:
            rendered = downloader.evaluate_outtmpl(progress.DOWNLOAD_TEMPLATE.removeprefix("download:"), {
                "progress": {"status": "downloading", "downloaded_bytes": 50,
                             "total_bytes_estimate": 200, "speed": 10, "eta": 15},
                "info": {"format_id": "401", "url": "https://private.invalid/signed-token"}})
        result = progress.parse_progress(rendered)
        self.assertEqual(result["percent"], 25)
        self.assertTrue(result["total_bytes_estimated"])
        self.assertEqual(result["speed_bytes_per_second"], 10)
        self.assertEqual(result["eta_seconds"], 15)
        self.assertNotIn("signed-token", rendered)
        unknown = progress.parse_progress(progress.PREFIX + json.dumps({"downloaded_bytes": 10}))
        self.assertIsNone(unknown["percent"])
        self.assertIsNone(unknown["eta_seconds"])
        self.assertEqual(progress.parse_progress(progress.POST_PREFIX + '"started"')["stage"], "merging")

    def test_real_ytdlp_download_reports_progress_and_preserves_received_bytes(self):
        payload = b"offline-video-fixture" * 20000
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "download.mp4"
            with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as httpd:
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                events = []
                try:
                    progress.run([server.TOOLS, "--ignore-config", "--no-playlist", "--no-part",
                                  "--output", target, f"http://127.0.0.1:{httpd.server_port}/fixture.mp4"],
                                 on_progress=events.append, timeout=15)
                finally:
                    httpd.shutdown()
                    thread.join()
            self.assertEqual(target.read_bytes(), payload)
            downloads = [event for event in events if event["stage"] == "downloading"]
            self.assertTrue(downloads)
            self.assertEqual(downloads[-1]["downloaded_bytes"], len(payload))
            self.assertEqual(downloads[-1]["percent"], 100)
            self.assertTrue(all(event["scope"] == "current_stream" for event in downloads))
            self.assertTrue(all(event["elapsed_seconds"] >= 0 for event in events))

    def test_failure_is_not_retried_and_error_urls_are_redacted(self):
        with tempfile.TemporaryDirectory() as temporary:
            stub = Path(temporary) / "fail"
            stub.write_text("#!/bin/sh\nprintf 'failed https://private.invalid/token\\n' >&2\nexit 7\n")
            stub.chmod(0o700)
            with self.assertRaisesRegex(RuntimeError, r"failed \[remote URL\]"):
                progress.run([stub], timeout=2)

    def test_silent_process_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            stub = Path(temporary) / "slow"
            stub.write_text("#!/bin/sh\nsleep 10\n")
            stub.chmod(0o700)
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                progress.run([stub], timeout=.1)


if __name__ == "__main__":
    unittest.main()
