"""Local CLI dispatch and recovery behavior, with no network or generation."""
import contextlib
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import orbit


class OrbitTests(unittest.TestCase):
    def invoke(self, args, responses):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(orbit, "api", side_effect=responses) as api, \
             patch.object(orbit.time, "sleep"), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = orbit.main(args)
        return code, output.getvalue(), errors.getvalue(), api.call_args_list

    def test_run_reads_seed_submits_once_and_prints_local_result(self):
        code, out, err, calls = self.invoke(["run"], [
            {"seed": "C9sL5j_iUiE"}, {"job_id": "job123"}, {"status": "running"},
            {"status": "complete", "result": {"url": "/media/video/exports/run/fal-camera.mp4"}},
        ])
        self.assertEqual(code, 0)
        self.assertEqual(calls[1].args, ("/api/generate", {"video_id": "C9sL5j_iUiE", "mode": "fal-h3-max"}))
        self.assertEqual(sum(call.args[0] == "/api/generate" for call in calls), 1)
        self.assertIn("Job: job123", out)
        self.assertIn("Output: http://127.0.0.1:8476/media/", out)
        self.assertEqual(err, "")

    def test_explicit_video_and_no_wait_do_not_read_seed_or_poll(self):
        code, out, _, calls = self.invoke(["run", "--video-id", "C9sL5j_iUiE", "--no-wait"], [{"job_id": "job123"}])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("Job: job123", out)

    def test_poll_error_preserves_job_id_and_never_resubmits(self):
        code, out, err, calls = self.invoke(["run", "--video-id", "C9sL5j_iUiE"], [
            {"job_id": "saved-job"}, orbit.OrbitError("Temporary read failure"),
        ])
        self.assertEqual(code, 1)
        self.assertIn("Job: saved-job", out)
        self.assertIn("status saved-job --wait", err)
        self.assertEqual(sum(call.args[0] == "/api/generate" for call in calls), 1)

    def test_interrupt_preserves_resume_command(self):
        code, _, err, calls = self.invoke(["status", "saved-job", "--wait"], [KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertIn("status saved-job --wait", err)
        self.assertEqual(calls[0].args, ("/api/jobs/saved-job",))

    def test_failed_job_returns_nonzero_and_no_mutation(self):
        code, out, err, calls = self.invoke(["status", "saved-job"], [{"status": "failed", "error": "Generation rejected"}])
        self.assertEqual(code, 1)
        self.assertIn("Status: failed", out)
        self.assertIn("Generation rejected", err)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], "/api/jobs/saved-job")

    def test_submit_transport_failure_does_not_retry(self):
        code, _, err, calls = self.invoke(["run", "--video-id", "C9sL5j_iUiE"], [orbit.OrbitError("Connection lost")])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("Check the app before running again", err)

    def test_wait_has_a_deadline_without_resubmission(self):
        with patch.object(orbit.time, "monotonic", side_effect=[0, orbit.WAIT_SECONDS + 1]):
            code, _, err, calls = self.invoke(["status", "saved-job", "--wait"], [{"status": "running"}])
        self.assertEqual(code, 1)
        self.assertIn("20 minutes", err)
        self.assertEqual(len(calls), 1)

    def test_http_layer_only_calls_fixed_loopback_once_on_transport_failure(self):
        with patch.object(orbit, "build_opener") as opener:
            opener.return_value.open.side_effect = URLError("offline")
            with self.assertRaises(orbit.OrbitError):
                orbit.api("/api/generate", {"video_id": "C9sL5j_iUiE", "mode": "fal-h3-max"})
            opener.return_value.open.assert_called_once()
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8476/api/generate")
            self.assertEqual(request.get_method(), "POST")

    def test_missing_status_is_an_error(self):
        code, _, err, calls = self.invoke(["status", "saved-job"], [{}])
        self.assertEqual(code, 1)
        self.assertIn("status is missing", err)
        self.assertEqual(len(calls), 1)

    def test_restart_fallback_reads_saved_run_and_submits_only_once(self):
        code, out, err, calls = self.invoke(["run", "--video-id", "C9sL5j_iUiE"], [
            {"job_id": "saved-job", "run_id": "saved-run", "video_id": "C9sL5j_iUiE"},
            {"status": "running"}, orbit.OrbitError("Unknown job", http_status=404),
            {"status": "running", "result": {"id": "saved-run"}},
            {"status": "complete", "result": {"id": "saved-run", "url": "/media/video/exports/run/highlight.mp4"}},
        ])
        self.assertEqual(code, 0)
        self.assertIn("Run: saved-run", out)
        self.assertIn("Reading the saved run", out)
        self.assertIn("highlight.mp4", out)
        self.assertEqual(err, "")
        self.assertEqual(sum(call.args[0] == "/api/generate" for call in calls), 1)
        self.assertEqual([call.args[0] for call in calls[-2:]], ["/api/runs/C9sL5j_iUiE/saved-run"] * 2)

    def test_status_restart_recovery_is_read_only_and_preserves_ids_on_failure(self):
        code, _, err, calls = self.invoke([
            "status", "saved-job", "--wait", "--video-id", "C9sL5j_iUiE", "--run-id", "saved-run",
        ], [orbit.OrbitError("Unknown job", http_status=404), orbit.OrbitError("Temporarily unavailable")])
        self.assertEqual(code, 1)
        self.assertEqual([call.args for call in calls], [("/api/jobs/saved-job",), ("/api/runs/C9sL5j_iUiE/saved-run",)])
        self.assertIn("status saved-job --wait --video-id C9sL5j_iUiE --run-id saved-run", err)

    def test_restart_fallback_does_not_hide_non404_job_errors(self):
        code, _, err, calls = self.invoke([
            "status", "saved-job", "--video-id", "C9sL5j_iUiE", "--run-id", "saved-run",
        ], [orbit.OrbitError("Failed to read local job", http_status=500)])
        self.assertEqual(code, 1)
        self.assertIn("Failed to read local job", err)
        self.assertEqual(len(calls), 1)

    def test_recovery_requires_both_valid_identifiers_before_reading(self):
        for options in (["--video-id", "C9sL5j_iUiE"], ["--run-id", "saved-run"],
                        ["--video-id", "C9sL5j_iUiE", "--run-id", "../unsafe"]):
            code, _, _, calls = self.invoke(["status", "saved-job", *options], [])
            self.assertEqual(code, 1)
            self.assertEqual(calls, [])

    def test_http_error_preserves_status_for_read_only_recovery(self):
        with patch.object(orbit, "build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError(orbit.BASE_URL + "/api/jobs/old", 404,
                                                            "Not Found", {}, io.BytesIO(b'{"error":"Unknown job"}'))
            with self.assertRaises(orbit.OrbitError) as caught:
                orbit.api("/api/jobs/old")
            self.assertEqual(caught.exception.http_status, 404)
            self.assertEqual(str(caught.exception), "Unknown job")
            opener.return_value.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
