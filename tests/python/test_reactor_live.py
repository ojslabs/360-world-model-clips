"""Scoped browser tokens and protected host routes, without Reactor sessions."""
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
from urllib.error import HTTPError
from urllib.parse import urlencode

from app import reactor_api
from app import reactor_credentials
from app import reactor_live
from app import server

try:
    from app import demo_auth
except ImportError:
    demo_auth = None

KEY = "rk_test_private_server_key_never_valid"
JWT = "test-only-scoped-jwt"
ORIGIN = "https://clips.example.test"
PASSWORD = "test-only-long-demo-password"


class ReactorLiveTests(unittest.TestCase):
    def test_token_mint_is_scoped_to_one_x2_session_and_exposes_only_browser_contract(self):
        with patch.object(reactor_api, "api_key", return_value=KEY), \
                patch.object(reactor_api, "request", return_value={"jwt": JWT, "private_debug": KEY}) as request, \
                patch.dict("sys.modules", {"reactor_sdk": None}):
            result = reactor_live.token()
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], "/tokens")
        payload = request.call_args.args[1]
        self.assertEqual(payload["expires_after"], 360)
        self.assertEqual(payload["authorization_details"], [{"type": "session",
            "resources": {"models": {"match": ["xmax/x2"]}},
            "constraints": {"max_sessions": 1, "max_session_duration_seconds": 300}}])
        self.assertEqual(request.call_args.kwargs["key"], KEY)
        self.assertEqual(result["jwt"], JWT)
        self.assertEqual(result["model"], "xmax/x2")
        self.assertEqual(result["expires_in"], 360)
        self.assertEqual(result["max_session_seconds"], 300)
        self.assertGreater(reactor_live.TOKEN_SECONDS, reactor_live.SESSION_SECONDS)
        self.assertGreater(result["expires_in"], result["max_session_seconds"])
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn("private_debug", result)

    def test_existing_server_token_defaults_and_session_binding_are_preserved(self):
        with patch.object(reactor_api, "api_key", return_value=KEY), \
                patch.object(reactor_api, "request", return_value={"jwt": JWT}) as request:
            reactor_api.mint_token(session_id="saved-session")
        payload = request.call_args.args[1]
        self.assertEqual(payload["expires_after"], 900)
        authorization = payload["authorization_details"][0]
        self.assertEqual(authorization["resources"]["models"]["match"], [reactor_api.FAST_H3])
        self.assertEqual(authorization["resources"]["sessions"], {"bind": ["saved-session"]})
        self.assertEqual(authorization["constraints"], {"max_sessions": 1, "max_session_duration_seconds": 600})

    def test_http_auth_error_body_and_key_do_not_escape_token_service(self):
        error = HTTPError("https://api.reactor.inc/tokens", 401, KEY, {},
                          io.BytesIO(json.dumps({"detail": KEY, "jwt": "private-upstream-token"}).encode()))
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(reactor_api, "api_key", return_value=KEY), \
                patch.object(reactor_api, "build_opener", return_value=opener):
            with self.assertRaises(Exception) as raised:
                reactor_live.token()
        self.assertNotIn(KEY, str(raised.exception))
        self.assertNotIn("private-upstream-token", str(raised.exception))
        opener.open.assert_called_once()

    def test_get_route_never_mints_or_enqueues_a_session(self):
        handler = object.__new__(server.Handler)
        handler.command, handler.path = "GET", "/api/reactor/live-token"
        handler.headers = {}
        handler.trusted = Mock(return_value=True)
        handler.send_json, handler.send_error = Mock(), Mock()
        auth = patch.object(demo_auth, "intercept", return_value=False) if demo_auth else patch.dict(os.environ, {})
        with auth, patch.object(reactor_api, "mint_token") as mint, patch.object(server, "task") as task:
            handler.do_GET()
        mint.assert_not_called()
        task.assert_not_called()
        handler.send_error.assert_called_with(404)


@unittest.skipIf(demo_auth is None, "Protected-host checks run in the standalone copy, which owns demo_auth.")
class ProtectedReactorLiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        replacements = [patch.dict(os.environ, {"DEMO_PASSWORD": PASSWORD, "PUBLIC_ORIGIN": ORIGIN}, clear=True),
                        patch.object(demo_auth, "FAILURES", {}), patch.object(server, "DATA", self.root),
                        patch.object(server, "JOBS", {}), patch.object(reactor_api, "api_key", return_value=KEY),
                        patch.object(reactor_credentials, "STORE", {}),
                        patch.object(reactor_credentials, "verify_key", return_value=True),
                        patch.object(reactor_credentials.threading, "Timer")]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        replacement = patch.object(reactor_api, "request", return_value={"jwt": JWT})
        self.upstream = replacement.start()
        self.addCleanup(replacement.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)

    def request(self, method="POST", path="/api/reactor/live-token", *, cookie=None, origin=ORIGIN,
                body=b"{}", content_type="application/json", host="clips.example.test"):
        headers = {"Host": host, "Content-Type": content_type}
        if origin is not None:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = cookie
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        try:
            connection.request(method, path, body=body if method == "POST" else None, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def cookie(self):
        status, headers, _ = self.request(path="/login", body=urlencode({"password": PASSWORD}).encode(),
                                         content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 303)
        auth_cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, headers, _ = self.request(path="/api/credentials/reactor", cookie=auth_cookie,
                                         body=json.dumps({"key": KEY}).encode())
        self.assertEqual(status, 200)
        return auth_cookie + "; " + headers["Set-Cookie"].split(";", 1)[0]

    def test_unauthenticated_post_and_head_cannot_mint_browser_tokens(self):
        for method in ("POST", "HEAD"):
            status, _, body = self.request(method)
            self.assertEqual(status, 401)
            if method == "HEAD":
                self.assertEqual(body, b"")
            else:
                self.assertEqual(json.loads(body)["login_url"], "/login")
        self.upstream.assert_not_called()

    def test_authenticated_same_origin_post_returns_scoped_token_without_saving_a_job(self):
        status, headers, body = self.request(cookie=self.cookie())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["jwt"], JWT)
        self.assertNotIn(KEY, body.decode())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.upstream.assert_called_once()
        self.assertEqual(self.upstream.call_args.args[0], "/tokens")
        self.assertEqual(server.JOBS, {})
        self.assertFalse((self.root / "jobs").exists())

    def test_cookie_does_not_bypass_cross_site_missing_origin_or_wrong_host_checks(self):
        cookie = self.cookie()
        for options in ({"origin": "https://evil.example"}, {"origin": None}, {"host": "evil.example"}):
            status, _, _ = self.request(cookie=cookie, **options)
            self.assertEqual(status, 403)
        self.upstream.assert_not_called()

    def test_failure_response_is_safe_and_does_not_create_a_session_or_job(self):
        self.upstream.side_effect = reactor_api.ReactorError("Reactor rejected access to this model.")
        status, _, body = self.request(cookie=self.cookie())
        self.assertGreaterEqual(status, 400)
        self.assertIn("error", json.loads(body))
        self.assertNotIn(KEY, body.decode())
        self.assertNotIn(JWT, body.decode())
        self.assertEqual(server.JOBS, {})
        self.upstream.assert_called_once()


if __name__ == "__main__":
    unittest.main()
