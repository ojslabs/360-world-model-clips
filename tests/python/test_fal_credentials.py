"""Browser-owned Fal credentials, isolated HTTP clients and offline workers."""
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from PIL import Image

from app import fal_camera
from app import action_labels
from app import fal_credentials
from app import media
from app import server

try:
    from app import demo_auth
except ImportError:
    demo_auth = None

OWNER_KEY = "test-owner-key-never-use-for-browser"
KEY_A = "test-browser-a-key-never-valid"
KEY_B = "test-browser-b-key-never-valid"
ORIGIN = "https://clips.example.test"
PASSWORD = "test-only-long-demo-password"
VIDEO = "C9sL5j_iUiE"


class Queue:
    def __init__(self):
        self.pending = []

    def submit(self, operation):
        self.pending.append(operation)

    def run_next(self):
        self.pending.pop(0)()


class FalCredentialTests(unittest.TestCase):
    hosted = False

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.queue = Queue()
        self.logs = io.StringIO()
        environment = {"FAL_KEY": OWNER_KEY}
        if self.hosted:
            environment.update(DEMO_PASSWORD=PASSWORD, PUBLIC_ORIGIN=ORIGIN)
        replacements = [patch.dict(os.environ, environment, clear=True),
                        patch.object(server, "DATA", self.root), patch.object(server, "JOBS", {}),
                        patch.object(fal_camera, "ROOT", self.root),
                        patch.object(fal_credentials, "STORE", {}),
                        patch.object(fal_credentials, "verify_key", return_value=True),
                        patch.object(fal_credentials.threading, "Timer"),
                        patch.object(fal_camera, "_json_request", side_effect=AssertionError("No provider request")),
                        patch("sys.stderr", self.logs)]
        for name in vars(server):
            if name == "POOL" or name.endswith("_POOL"):
                replacements.append(patch.object(server, name, self.queue))
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.auth_cookie = self.login() if self.hosted else ""
        location = server.folder(VIDEO)
        (location / "frames").mkdir(parents=True)
        Image.new("RGB", (160, 90), "red").save(location / "frames/moment.png")
        (location / "source.mp4").write_bytes(b"offline source fixture")
        media.write_json(server.manifest(VIDEO), {
            "id": VIDEO, "title": "Saved source", "status": "ready",
            "media": {"duration": 90, "fps": 30, "width": 1920, "height": 1080},
            "candidates": [{"id": "moment", "selected": True, "freeze_time": 30,
                            "tail_seconds": media.DEFAULTS["tail_seconds"],
                            "frame_url": f"/media/{VIDEO}/frames/moment.png"}],
            "generations": []})

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)

    def request(self, path="/api/credentials/fal", *, method="GET", data=None,
                cookie="", authenticated=True, origin=True, host=None, raw=None,
                content_type="application/json"):
        expected = ORIGIN if self.hosted else f"http://127.0.0.1:{self.http.server_port}"
        headers = {"Host": host or expected.split("://", 1)[1], "Content-Type": content_type}
        if origin is not None:
            headers["Origin"] = expected if origin is True else origin
        cookies = [self.auth_cookie if authenticated else "", cookie]
        if any(cookies):
            headers["Cookie"] = "; ".join(value for value in cookies if value)
        body = raw if raw is not None else json.dumps(data or {}).encode() if method == "POST" else None
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def login(self):
        self.auth_cookie = ""
        status, headers, _ = self.request("/login", method="POST", authenticated=False,
            raw=urlencode({"password": PASSWORD}).encode(), content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 303)
        return headers["Set-Cookie"].split(";", 1)[0]

    def connect(self, key=KEY_A, cookie=""):
        status, headers, raw = self.request(method="POST", data={"key": key}, cookie=cookie)
        self.assertEqual(status, 200, raw.decode())
        self.assertTrue(json.loads(raw)["configured"])
        self.assertNotIn(key, raw.decode())
        self.assertNotIn(key, json.dumps(headers))
        self.assertEqual(headers["Cache-Control"], "no-store")
        return headers["Set-Cookie"].split(";", 1)[0]

    def configured(self, cookie=""):
        status, _, raw = self.request(cookie=cookie)
        self.assertEqual(status, 200, raw.decode())
        return json.loads(raw)["configured"]

    def generate(self, cookie=""):
        return self.request("/api/generate", method="POST", cookie=cookie,
                            data={"video_id": VIDEO, "mode": "fal-h3-max", "item_id": "moment"})

    def assert_no_saved_secrets(self):
        for path in self.root.rglob("*"):
            if path.is_file() and path.suffix != ".png":
                contents = path.read_bytes()
                for secret in (OWNER_KEY, KEY_A, KEY_B):
                    self.assertNotIn(secret.encode(), contents, str(path))
        for secret in (OWNER_KEY, KEY_A, KEY_B):
            self.assertNotIn(secret, self.logs.getvalue())

    def test_owner_key_does_not_configure_an_unconnected_browser_or_authorize_generation(self):
        self.assertFalse(self.configured())
        status, _, raw = self.generate()
        self.assertGreaterEqual(status, 400)
        self.assertIn("error", json.loads(raw))
        self.assertEqual(self.queue.pending, [])
        self.assertEqual(server.JOBS, {})
        self.assertEqual(server.load_project(VIDEO)["generations"], [])
        status, _, raw = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["generation"]["configured"])
        self.assert_no_saved_secrets()

    def test_distinct_browser_cookies_do_not_share_keys_and_clear_is_scoped(self):
        cookie_a = self.connect(KEY_A)
        cookie_b = self.connect(KEY_B)
        self.assertNotEqual(cookie_a, cookie_b)
        self.assertTrue(self.configured(cookie_a))
        self.assertTrue(self.configured(cookie_b))
        self.assertFalse(self.configured())
        status, _, raw = self.request("/api/credentials/fal/clear", method="POST", cookie=cookie_a)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["configured"])
        self.assertFalse(self.configured(cookie_a))
        self.assertTrue(self.configured(cookie_b))
        self.assert_no_saved_secrets()

    def test_cookie_is_opaque_http_only_strict_and_secure_for_https(self):
        status, headers, raw = self.request(method="POST", data={"key": KEY_A})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        if self.hosted:
            self.assertIn("Secure", cookie)
        self.assertNotIn(KEY_A, cookie + raw.decode())
        self.assert_no_saved_secrets()

    def test_bad_keys_are_not_reflected_or_saved(self):
        for key in ("", "secret with spaces", "private\nheader", 17, None, {"key": KEY_A}):
            with self.subTest(kind=type(key).__name__):
                status, _, raw = self.request(method="POST", data={"key": key})
                self.assertEqual(status, 400)
                if isinstance(key, str) and key:
                    self.assertNotIn(key, raw.decode())
                self.assertFalse(self.configured())
        self.assert_no_saved_secrets()

    def test_wrong_origin_or_host_cannot_connect_clear_or_generate(self):
        cookie = self.connect()
        for path, data in (("/api/credentials/fal", {"key": KEY_B}),
                           ("/api/credentials/fal/clear", {}),
                           ("/api/generate", {"video_id": VIDEO, "mode": "fal-h3-max"})):
            for options in ({"origin": "https://evil.example"}, {"host": "evil.example"}):
                status, _, _ = self.request(path, method="POST", data=data, cookie=cookie, **options)
                self.assertEqual(status, 403)
        self.assertTrue(self.configured(cookie))
        self.assertEqual(self.queue.pending, [])

    def test_expired_or_forged_cookie_cannot_use_owner_key(self):
        cookie = self.connect()
        credential = fal_credentials.get(cookie)
        self.assertIsNotNone(credential)
        with patch.object(fal_credentials.time, "time", return_value=credential.expires_at + 1):
            self.assertFalse(self.configured(cookie))
            status, _, _ = self.generate(cookie)
            self.assertGreaterEqual(status, 400)
        forged = cookie.rsplit("=", 1)[0] + "=not-a-valid-session-token"
        self.assertFalse(self.configured(forged))
        self.assertEqual(self.queue.pending, [])
        self.assert_no_saved_secrets()

    def test_orbit_worker_keeps_submitted_key_after_browser_clear_and_hides_it_on_failure(self):
        cookie = self.connect(KEY_A)
        status, _, raw = self.generate(cookie)
        self.assertEqual(status, 200, raw.decode())
        job_id = json.loads(raw)["job_id"]
        self.request("/api/credentials/fal/clear", method="POST", cookie=cookie)
        self.assertFalse(self.configured(cookie))
        with patch.object(fal_camera, "generate", side_effect=fal_camera.FalError("private " + KEY_A)) as generate, \
                patch.object(fal_camera, "api_key", side_effect=AssertionError("Owner key forbidden")):
            self.queue.run_next()
        generate.assert_called_once()
        self.assertEqual(generate.call_args.kwargs["credential_key"], KEY_A)
        self.assertEqual(server.get_job(job_id)["status"], "failed")
        self.assert_no_saved_secrets()

    def test_two_clients_bind_distinct_keys_to_their_own_submitted_workers(self):
        cookie_a, cookie_b = self.connect(KEY_A), self.connect(KEY_B)
        with patch.object(fal_camera, "generate", side_effect=fal_camera.FalError("offline failure")) as generate:
            for cookie, key in ((cookie_a, KEY_A), (cookie_b, KEY_B)):
                status, _, raw = self.generate(cookie)
                self.assertEqual(status, 200, raw.decode())
                self.queue.run_next()
                self.assertEqual(generate.call_args.kwargs["credential_key"], key)
        self.assertEqual(generate.call_count, 2)
        self.assert_no_saved_secrets()

    def test_http_action_recognition_also_requires_its_browser_key(self):
        status, _, _ = self.request("/api/label", method="POST",
                                   data={"video_id": VIDEO, "item_id": "moment", "time": 30})
        self.assertGreaterEqual(status, 400)
        self.assertEqual(self.queue.pending, [])
        status, _, raw = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["action_labeling"]["ready"])

    def test_label_worker_keeps_its_submitted_key_when_browser_replaces_it(self):
        cookie_a = self.connect(KEY_A)
        status, _, raw = self.request("/api/label", method="POST", cookie=cookie_a,
            data={"video_id": VIDEO, "item_id": "moment", "time": 30})
        self.assertEqual(status, 200, raw.decode())
        cookie_b = self.connect(KEY_B, cookie_a)
        self.assertTrue(self.configured(cookie_b))
        result = {"status": "complete", "label": "Visible action", "confidence": "medium", "source_time": 30}
        with patch.object(action_labels, "label_moment", return_value=result) as label:
            self.queue.run_next()
        label.assert_called_once()
        self.assertEqual(label.call_args.kwargs["credential_key"], KEY_A)
        self.assert_no_saved_secrets()

    def test_browser_run_restart_does_not_resume_using_owner_environment(self):
        cookie = self.connect()
        status, _, raw = self.generate(cookie)
        self.assertEqual(status, 200, raw.decode())
        started = json.loads(raw)
        target = server.folder(VIDEO) / "exports" / started["run_id"]
        media.write_json(target / "fal-request.json", {"request_id": "already-paid-request", "seconds": 6})
        self.queue.pending.clear()
        self.request("/api/credentials/fal/clear", method="POST", cookie=cookie)
        with patch.object(server, "execute_generation") as execute:
            server.reconcile_jobs()
            while self.queue.pending:
                self.queue.run_next()
        execute.assert_not_called()
        self.assertEqual(server.read_run(VIDEO, started["run_id"])["status"], "interrupted")
        self.assert_no_saved_secrets()

    def test_only_reconnecting_original_key_resumes_acknowledged_request_without_a_new_submission(self):
        cookie = self.connect(KEY_A)
        status, _, raw = self.generate(cookie)
        self.assertEqual(status, 200, raw.decode())
        started = json.loads(raw)
        target = server.folder(VIDEO) / "exports" / started["run_id"]
        media.write_json(target / "fal-request.json", {"request_id": "already-paid-request", "seconds": 6})
        self.queue.pending.clear()
        fal_credentials.STORE.clear()
        server.reconcile_jobs()
        self.assertEqual(self.queue.pending, [])
        self.connect(KEY_B)
        self.assertEqual(self.queue.pending, [])
        cookie_a = self.connect(KEY_A)
        self.assertEqual(len(self.queue.pending), 1)
        for _ in range(2):
            self.request(cookie=cookie_a)
            self.request("/api/state", cookie=cookie_a)
        self.assertEqual(len(self.queue.pending), 1)
        with patch.object(server, "execute_generation", return_value={"status": "complete"}) as execute:
            self.queue.run_next()
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args, (VIDEO, started["run_id"]))
        self.assertTrue(execute.call_args.kwargs["resume_only"])
        self.assertEqual(execute.call_args.kwargs["credential"].key, KEY_A)
        self.assert_no_saved_secrets()

    def test_reconnecting_key_does_not_retry_an_unacknowledged_submission(self):
        cookie = self.connect()
        status, _, raw = self.generate(cookie)
        self.assertEqual(status, 200, raw.decode())
        self.queue.pending.clear()
        fal_credentials.STORE.clear()
        server.reconcile_jobs()
        self.connect()
        self.assertEqual(self.queue.pending, [])
        self.assertEqual(server.read_run(VIDEO, json.loads(raw)["run_id"])["status"], "interrupted")


@unittest.skipIf(demo_auth is None, "Shared-host auth belongs to the standalone copy.")
class HostedFalCredentialTests(FalCredentialTests):
    hosted = True

    def test_unauthenticated_api_requests_cannot_set_read_clear_or_use_a_key(self):
        cookie = self.connect()
        for path, method, data in (("/api/credentials/fal", "GET", None),
                                   ("/api/credentials/fal", "HEAD", None),
                                   ("/api/credentials/fal", "POST", {"key": KEY_B}),
                                   ("/api/credentials/fal/clear", "POST", {}),
                                   ("/api/generate", "POST", {"video_id": VIDEO, "mode": "fal-h3-max"})):
            status, _, _ = self.request(path, method=method, data=data, cookie=cookie, authenticated=False)
            self.assertEqual(status, 401)
        self.assertTrue(self.configured(cookie))
        self.assertEqual(self.queue.pending, [])

    def test_missing_origin_cannot_modify_browser_credentials(self):
        status, _, _ = self.request(method="POST", data={"key": KEY_A}, origin=None)
        self.assertEqual(status, 403)
        self.assertFalse(self.configured())

    def test_logout_revokes_the_browser_fal_key_even_if_its_cookie_is_replayed(self):
        cookie = self.connect()
        status, _, _ = self.request("/logout", method="POST", cookie=cookie)
        self.assertEqual(status, 303)
        self.assertIsNone(fal_credentials.get(cookie))
        self.assertFalse(self.configured(cookie))
        self.assert_no_saved_secrets()

    def test_new_login_does_not_inherit_a_previous_browser_fal_credential(self):
        cookie = self.connect()
        status, _, _ = self.request("/login", method="POST", cookie=cookie, authenticated=False,
            raw=urlencode({"password": PASSWORD}).encode(), content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 303)
        self.assertIsNone(fal_credentials.get(cookie))
        self.assert_no_saved_secrets()


class CredentialBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.frame = self.root / "frame.png"
        Image.new("RGB", (160, 90), "blue").save(self.frame)

    def test_credential_snapshot_is_immutable_and_repr_omits_key(self):
        with patch.object(fal_credentials, "STORE", {}), \
                patch.object(fal_credentials, "verify_key", return_value=True), \
                patch.object(fal_credentials.threading, "Timer"):
            token, credential = fal_credentials.set_key("", KEY_A)
            with self.assertRaises(AttributeError):
                credential.key = KEY_B
            self.assertNotIn(KEY_A, repr(credential))
            cookie = fal_credentials.cookie_header(token)
            fal_credentials.clear(cookie)
            self.assertIsNone(fal_credentials.get(cookie))
            self.assertEqual(credential.key, KEY_A)

    def test_key_verification_is_one_read_only_account_request_and_errors_hide_credentials(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"{}"
        opener = Mock()
        opener.open.return_value = response
        with patch.object(fal_credentials, "build_opener", return_value=opener):
            self.assertTrue(fal_credentials.verify_key(KEY_A))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.fal.ai/v1/storage/settings")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Key " + KEY_A)
        for failure in (HTTPError(request.full_url, 401, KEY_A, {}, io.BytesIO(KEY_A.encode())),
                        URLError(KEY_A)):
            opener.open.side_effect = failure
            with patch.object(fal_credentials, "build_opener", return_value=opener):
                with self.assertRaises(fal_credentials.CredentialError) as raised:
                    fal_credentials.verify_key(KEY_A)
            self.assertNotIn(KEY_A, str(raised.exception))

    def test_account_settings_permission_denial_keeps_inference_key_explicitly_unverified(self):
        opener = Mock()
        opener.open.side_effect = HTTPError("https://api.fal.ai/v1/storage/settings", 403, KEY_A, {},
                                           io.BytesIO(KEY_A.encode()))
        with patch.object(fal_credentials, "build_opener", return_value=opener), \
                patch.object(fal_credentials, "STORE", {}), patch.object(fal_credentials.threading, "Timer"):
            token, credential = fal_credentials.set_key("", KEY_A)
            status = fal_credentials.status(fal_credentials.cookie_header(token))
        self.assertTrue(status["configured"])
        self.assertFalse(status["verified"])
        self.assertFalse(credential.verified)
        self.assertNotIn(KEY_A, json.dumps(status))
        self.assertEqual(opener.open.call_args.args[0].get_method(), "GET")

    def test_explicit_adapter_key_bypasses_owner_loader_and_is_not_written_to_request_receipt(self):
        with patch.dict(os.environ, {"FAL_KEY": OWNER_KEY}), \
                patch.object(fal_camera, "_json_request", side_effect=fal_camera.FalError("offline stop")) as request:
            with self.assertRaisesRegex(fal_camera.FalError, "offline stop"):
                fal_camera.generate(self.frame, self.root / "run", media.ORBIT_PRESET["input"]["prompt"],
                                    credential_key=KEY_A)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1], KEY_A)
        receipt = (self.root / "run/fal-request.json").read_text()
        self.assertNotIn(KEY_A, receipt)
        self.assertNotIn(OWNER_KEY, receipt)

    def test_acknowledged_browser_receipt_cannot_resume_with_a_different_key(self):
        target = self.root / "run"
        prompt = media.ORBIT_PRESET["input"]["prompt"]
        with patch.dict(os.environ, {"FAL_KEY": OWNER_KEY}), \
                patch.object(fal_camera, "_json_request", side_effect=[
                    {"request_id": "saved-paid-request"}, fal_camera.FalError("offline queue")]):
            with self.assertRaisesRegex(fal_camera.FalError, "offline queue"):
                fal_camera.generate(self.frame, target, prompt, credential_key=KEY_A)
        self.assertEqual(media.read_json(target / "fal-request.json")["request_id"], "saved-paid-request")
        for options in ({"credential_key": KEY_B}, {}):
            with self.subTest(explicit_key=bool(options)), patch.dict(os.environ, {"FAL_KEY": OWNER_KEY}), \
                    patch.object(fal_camera, "_json_request") as request:
                with self.assertRaises(fal_camera.FalError):
                    fal_camera.generate(self.frame, target, prompt, resume_only=True, **options)
            request.assert_not_called()

    def test_action_label_explicit_key_never_uses_owner_and_error_receipt_is_redacted(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"offline source")
        with patch.object(fal_camera, "api_key", side_effect=AssertionError("Owner key forbidden")), \
                patch.object(action_labels, "_contact_sheet", return_value=(b"offline sheet", [29.5, 30, 30.5])), \
                patch.object(action_labels, "_infer", side_effect=RuntimeError("private " + KEY_A)) as infer:
            result = action_labels.label_moment(source, 30, self.root / "labels", frame_path=self.frame,
                                                credential_key=KEY_A)
        infer.assert_called_once()
        self.assertNotIn(KEY_A, json.dumps(result))
        for path in (self.root / "labels").glob("*.json"):
            self.assertNotIn(KEY_A, path.read_text(), str(path))


if __name__ == "__main__":
    unittest.main()
