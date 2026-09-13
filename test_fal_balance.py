"""Read-only billing protocol and browser-scoped asynchronous caching, all offline."""
import io
import json
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import fal_credentials as credentials

KEY_A = "offline-browser-a-never-valid"
KEY_B = "offline-browser-b-never-valid"
REAL_SCHEDULE = credentials._schedule_balance
REAL_THREAD = threading.Thread


class BalanceCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.
        self.pending = []
        replacements = [patch.object(credentials, "STORE", {}),
                        patch.object(credentials, "BALANCES", {}),
                        patch.object(credentials, "BALANCE_INFLIGHT", {}),
                        patch.object(credentials, "verify_key", return_value=True),
                        patch.object(credentials.threading, "Timer"),
                        patch.object(credentials.time, "time", side_effect=lambda: self.now),
                        patch.object(credentials, "_schedule_balance", side_effect=lambda *args: self.pending.append(args)),
                        patch.object(credentials, "build_opener", side_effect=AssertionError("No real network in cache tests"))]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)

    def connect(self, key=KEY_A, old_cookie=None):
        token, credential = credentials.set_key(old_cookie, key)
        return token, credential, credentials.cookie_header(token)

    def finish(self, result=None, operation=None):
        token, credential, request_tag = self.pending.pop(0)
        with patch.object(credentials, "_read_balance", side_effect=operation,
                          return_value=result or {"status": "ready", "amount": 12.5, "currency": "USD"}) as provider:
            credentials._balance_worker(token, credential, request_tag)
        return provider

    def test_disconnected_status_never_reads_owner_environment_or_schedules_balance(self):
        with patch.dict(os.environ, {"FAL_KEY": "owner-must-not-be-read"}):
            for cookie in (None, "", "wmc_fal_session=wrong"):
                self.assertEqual(credentials.status(cookie), {"configured": False, "verified": False})
        self.assertEqual(self.pending, [])
        self.assertEqual(credentials.BALANCES, {})

    def test_loading_is_immediate_one_inflight_and_completed_zero_balance_is_cached(self):
        token, credential, cookie = self.connect()
        self.assertEqual(credentials.status(cookie)["balance"], {"status": "loading"})
        for _ in range(20):
            self.assertEqual(credentials.status(cookie)["balance"]["status"], "loading")
        self.assertEqual(len(self.pending), 1)
        provider = self.finish({"status": "ready", "amount": 0, "currency": "USD"})
        provider.assert_called_once_with(KEY_A)
        shown = credentials.status(cookie)
        self.assertEqual(shown["balance"], {"status": "ready", "amount": 0, "currency": "USD", "checked_at": self.now})
        shown["balance"]["amount"] = 999
        self.assertEqual(credentials.status(cookie)["balance"]["amount"], 0)
        self.assertEqual(self.pending, [])
        self.assertNotIn(KEY_A, json.dumps(shown))
        self.assertNotIn(KEY_A, repr(credential))

    def test_sixty_second_refresh_is_one_new_read_and_all_terminal_states_are_cached(self):
        for state in ("ready", "forbidden", "unavailable"):
            _, _, cookie = self.connect()
            credentials.status(cookie)
            self.finish({"status": state})
            self.now += 59.9
            self.assertEqual(credentials.status(cookie)["balance"]["status"], state)
            self.assertEqual(self.pending, [])
            self.now += .2
            self.assertEqual(credentials.status(cookie)["balance"]["status"], "loading")
            credentials.status(cookie)
            self.assertEqual(len(self.pending), 1)
            self.finish({"status": state})
            credentials.clear(cookie)

    def test_two_browsers_have_separate_keys_amounts_and_refreshes(self):
        first, _, a = self.connect(KEY_A)
        second, _, b = self.connect(KEY_B)
        credentials.status(a)
        credentials.status(b)
        self.finish({"status": "ready", "amount": -2.5, "currency": "GBP"}).assert_called_once_with(KEY_A)
        self.finish({"status": "ready", "amount": 100, "currency": "USD"}).assert_called_once_with(KEY_B)
        self.assertEqual(credentials.status(a)["balance"]["amount"], -2.5)
        self.assertEqual(credentials.status(b)["balance"]["amount"], 100)
        credentials.clear(a)
        self.assertNotIn(first, credentials.BALANCES)
        self.assertIn(second, credentials.BALANCES)
        self.assertEqual(credentials.status(a), {"configured": False, "verified": False})

    def test_clear_replacement_and_expiry_reject_already_scheduled_work_before_fetch(self):
        for change in ("clear", "replace", "expire"):
            with self.subTest(change=change):
                token, credential, cookie = self.connect()
                credentials.status(cookie)
                if change == "clear":
                    credentials.clear(cookie)
                elif change == "replace":
                    self.connect(KEY_B, cookie)
                else:
                    self.now = credential.expires_at
                    credentials.status(cookie)
                self.finish().assert_not_called()
                self.assertNotIn(token, credentials.BALANCES)
                self.assertNotIn(token, credentials.BALANCE_INFLIGHT)

    def test_old_response_cannot_restore_clear_replace_or_expired_credentials(self):
        for change in ("clear", "replace", "expire"):
            token, credential, cookie = self.connect()
            credentials.status(cookie)
            def in_flight(_key):
                if change == "clear":
                    credentials.clear(cookie)
                elif change == "replace":
                    _, _, replacement_cookie = self.connect(KEY_B, cookie)
                    credentials.status(replacement_cookie)
                else:
                    self.now = credential.expires_at + 1
                return {"status": "ready", "amount": 55, "currency": "USD"}
            self.finish(operation=in_flight)
            self.assertNotIn(token, credentials.BALANCES)
            self.assertNotIn(token, credentials.BALANCE_INFLIGHT)
            self.assertFalse(credentials.status(cookie)["configured"])
            while self.pending:
                self.finish()

    def test_expiry_timer_evicts_ready_balance_and_replacement_drops_old_cache(self):
        token, credential, cookie = self.connect()
        credentials.status(cookie)
        self.finish()
        credentials._expire(token, credential.fingerprint)
        self.assertFalse(credentials.status(cookie)["configured"])
        self.assertEqual(credentials.BALANCES, {})
        _, _, cookie = self.connect()
        credentials.status(cookie)
        self.finish()
        self.connect(KEY_B, cookie)
        self.assertEqual(credentials.BALANCES, {})

    def test_worker_and_scheduler_failures_are_sanitized_and_release_inflight(self):
        token, _, cookie = self.connect()
        credentials.status(cookie)
        self.finish(operation=RuntimeError(KEY_A))
        self.assertEqual(credentials.status(cookie)["balance"]["status"], "unavailable")
        self.assertNotIn(token, credentials.BALANCE_INFLIGHT)
        self.now += 61
        with patch.object(credentials, "_schedule_balance", side_effect=RuntimeError(KEY_A)):
            result = credentials.status(cookie)
        self.assertEqual(result["balance"]["status"], "unavailable")
        self.assertNotIn(KEY_A, json.dumps(result))
        self.assertNotIn(token, credentials.BALANCE_INFLIGHT)

    def test_cache_count_is_bounded_by_credential_store_and_replacement_reuses_capacity(self):
        with patch.object(credentials, "MAX_CREDENTIALS", 2):
            _, _, cookie = self.connect()
            credentials.status(cookie)
            _, _, other = self.connect(KEY_B)
            credentials.status(other)
            with self.assertRaises(credentials.CredentialError):
                self.connect("offline-browser-c-never-valid")
            _, _, replaced = self.connect(KEY_B, cookie)
            credentials.status(replaced)
            self.assertEqual(len(credentials.STORE), 2)
            self.assertLessEqual(len(credentials.BALANCES), 2)
            self.assertLessEqual(len(credentials.BALANCE_INFLIGHT), 2)

    def test_real_daemon_worker_does_not_block_status_or_hold_store_lock(self):
        _, _, cookie = self.connect()
        entered, release = threading.Event(), threading.Event()
        workers = []
        def provider(_key):
            entered.set()
            release.wait(2)
            return {"status": "ready", "amount": 5, "currency": "USD"}
        def thread_factory(*args, **kwargs):
            worker = REAL_THREAD(*args, **kwargs)
            workers.append(worker)
            return worker
        with patch.object(credentials, "_schedule_balance", REAL_SCHEDULE), \
                patch.object(credentials, "_read_balance", side_effect=provider) as fetch, \
                patch.object(credentials.threading, "Thread", side_effect=thread_factory):
            try:
                started = time.perf_counter()
                self.assertEqual(credentials.status(cookie)["balance"]["status"], "loading")
                self.assertLess(time.perf_counter() - started, .5)
                self.assertTrue(entered.wait(1))
                for _ in range(10):
                    self.assertEqual(credentials.status(cookie)["balance"]["status"], "loading")
                credentials.clear(cookie)
                self.assertFalse(credentials.status(cookie)["configured"])
            finally:
                release.set()
                for worker in workers:
                    worker.join(2)
            fetch.assert_called_once_with(KEY_A)
        self.assertTrue(workers[0].daemon)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(credentials.BALANCES, {})


class BalanceProtocolTests(unittest.TestCase):
    def response(self, raw=None, failure=None):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = raw
        opener = Mock()
        opener.open.return_value = response
        if failure:
            opener.open.side_effect = failure
        return opener, response

    def read(self, raw=None, failure=None):
        opener, response = self.response(raw, failure)
        with patch.object(credentials, "build_opener", return_value=opener) as constructor:
            result = credentials._read_balance(KEY_A)
        return result, opener, response, constructor

    def test_exact_read_only_endpoint_timeout_size_limit_and_redirect_block(self):
        result, opener, response, constructor = self.read(b'{"credits":{"current_balance":24.5,"currency":"USD"}}')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.fal.ai/v1/account/billing?expand=credits")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Key " + KEY_A)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 8)
        response.read.assert_called_once_with(65537)
        self.assertEqual(result, {"status": "ready", "amount": 24.5, "currency": "USD"})
        self.assertIsInstance(constructor.call_args.args[0], credentials.NoRedirect)
        self.assertIsNone(constructor.call_args.args[0].redirect_request(None, None, 302, "", {}, "https://other.test"))

    def test_zero_negative_and_lowercase_currency_are_valid(self):
        for value in (0, -12.25, 100):
            result, *_ = self.read(json.dumps({"credits": {"current_balance": value, "currency": "gbp"}}).encode())
            self.assertEqual(result, {"status": "ready", "amount": value, "currency": "GBP"})

    def test_invalid_schema_amount_currency_and_oversized_responses_are_unavailable(self):
        invalid = [b"bad JSON " + KEY_A.encode(), b"{}", b"[]", b"null", b"x" * 65537]
        for amount in (None, True, "24.5", float("nan"), float("inf"), -float("inf"), 10 ** 1000):
            invalid.append(json.dumps({"credits": {"current_balance": amount, "currency": "USD"}}).encode())
        for currency in (None, "US", "USDD", "$", "U1D", KEY_A):
            invalid.append(json.dumps({"credits": {"current_balance": 4, "currency": currency}}).encode())
        for raw in invalid:
            result, *_ = self.read(raw)
            self.assertEqual(result, {"status": "unavailable"})
            self.assertNotIn(KEY_A, json.dumps(result))

    def test_permission_and_network_errors_never_return_raw_provider_text(self):
        for code in (301, 307, 401, 403, 429, 500):
            failure = HTTPError("https://api.fal.ai/v1/account/billing", code, KEY_A, {}, io.BytesIO(KEY_A.encode()))
            result, opener, *_ = self.read(failure=failure)
            self.assertEqual(result, {"status": "forbidden" if code == 403 else "unavailable"})
            self.assertNotIn(KEY_A, json.dumps(result))
            opener.open.assert_called_once()
        for failure in (URLError(KEY_A), TimeoutError(KEY_A), OSError(KEY_A)):
            result, *_ = self.read(failure=failure)
            self.assertEqual(result, {"status": "unavailable"})


if __name__ == "__main__":
    unittest.main()
