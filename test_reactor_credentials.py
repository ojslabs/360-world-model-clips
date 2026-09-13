"""Offline browser-key isolation, verification and process handoff checks."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import media
import reactor_api as api
import reactor_credentials as credentials
import reactor_live
import reactor_remix as remix

KEY_A = "rk_browser_A_private:key"
KEY_B = "rk_browser_B_private:key"
JWT = "private-test-jwt"


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        for replacement in (patch.object(credentials, "STORE", {}),
                            patch.object(credentials, "verify_key", return_value=True),
                            patch.object(credentials.threading, "Timer"),
                            patch.object(credentials.time, "time", side_effect=lambda: self.now),
                            patch.object(api, "api_key", side_effect=AssertionError("No owner fallback"))):
            replacement.start()
            self.addCleanup(replacement.stop)

    def connect(self, key=KEY_A, cookie=""):
        token, credential = credentials.set_key(cookie, key)
        return credentials.cookie_header(token), credential

    def test_status_never_reflects_key_token_or_owner_and_clients_are_isolated(self):
        first, captured = self.connect()
        second, other = self.connect(KEY_B)
        self.assertEqual(credentials.require(first).key, KEY_A)
        self.assertEqual(credentials.require(second).key, KEY_B)
        self.assertNotEqual(captured.fingerprint, other.fingerprint)
        self.assertEqual(captured.fingerprint, api.credential_fingerprint(KEY_A))
        state = credentials.status(first)
        self.assertTrue(state["configured"])
        self.assertTrue(state["verified"])
        self.assertEqual(state["expires_at"], 1000 + 8 * 60 * 60)
        for secret in (KEY_A, captured.fingerprint, first.split(";", 1)[0].split("=", 1)[1]):
            self.assertNotIn(secret, json.dumps(state))
        self.assertNotIn(KEY_A, repr(captured))

    def test_clear_replacement_and_expiry_revoke_cookie_but_not_worker_snapshot(self):
        first, captured = self.connect()
        second, _ = self.connect(KEY_B, first)
        self.assertFalse(credentials.status(first)["configured"])
        self.assertEqual(captured.key, KEY_A)
        credentials.clear(second)
        with self.assertRaises(credentials.CredentialError):
            credentials.require(second)
        third, _ = self.connect()
        self.now += credentials.TTL_SECONDS
        self.assertFalse(credentials.status(third)["configured"])
        self.assertEqual(credentials.STORE, {})

    def test_expiry_timer_and_store_limit(self):
        with patch.object(credentials, "MAX_CREDENTIALS", 1):
            first, captured = self.connect()
            with self.assertRaisesRegex(credentials.CredentialError, "too many"):
                self.connect(KEY_B)
            second, _ = self.connect(KEY_B, first)
            self.assertEqual(len(credentials.STORE), 1)
            credentials._expire(credentials._token(first), captured.fingerprint)
            self.assertTrue(credentials.status(second)["configured"])
            credentials._expire(credentials._token(second), credentials.require(second).fingerprint)
            self.assertEqual(credentials.STORE, {})

    def test_malformed_keys_and_cookies_do_not_trigger_verification(self):
        for key in (None, "", "wrong_prefix", "rk_1234", "rk_" + "x" * 510, "rk_private key", "rk_private\nkey", "rk_unicode_é"):
            with self.subTest(key_type=type(key).__name__), self.assertRaises(credentials.CredentialError):
                credentials.set_key("", key)
        credentials.verify_key.assert_not_called()
        for cookie in (None, "wmc_reactor_session=bad", "wmc_fal_session=" + "x" * 43):
            self.assertFalse(credentials.status(cookie)["configured"])

    def test_cookie_is_opaque_httponly_strict_and_clearable(self):
        cookie, _ = self.connect()
        self.assertIn("HttpOnly; SameSite=Strict; Max-Age=28800", cookie)
        self.assertNotIn(KEY_A, cookie)
        self.assertIn("; Secure", credentials.cookie_header(credentials._token(cookie), secure=True))
        self.assertIn("Max-Age=0", credentials.cookie_header(clear=True))


class ProviderBoundaryTests(unittest.TestCase):
    def test_verify_checks_only_one_bounded_x2_token_and_discards_it(self):
        with patch.object(api, "api_key", side_effect=AssertionError("No owner key")), \
             patch.object(api, "request", return_value={"jwt": JWT, "debug": KEY_A}) as request:
            self.assertIs(credentials.verify_key(KEY_A), True)
        request.assert_called_once()
        path, payload = request.call_args.args
        self.assertEqual(path, "/tokens")
        self.assertEqual(request.call_args.kwargs, {"key": KEY_A})
        self.assertEqual(payload, {"expires_after": 60, "authorization_details": [{"type": "session",
            "resources": {"models": {"match": ["xmax/x2"]}},
            "constraints": {"max_sessions": 1, "max_session_duration_seconds": 60}}]})

    def test_explicit_none_and_invalid_key_cannot_use_owner_environment(self):
        with patch.object(api, "api_key", return_value=KEY_B) as owner, \
             patch.object(api, "request") as request:
            for key in (None, "", "invalid", "rk_key with spaces"):
                with self.assertRaises(api.ReactorError):
                    api.mint_token(credential_key=key)
            with self.assertRaises(ValueError):
                reactor_live.token(credential_key=None)
        owner.assert_not_called()
        request.assert_not_called()

    def test_live_token_uses_only_snapshot_and_never_reflects_upstream_key(self):
        with patch.object(api, "api_key", side_effect=AssertionError("No owner key")), \
             patch.object(api, "request", return_value={"jwt": JWT, "debug": KEY_A}) as request:
            result = reactor_live.token(credential_key=KEY_A)
        self.assertEqual(request.call_args.kwargs["key"], KEY_A)
        self.assertEqual(result["jwt"], JWT)
        self.assertNotIn(KEY_A, json.dumps(result))
        with patch.object(api, "request", side_effect=api.ReactorError("Upstream rejected " + KEY_A)):
            for action in (lambda: reactor_live.token(credential_key=KEY_A), lambda: credentials.verify_key(KEY_A)):
                with self.assertRaises(ValueError) as raised:
                    action()
                self.assertNotIn(KEY_A, str(raised.exception))

    def test_read_is_size_bounded_and_redirects_errors_are_sanitized(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(api, "build_opener", return_value=opener):
            response.read.return_value = b"x" * (api.MAX_RESPONSE_BYTES + 1)
            with self.assertRaisesRegex(api.ReactorError, "oversized"):
                api.mint_token(credential_key=KEY_A)
            response.read.assert_called_once_with(api.MAX_RESPONSE_BYTES + 1)
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
            for code in (302, 401, 403, 429, 500):
                opener.open.side_effect = HTTPError("https://api.reactor.inc/tokens", code, KEY_A, {}, io.BytesIO(KEY_A.encode()))
                with self.assertRaises(api.ReactorError) as raised:
                    api.mint_token(credential_key=KEY_A)
                self.assertNotIn(KEY_A, str(raised.exception))
        self.assertIsNone(api.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.test"))


class RemixHandoffTests(unittest.TestCase):
    def test_key_crosses_private_pipe_only_and_worker_uses_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / "source.mp4", root / "remix.mp4"
            source.write_bytes(b"unchanged source")
            details = {"width": 160, "height": 90, "duration": 5, "fps": 24}
            calls = []

            async def session(request, receipt, save, *, credential_key):
                calls.append(credential_key)

            def finish(request, receipt, save):
                receipt.update(status="complete", path=request["output"])
                save("complete")

            class Pipe(io.BytesIO):
                def close(self):
                    self.sent = self.getvalue()
                    super().close()

            class Worker:
                returncode = None
                def __init__(self, args, **kwargs):
                    self.args, self.kwargs, self.stdin = args, kwargs, Pipe()
                    workers.append(self)
                def poll(self):
                    if self.returncode is None:
                        self.returncode = remix._worker(self.args[3], credential_key=self.stdin.sent.decode())
                    return self.returncode

            workers = []
            with patch.object(api, "api_key", side_effect=AssertionError("No owner key")), \
                 patch.object(media, "probe", return_value=details), patch.object(remix, "_video_info", return_value=details), \
                 patch.object(remix, "_session", side_effect=session), patch.object(remix, "_finish", side_effect=finish), \
                 patch.object(remix.subprocess, "Popen", side_effect=Worker):
                result = remix.generate(source, output, "Night", credential_key=KEY_A,
                                        credential_owner=api.credential_fingerprint(KEY_A))
            self.assertEqual(calls, [KEY_A])
            self.assertEqual(len(workers), 1)
            self.assertNotIn(KEY_A, repr(workers[0].args))
            self.assertNotIn("env", workers[0].kwargs)
            self.assertEqual(result["credential_source"], "browser")
            self.assertEqual(result["credential_owner"], api.credential_fingerprint(KEY_A))
            for path in root.glob("*.json"):
                self.assertNotIn(KEY_A, path.read_text())
            self.assertEqual(source.read_bytes(), b"unchanged source")

    def test_missing_or_wrong_worker_key_cannot_start_or_fall_back(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = {"output": str(root / "out.mp4"), "source": "source", "source_media": {"width": 160, "height": 90},
                       "source_sha256": "hash", "prompt": "Night", "created_at": 1000, "expected_frames": 120,
                       "credential_source": "browser", "credential_owner": api.credential_fingerprint(KEY_A)}
            path = root / "request.json"
            media.write_json(path, request)
            with patch.object(api, "api_key", side_effect=AssertionError("No owner key")), \
                 patch.object(remix, "_session") as session:
                for key in (api.ENV_CREDENTIAL, None, KEY_B):
                    self.assertEqual(remix._worker(path, credential_key=key), 1)
                session.assert_not_called()

    def test_diagnostic_redacts_exact_browser_key_including_punctuation(self):
        self.assertNotIn(KEY_A, remix._safe_error(RuntimeError("Failure " + KEY_A), KEY_A))


if __name__ == "__main__":
    unittest.main()
