"""Shared-demo authentication, using isolated environment and local HTTP only."""
import concurrent.futures
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import demo_auth

PASSWORD = "test-only-shared-passcode-123"
SECRET = "test-only-session-secret-at-least-32-characters"
ORIGIN = "https://clips.example.test"


class Handler:
    def __init__(self, method="GET", path="/", body=b"", headers=None, ip="192.0.2.5"):
        self.command, self.path = method, path
        self.server = SimpleNamespace(server_port=8476)
        self.client_address = (ip, 50000)
        self.headers = {"Host": "clips.example.test", **(headers or {})}
        self.rfile, self.wfile = io.BytesIO(body), io.BytesIO()
        self.response_headers = {}
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass


class DemoAuthTests(unittest.TestCase):
    def setUp(self):
        for replacement in (patch.dict(os.environ, {"DEMO_PASSWORD": PASSWORD, "PUBLIC_ORIGIN": ORIGIN,
                                                    "DEMO_SESSION_SECRET": SECRET}, clear=True),
                            patch.object(demo_auth, "FAILURES", {})):
            replacement.start()
            self.addCleanup(replacement.stop)

    def gate(self, handler):
        if not demo_auth.trusted(handler):
            handler.send_response(403)
            return True
        return demo_auth.intercept(handler, head=handler.command == "HEAD")

    def login(self, password=PASSWORD, next_path="/", **kwargs):
        body = urlencode({"password": password, "next": next_path}).encode()
        handler = Handler("POST", "/login", body, {"Origin": ORIGIN,
            "Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body)),
            **kwargs.pop("headers", {})}, **kwargs)
        self.assertTrue(self.gate(handler))
        return handler

    def token_cookie(self):
        return self.login().response_headers["Set-Cookie"].split(";", 1)[0]

    def test_external_bind_requires_strong_password_and_canonical_https_origin(self):
        demo_auth.validate_deployment("0.0.0.0")
        for values in ({"DEMO_PASSWORD": ""}, {"DEMO_PASSWORD": "short"}, {"PUBLIC_ORIGIN": ""},
                       {"PUBLIC_ORIGIN": "http://clips.example.test"}, {"PUBLIC_ORIGIN": ORIGIN + "/"},
                       {"PUBLIC_ORIGIN": ORIGIN + "/app"}, {"PUBLIC_ORIGIN": ORIGIN + "?secret=x"},
                       {"PUBLIC_ORIGIN": "https://user:pass@clips.example.test"},
                       {"DEMO_SESSION_SECRET": "short"}):
            with self.subTest(values=list(values)), patch.dict(os.environ, values), self.assertRaises(ValueError):
                demo_auth.validate_deployment("0.0.0.0")
        with patch.dict(os.environ, {"PUBLIC_ORIGIN": "http://localhost:8476"}):
            demo_auth.validate_deployment("127.0.0.1")

    def test_local_mode_preserves_cli_and_rejects_foreign_hosts_and_origins(self):
        with patch.dict(os.environ, {}, clear=True):
            demo_auth.validate_deployment("127.0.0.1")
            local = Handler("POST", "/api/generate", headers={"Host": "localhost:8476"})
            self.assertFalse(self.gate(local))
            local.headers["Origin"] = "https://evil.example"
            self.assertTrue(self.gate(local))
            self.assertEqual(local.status, 403)
            self.assertFalse(demo_auth.trusted(Handler()))

    def test_all_media_assets_and_api_mutations_require_authentication_including_head(self):
        for method, path in (("GET", "/"), ("HEAD", "/media/example/output.mp4"),
                             ("GET", "/assets/font.woff2"), ("GET", "/app.js"),
                             ("POST", "/api/generate"), ("HEAD", "/api/state")):
            with self.subTest(method=method, path=path):
                handler = Handler(method, path, headers={"Origin": ORIGIN})
                self.assertTrue(self.gate(handler))
                self.assertEqual(handler.status, 401 if path.startswith("/api/") else 303)
                if method == "HEAD":
                    self.assertEqual(handler.wfile.getvalue(), b"")
                elif path.startswith("/api/"):
                    self.assertEqual(json.loads(handler.wfile.getvalue())["login_url"], "/login")
                else:
                    self.assertTrue(handler.response_headers["Location"].startswith("/login?next="))

    def test_health_is_public_minimal_and_loopback_host_only_bypasses_for_health(self):
        for host in ("clips.example.test", "127.0.0.1:8476"):
            handler = Handler(path="/healthz", headers={"Host": host})
            self.assertTrue(self.gate(handler))
            self.assertEqual(handler.status, 200)
            self.assertEqual(json.loads(handler.wfile.getvalue()), {"ok": True})
        self.assertFalse(demo_auth.trusted(Handler(path="/api/state", headers={"Host": "127.0.0.1:8476"})))
        self.assertFalse(demo_auth.trusted(Handler("POST", "/healthz", headers={"Host": "127.0.0.1:8476"})))

    def test_wrong_password_cannot_create_session_and_valid_cookie_protects_all_routes(self):
        wrong = self.login("wrong")
        self.assertEqual(wrong.status, 401)
        self.assertNotIn("Set-Cookie", wrong.response_headers)
        valid = self.login(next_path="/?source=abc")
        self.assertEqual(valid.status, 303)
        self.assertEqual(valid.response_headers["Location"], "/?source=abc")
        cookie = valid.response_headers["Set-Cookie"]
        for attribute in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/", "Max-Age=86400"):
            self.assertIn(attribute, cookie)
        for path in ("/", "/media/video/file.mp4", "/api/state", "/assets/font.woff2"):
            self.assertFalse(self.gate(Handler(path=path, headers={"Cookie": cookie})))

    def test_login_redirects_stay_relative_even_with_encoded_controls_or_slashes(self):
        for value in ("https://evil.example", "//evil.example", "/%2fevil.example", "/\\evil.example",
                      "/%5cevil.example", "/ok%0d%0aLocation:evil", "/\x00bad"):
            with self.subTest(value=value):
                result = self.login(next_path=value)
                self.assertEqual(result.response_headers["Location"], "/")
        handler = Handler(path="/login?" + urlencode({"next": '/?x="<script>alert(1)</script>'}))
        self.assertTrue(self.gate(handler))
        self.assertNotIn(b"<script>", handler.wfile.getvalue())

    def test_cookie_expiry_tampering_and_password_rotation_fail_closed(self):
        with patch.object(demo_auth.time, "time", return_value=100000):
            cookie = self.token_cookie()
        with patch.object(demo_auth.time, "time", return_value=100001):
            self.assertFalse(self.gate(Handler(headers={"Cookie": cookie})))
            self.assertTrue(self.gate(Handler(headers={"Cookie": cookie[:-1] + ("a" if cookie[-1] != "a" else "b")})))
            with patch.dict(os.environ, {"DEMO_PASSWORD": PASSWORD + "-rotated"}):
                self.assertTrue(self.gate(Handler(headers={"Cookie": cookie})))
        with patch.object(demo_auth.time, "time", return_value=100000 + demo_auth.SESSION_SECONDS):
            self.assertTrue(self.gate(Handler(headers={"Cookie": cookie})))

    def test_post_requires_actual_matching_origin_and_ignores_forwarded_headers(self):
        cookie = self.token_cookie()
        for origin in (None, "https://evil.example", "http://clips.example.test", "null"):
            headers = {"Cookie": cookie, "X-Forwarded-Host": "clips.example.test", "X-Forwarded-Proto": "https"}
            if origin is not None:
                headers["Origin"] = origin
            handler = Handler("POST", "/api/generate", headers=headers)
            self.assertTrue(self.gate(handler))
            self.assertEqual(handler.status, 403)
        self.assertFalse(self.gate(Handler("POST", "/api/generate", headers={"Cookie": cookie, "Origin": ORIGIN})))
        self.assertFalse(demo_auth.trusted(Handler(headers={"Host": "evil.example", "X-Forwarded-Host": "clips.example.test"})))
        result = self.login(headers={"X-Forwarded-Proto": "http"})
        self.assertIn("; Secure", result.response_headers["Set-Cookie"])

    def test_ten_failed_attempts_per_actual_peer_then_expiry_and_independent_peer(self):
        with patch.object(demo_auth.time, "monotonic", return_value=100):
            for index in range(10):
                self.assertEqual(self.login("wrong", headers={"X-Forwarded-For": f"192.0.2.{index}"}).status, 401)
            limited = self.login()
            self.assertEqual(limited.status, 429)
            self.assertEqual(limited.response_headers["Retry-After"], "60")
            self.assertEqual(self.login(ip="192.0.2.8").status, 303)
        with patch.object(demo_auth.time, "monotonic", return_value=161):
            self.assertEqual(self.login().status, 303)

    def test_rate_limit_reserves_concurrent_attempts_atomically(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            statuses = list(pool.map(lambda _: self.login("wrong").status, range(20)))
        self.assertEqual(statuses.count(401), 10)
        self.assertEqual(statuses.count(429), 10)

    def test_form_is_bounded_and_logout_clears_only_browser_cookie(self):
        bad = Handler("POST", "/login", headers={"Origin": ORIGIN, "Content-Length": "4097",
                      "Content-Type": "application/x-www-form-urlencoded"})
        self.assertTrue(self.gate(bad))
        self.assertEqual(bad.status, 400)
        cookie = self.token_cookie()
        logout = Handler("POST", "/logout", headers={"Origin": ORIGIN, "Cookie": cookie})
        self.assertTrue(self.gate(logout))
        self.assertEqual(logout.response_headers["Location"], "/login")
        self.assertIn("Max-Age=0", logout.response_headers["Set-Cookie"])
        # The shared demo has no global session database; logout clears this browser.
        self.assertFalse(self.gate(Handler(headers={"Cookie": cookie})))

    def test_html_and_api_errors_never_contain_configured_secrets(self):
        for path in ("/login", "/api/state", "/media/example/file.mp4"):
            handler = Handler(path=path)
            self.assertTrue(self.gate(handler))
            public = handler.wfile.getvalue().decode() + repr(handler.response_headers)
            self.assertNotIn(PASSWORD, public)
            self.assertNotIn(SECRET, public)

    def test_real_http_boundary_does_not_execute_unauthenticated_mutation(self):
        executed = []

        class Gate(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                if not demo_auth.trusted(self):
                    self.send_error(403)
                elif not demo_auth.intercept(self):
                    executed.append(self.path)
                    self.send_response(204)
                    self.end_headers()

        test_http = ThreadingHTTPServer(("127.0.0.1", 0), Gate)
        thread = threading.Thread(target=test_http.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", test_http.server_port, timeout=3)
            connection.request("POST", "/api/generate", body=b"{}", headers={"Host": "clips.example.test", "Origin": ORIGIN})
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()
            self.assertEqual(executed, [])
            connection = http.client.HTTPConnection("127.0.0.1", test_http.server_port, timeout=3)
            connection.request("POST", "/api/generate", body=b"{}", headers={"Host": "clips.example.test", "Origin": ORIGIN,
                               "Cookie": self.token_cookie()})
            response = connection.getresponse()
            self.assertEqual(response.status, 204)
            response.read()
            connection.close()
            self.assertEqual(executed, ["/api/generate"])
        finally:
            test_http.shutdown()
            test_http.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
