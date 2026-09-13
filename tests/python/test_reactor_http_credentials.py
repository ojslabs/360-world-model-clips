"""Browser-owned Reactor routes, with real local HTTP and mocked providers."""
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
from urllib.parse import urlencode

from app import fal_credentials
from app import media
from app import reactor_api
from app import reactor_credentials
from app import reactor_remix
from app import server

try:
    from app import demo_auth
except ImportError:
    demo_auth = None

KEY_A = "rk_test_browser_a_never_valid"
KEY_B = "rk_test_browser_b_never_valid"
OWNER_KEY = "rk_test_owner_never_use_for_http"
FAL_KEY = "test_fal_browser_key_never_valid"
JWT = "test_scoped_browser_jwt"
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


class ReactorHTTPTests(unittest.TestCase):
    hosted = False

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.queue = Queue()
        self.logs = io.StringIO()
        self.verify = Mock(return_value=True)
        self.upstream = Mock(return_value={"jwt": JWT})
        self.remix_provider = Mock(side_effect=self.generated)
        environment = {"REACTOR_API_KEY": OWNER_KEY, "FAL_KEY": "test_owner_fal_key"}
        if self.hosted:
            environment.update(DEMO_PASSWORD=PASSWORD, PUBLIC_ORIGIN=ORIGIN)
        replacements = [patch.dict(os.environ, environment, clear=True),
                        patch.object(server, "DATA", self.root), patch.object(server, "JOBS", {}),
                        patch.object(reactor_credentials, "STORE", {}),
                        patch.object(fal_credentials, "STORE", {}),
                        patch.object(reactor_credentials, "verify_key", self.verify),
                        patch.object(fal_credentials, "verify_key", return_value=True),
                        patch.object(reactor_credentials.threading, "Timer"),
                        patch.object(reactor_api, "api_key", return_value=OWNER_KEY),
                        patch.object(reactor_api, "request", self.upstream),
                        patch.object(reactor_remix, "generate", self.remix_provider),
                        patch("sys.stderr", self.logs)]
        replacements += [patch.object(server, name, self.queue) for name in vars(server)
                         if name == "POOL" or name.endswith("_POOL")]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.auth_cookie = self.login() if self.hosted else ""
        self.target = server.folder(VIDEO) / "exports/original-run"
        self.target.mkdir(parents=True)
        self.original = self.target / "highlight.mp4"
        self.original.write_bytes(b"completed original fixture")
        self.details = {"duration": 20, "width": 1920, "height": 1080, "fps": 30,
                        "video_codec": "h264", "audio_codec": "aac", "decoded_video": True}
        media.write_json(server.manifest(VIDEO), {
            "id": VIDEO, "title": "Completed fixture", "media": self.details,
            "candidates": [], "exports": [], "generations": [{
                "id": "original-run", "provider": "Fal", "status": "complete",
                "composites": [{"id": "finished", "run_id": "original-run", "media": self.details,
                                "url": f"/media/{VIDEO}/exports/original-run/highlight.mp4"}]}]})

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)

    def request(self, path="/api/credentials/reactor", *, method="GET", data=None,
                cookie="", authenticated=True, origin=True, host=None, raw=None,
                content_type="application/json"):
        expected = ORIGIN if self.hosted else f"http://127.0.0.1:{self.http.server_port}"
        headers = {"Host": host or expected.split("://", 1)[1], "Content-Type": content_type}
        if origin is not None:
            headers["Origin"] = expected if origin is True else origin
        cookies = [self.auth_cookie if authenticated else "", cookie]
        if any(cookies):
            headers["Cookie"] = "; ".join(value for value in cookies if value)
        body = raw if raw is not None else json.dumps(data if data is not None else {}).encode() if method == "POST" else None
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def login(self):
        self.auth_cookie = ""
        code, headers, _ = self.request("/login", method="POST", authenticated=False,
            raw=urlencode({"password": PASSWORD}).encode(), content_type="application/x-www-form-urlencoded")
        self.assertEqual(code, 303)
        return headers["Set-Cookie"].split(";", 1)[0]

    def connect(self, key=KEY_A, cookie="", provider="reactor"):
        code, headers, body = self.request(f"/api/credentials/{provider}", method="POST",
                                           data={"key": key}, cookie=cookie)
        self.assertEqual(code, 200, body.decode())
        self.assertTrue(json.loads(body)["configured"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(key, body.decode() + json.dumps(headers))
        return headers["Set-Cookie"].split(";", 1)[0]

    def account(self, cookie="", provider="reactor"):
        code, headers, body = self.request(f"/api/credentials/{provider}", cookie=cookie)
        self.assertEqual(code, 200, body.decode())
        self.assertEqual(headers["Cache-Control"], "no-store")
        return json.loads(body)

    def remix(self, cookie="", batch=False):
        body = {"video_id": VIDEO, "preset_id": "day-to-night"}
        body.update({"composite_ids": ["finished"]} if batch else {"composite_id": "finished"})
        return self.request("/api/remix", method="POST", cookie=cookie, data=body)

    def generated(self, source, output, prompt, *, on_progress=None, credential_key=None, **options):
        self.assertEqual(Path(source).resolve(), self.original.resolve())
        self.assertIn(credential_key, (KEY_A, KEY_B))
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"mocked Reactor result")
        return {"path": str(output), "media": self.details, "original_audio_preserved": True,
                "timeline_preservation_verified": False}

    def assert_no_secrets(self, response=b""):
        values = [response.decode(), self.logs.getvalue()]
        values += [path.read_text() for path in self.root.rglob("*.json")]
        for value in values:
            for secret in (OWNER_KEY, KEY_A, KEY_B, FAL_KEY):
                self.assertNotIn(secret, value)

    def test_blank_browser_never_uses_owner_key_or_starts_paid_work(self):
        self.assertFalse(self.account()["configured"])
        for path in ("/api/state", "/api/remix-presets"):
            code, _, body = self.request(path)
            self.assertEqual(code, 200)
            value = json.loads(body)
            self.assertFalse(value["reactor_credentials"]["configured"] if path == "/api/state"
                             else value["configured"])
            self.assert_no_secrets(body)
        for result in (self.remix(), self.remix(batch=True),
                       self.request("/api/reactor/live-token", method="POST")):
            self.assertGreaterEqual(result[0], 400)
            self.assertIn("error", json.loads(result[2]))
            self.assert_no_secrets(result[2])
        self.assertEqual(self.queue.pending, [])
        self.assertEqual(server.JOBS, {})
        self.verify.assert_not_called()
        self.upstream.assert_not_called()
        self.remix_provider.assert_not_called()

    def test_two_browser_keys_mint_tokens_for_the_correct_account(self):
        cookie_a, cookie_b = self.connect(KEY_A), self.connect(KEY_B)
        self.assertNotEqual(cookie_a, cookie_b)
        for cookie, key in ((cookie_a, KEY_A), (cookie_b, KEY_B)):
            self.assertTrue(self.account(cookie)["configured"])
            code, headers, body = self.request("/api/reactor/live-token", method="POST", cookie=cookie)
            self.assertEqual(code, 200, body.decode())
            self.assertEqual(json.loads(body)["jwt"], JWT)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(self.upstream.call_args.args[0], "/tokens")
            self.assertEqual(self.upstream.call_args.kwargs["key"], key)
            self.assert_no_secrets(body)
        self.assertFalse(self.account()["configured"])
        self.assertEqual(self.upstream.call_count, 2)
        self.assertEqual(self.queue.pending, [])

    def test_fal_and_reactor_connections_have_independent_cookies_and_disconnects(self):
        fal_cookie = self.connect(FAL_KEY, provider="fal")
        self.assertFalse(self.account(fal_cookie)["configured"])
        reactor_cookie = self.connect(KEY_A, fal_cookie)
        self.assertNotEqual(fal_cookie.split("=", 1)[0], reactor_cookie.split("=", 1)[0])
        both = fal_cookie + "; " + reactor_cookie
        self.request("/api/credentials/reactor/clear", method="POST", cookie=both)
        self.assertFalse(self.account(both)["configured"])
        self.assertTrue(self.account(both, "fal")["configured"])
        reactor_cookie = self.connect(KEY_B, fal_cookie)
        both = fal_cookie + "; " + reactor_cookie
        self.request("/api/credentials/fal/clear", method="POST", cookie=both)
        self.assertTrue(self.account(both)["configured"])
        self.assertFalse(self.account(both, "fal")["configured"])
        self.upstream.assert_not_called()
        self.assert_no_secrets()

    def test_key_form_requires_same_origin_small_json_and_never_reflects_keys(self):
        for options in ({"origin": None}, {"origin": "https://evil.example"}, {"host": "evil.example"}):
            code, _, body = self.request(method="POST", data={"key": KEY_A}, **options)
            self.assertEqual(code, 403)
            self.assert_no_secrets(body)
        for options in ({"data": []}, {"raw": b"{"}, {"raw": b"x" * 65537},
                        {"raw": urlencode({"key": KEY_A}).encode(),
                         "content_type": "application/x-www-form-urlencoded"}):
            code, _, body = self.request(method="POST", **options)
            self.assertGreaterEqual(code, 400)
            self.assert_no_secrets(body)
        self.verify.assert_not_called()
        self.upstream.assert_not_called()
        self.assertFalse(self.account()["configured"])

    def test_cookie_is_opaque_http_only_strict_and_https_secure(self):
        code, headers, body = self.request(method="POST", data={"key": KEY_A})
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Max-Age=28800", cookie)
        if self.hosted:
            self.assertIn("Secure", cookie)
        self.assert_no_secrets(body + cookie.encode())

    def test_disconnect_and_expiry_reject_future_work_without_affecting_another_browser(self):
        cookie_a, cookie_b = self.connect(KEY_A), self.connect(KEY_B)
        code, headers, body = self.request("/api/credentials/reactor/clear", method="POST", cookie=cookie_a)
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertFalse(json.loads(body)["configured"])
        self.assertTrue(self.account(cookie_b)["configured"])
        self.assertGreaterEqual(self.remix(cookie_a)[0], 400)
        credential = reactor_credentials.get(cookie_b)
        with patch.object(reactor_credentials.time, "time", return_value=credential.expires_at + 1):
            self.assertFalse(self.account(cookie_b)["configured"])
            self.assertGreaterEqual(self.request("/api/reactor/live-token", method="POST", cookie=cookie_b)[0], 400)
        self.assertEqual(self.queue.pending, [])
        self.upstream.assert_not_called()
        self.assert_no_secrets()

    def test_queued_remix_keeps_its_original_key_after_replacement_and_disconnect(self):
        cookie_a = self.connect(KEY_A)
        credential_a = reactor_credentials.get(cookie_a)
        code, _, body = self.remix(cookie_a)
        self.assertEqual(code, 200, body.decode())
        result = json.loads(body)
        cookie_b = self.connect(KEY_B, cookie_a)
        self.request("/api/credentials/reactor/clear", method="POST", cookie=cookie_b)
        self.assertFalse(self.account(cookie_a)["configured"])
        self.queue.run_next()
        self.remix_provider.assert_called_once()
        self.assertEqual(self.remix_provider.call_args.kwargs["credential_key"], KEY_A)
        saved = next(item for item in server.load_project(VIDEO)["remixes"] if item["id"] == result["remix_id"])
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["credential_owner"], credential_a.fingerprint)
        self.assertEqual(saved["credential_source"], "browser")
        self.assertEqual(self.original.read_bytes(), b"completed original fixture")
        self.assert_no_secrets()

    def test_remix_cache_is_scoped_to_key_and_repeated_clicks_do_not_create_another_job(self):
        cookie_a, cookie_b = self.connect(KEY_A), self.connect(KEY_B)
        first = json.loads(self.remix(cookie_a)[2])
        repeated = json.loads(self.remix(cookie_a)[2])
        other = json.loads(self.remix(cookie_b)[2])
        self.assertEqual(first["job_id"], repeated["job_id"])
        self.assertNotEqual(first["remix_id"], other["remix_id"])
        self.assertEqual(len(self.queue.pending), 2)
        self.queue.run_next()
        self.queue.run_next()
        self.assertEqual([call.kwargs["credential_key"] for call in self.remix_provider.call_args_list],
                         [KEY_A, KEY_B])
        self.assertEqual(json.loads(self.remix(cookie_a)[2])["remix_id"], first["remix_id"])
        self.assertEqual(self.queue.pending, [])
        self.assert_no_secrets()

    def test_provider_errors_never_expose_browser_key_in_response_or_persistent_job(self):
        cookie = self.connect()
        self.upstream.side_effect = reactor_api.ReactorError("Upstream rejected " + KEY_A)
        code, _, body = self.request("/api/reactor/live-token", method="POST", cookie=cookie)
        self.assertGreaterEqual(code, 400)
        self.assert_no_secrets(body)
        self.remix_provider.side_effect = reactor_remix.RemixError("Upstream rejected " + KEY_A)
        code, _, body = self.remix(cookie)
        self.assertEqual(code, 200, body.decode())
        result = json.loads(body)
        self.queue.run_next()
        self.assertEqual(server.get_job(result["job_id"])["status"], "failed")
        self.assert_no_secrets()


@unittest.skipIf(demo_auth is None, "Hosted authentication belongs to the standalone copy.")
class HostedReactorHTTPTests(ReactorHTTPTests):
    hosted = True

    def test_shared_host_authentication_gates_credentials_remixes_and_live_tokens(self):
        for path, method, data in (("/api/credentials/reactor", "GET", None),
                                  ("/api/credentials/reactor", "POST", {"key": KEY_A}),
                                  ("/api/credentials/reactor/clear", "POST", {}),
                                  ("/api/reactor/live-token", "POST", {}),
                                  ("/api/remix", "POST", {"video_id": VIDEO, "composite_id": "finished"})):
            code, _, body = self.request(path, method=method, data=data, authenticated=False)
            self.assertEqual(code, 401)
            self.assertEqual(json.loads(body)["login_url"], "/login")
            self.assert_no_secrets(body)
        self.verify.assert_not_called()
        self.upstream.assert_not_called()
        self.assertEqual(self.queue.pending, [])

    def test_logout_revokes_both_credentials_even_if_cookies_are_replayed(self):
        fal_cookie = self.connect(FAL_KEY, provider="fal")
        reactor_cookie = self.connect()
        both = fal_cookie + "; " + reactor_cookie
        code, _, _ = self.request("/logout", method="POST", cookie=both)
        self.assertEqual(code, 303)
        self.assertIsNone(reactor_credentials.get(both))
        self.assertIsNone(fal_credentials.get(both))
        self.assertFalse(self.account(both)["configured"])
        self.assertGreaterEqual(self.request("/api/reactor/live-token", method="POST", cookie=both)[0], 400)
        self.upstream.assert_not_called()

    def test_new_login_cannot_inherit_an_old_reactor_connection(self):
        cookie = self.connect()
        code, _, _ = self.request("/login", method="POST", cookie=cookie, authenticated=False,
            raw=urlencode({"password": PASSWORD}).encode(), content_type="application/x-www-form-urlencoded")
        self.assertEqual(code, 303)
        self.assertIsNone(reactor_credentials.get(cookie))
        self.assert_no_secrets()


if __name__ == "__main__":
    unittest.main()
