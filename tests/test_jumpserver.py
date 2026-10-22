import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fl_dashboard.resources.jumpserver import (AUTH_ERROR, INVENTORY_ERROR, JumpServerError,
    JumpServerInventory, LOGIN_ERROR, MAX_ASSETS, NETWORK_ERROR, _request)


def asset(number=1, **extra):
    return {"id": f"00000000-0000-0000-0000-{number:012d}", "name": f"test-device-{number}",
            "address": "private-address-never-published", "platform": {"name": "Linux"},
            "protocols": [{"name": "ssh"}], "tags": [{"name": "gpu"}], **extra}


class JumpServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.rows = [asset()]
        self.status = 200
        self.account_status = 200
        self.login_status = 200
        self.login_body = {"token": "fixture-private-session-token"}
        self.failure = None
        self.before_accounts = None
        self.config = {"base_url": "https://jumpserver.example.test", "username": "fixture-user"}
        self.login_payload = {**self.config, "password": "fixture-private-password"}
        self.store = self.make()

    def make(self, requester=None, **kwargs):
        store = JumpServerInventory(self.root, encrypt=lambda value: b"encrypted:" + value[::-1],
                                    decrypt=lambda value: value.removeprefix(b"encrypted:")[::-1],
                                    requester=requester or self.requester, **kwargs)
        self.addCleanup(store.close)
        return store

    def requester(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.failure:
            raise self.failure
        path = urlsplit(url).path
        if path.endswith("authentication/auth/"):
            return self.login_status, self.login_body
        if path.endswith("/accounts/"):
            if self.before_accounts:
                self.before_accounts()
            return self.account_status, [{"username": "secure"}, {"username": "root"}]
        return self.status, {"count": len(self.rows), "next": None, "results": self.rows}

    def wait(self, store=None):
        store = store or self.store
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = store.snapshot()
            if not value["refreshing"]:
                return value
            time.sleep(.005)
        self.fail("Fixture inventory did not settle")

    def login(self):
        self.store.login(self.login_payload)
        return self.wait()

    def test_login_refresh_and_disk_never_expose_password_or_token(self):
        self.assertFalse(self.store.snapshot()["enabled"])
        self.assertEqual([], self.calls)
        value = self.login()
        self.assertTrue(value["authenticated"])
        self.assertEqual("jumpserver.example.test", value["ssh_host"])
        self.assertEqual(["secure", "root"], value["assets"][0]["accounts"])
        self.assertEqual(["gpu"], value["assets"][0]["tags"])
        self.assertEqual("Linux", value["assets"][0]["platform"])
        public = json.dumps(self.store.catalog())
        persisted = self.store.path.read_text(encoding="utf-8")
        for forbidden in ("fixture-private-password", "fixture-private-session-token", "private-address-never-published"):
            self.assertNotIn(forbidden, public)
            self.assertNotIn(forbidden, persisted)
        self.assertGreater(self.store.catalog()["generation"], 0)
        self.assertEqual({"username": "fixture-user", "password": "fixture-private-password"}, self.calls[0][2]["json_body"])
        self.assertNotIn("Authorization", self.calls[0][2]["headers"])
        self.assertEqual("Bearer fixture-private-session-token", self.calls[1][2]["headers"]["Authorization"])
        restored = self.make()
        self.assertFalse(restored.snapshot()["authenticated"])
        self.assertEqual(1, restored.snapshot()["asset_count"])
        restored.refresh()
        self.assertTrue(self.wait(restored)["authenticated"])

    def test_login_failure_and_mfa_are_fixed_and_redacted(self):
        self.login_status = 401
        self.login_body = {"detail": "sensitive upstream account contents"}
        with self.assertRaisesRegex(JumpServerError, LOGIN_ERROR):
            self.store.login(self.login_payload)
        self.assertNotIn("sensitive", json.dumps(self.store.snapshot()))
        self.login_status = 200
        self.login_body = {"mfa_required": True, "challenge": "secret-challenge"}
        with self.assertRaisesRegex(JumpServerError, "MFA/SSO"):
            self.store.login(self.login_payload)
        self.assertNotIn("secret-challenge", json.dumps(self.store.snapshot()))
        self.assertFalse(self.store.snapshot()["authenticated"])

    def test_network_errors_and_oversized_responses_are_redacted(self):
        self.failure = RuntimeError("secret password and raw endpoint")
        with self.assertRaisesRegex(JumpServerError, NETWORK_ERROR):
            self.store.login(self.login_payload)
        self.failure = None
        self.login_body = {"token": "x" * (2 * 1024 * 1024 + 1)}
        with self.assertRaisesRegex(JumpServerError, "超过上限"):
            self.store.login(self.login_payload)
        self.assertNotIn("secret password", json.dumps(self.store.snapshot()))

    def test_complete_paginated_inventory_and_accounts(self):
        def requester(method, url, **kwargs):
            if method == "POST":
                return 200, {"token": "token-fixture"}
            parts = urlsplit(url)
            offset = int(parse_qs(parts.query)["offset"][0])
            if parts.path.endswith("/accounts/"):
                return 200, {"count": 2, "next": "?limit=1&offset=1" if offset == 0 else None,
                             "results": [{"username": "secure" if offset == 0 else "root"}]}
            return 200, {"count": 2, "next": "?limit=1&offset=1" if offset == 0 else None,
                         "results": [asset(offset + 1)]}
        store = self.make(requester)
        store.login(self.login_payload)
        value = self.wait(store)
        self.assertEqual(2, value["asset_count"])
        self.assertTrue(value["authenticated"])
        self.assertEqual(["secure", "root"], value["assets"][1]["accounts"])

    def test_failed_or_incomplete_refresh_keeps_previous_complete_snapshot(self):
        self.login()
        previous = self.store.snapshot()["assets"]
        bad_pages = [
            {"count": 2, "next": None, "results": [asset(2)]},
            {"count": 2, "next": "https://other.example.test/steal?offset=1", "results": [asset(2)]},
            {"count": 2, "next": "?offset=0", "results": [asset(2)]},
            {"count": MAX_ASSETS + 1, "next": None, "results": []},
            {"count": 1, "next": None, "results": [asset(2, id="../../invalid-id")]},
            {"count": 2, "next": None, "results": [asset(2), asset(2)]},
        ]
        for page in bad_pages:
            with self.subTest(page=page):
                self.store._requester = lambda *args, page=page, **kwargs: (200, page)
                self.store.refresh()
                value = self.wait()
                self.assertFalse(value["authenticated"])
                self.assertTrue(value["error"])
                self.assertEqual(previous, value["assets"])

    def test_asset_dedup_conflicts_and_protocols(self):
        self.rows = [asset(), asset(), asset(2, protocols=[{"name": "rdp"}])]
        original = self.store._requester
        self.store._requester = lambda method, url, **kwargs: ((200, self.rows) if "/my/assets/" in url
                                                              else original(method, url, **kwargs))
        value = self.login()
        self.assertEqual(2, value["asset_count"])
        self.assertFalse(value["assets"][1]["ssh_supported"])
        self.assertEqual([], value["assets"][1]["accounts"])
        self.assertEqual(1, sum(urlsplit(c[1]).path.endswith("/accounts/") for c in self.calls))
        self.rows = [asset(), asset(name="conflicting identity")]
        self.store.refresh()
        self.assertFalse(self.wait()["authenticated"])
        self.assertEqual(2, self.store.snapshot()["asset_count"])

    def test_account_failure_prevents_partial_publication_and_expires_token(self):
        previous = self.login()["assets"]
        self.rows = [asset(2)]
        self.account_status = 403
        self.store.refresh()
        value = self.wait()
        self.assertEqual(AUTH_ERROR, value["error"])
        self.assertEqual(previous, value["assets"])
        self.assertFalse(value["authenticated"])
        self.assertIsNone(json.loads(self.store.path.read_text(encoding="utf-8"))["encrypted_token"])
        with self.assertRaisesRegex(JumpServerError, "先登录"):
            self.store.refresh()

    def test_configuration_and_logout_invalidate_inflight_results(self):
        entered, release = threading.Event(), threading.Event()
        self.before_accounts = lambda: (entered.set(), release.wait(3))
        self.addCleanup(release.set)
        self.store.login(self.login_payload)
        self.assertTrue(entered.wait(2))
        before = self.store.catalog()["generation"]
        self.store.configure({"username": "other-user"})
        release.set()
        value = self.wait()
        self.assertEqual(0, value["asset_count"])
        self.assertFalse(value["authenticated"])
        self.assertGreater(self.store.catalog()["generation"], before)
        self.assertIsNone(json.loads(self.store.path.read_text(encoding="utf-8"))["encrypted_token"])
        value = self.store.logout()
        self.assertFalse(value["enabled"])
        self.assertFalse(value["authenticated"])
        self.assertFalse(self.store.refresh()["refreshing"])

    def test_account_requests_are_bounded_and_refresh_deduplicates(self):
        self.rows = [asset(i) for i in range(1, 9)]
        lock, entered, release = threading.Lock(), threading.Event(), threading.Event()
        active, peak = [0], [0]
        def pause():
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                if active[0] == 4:
                    entered.set()
            release.wait(3)
            with lock:
                active[0] -= 1
        self.before_accounts = pause
        self.addCleanup(release.set)
        self.store.login(self.login_payload)
        self.assertTrue(entered.wait(2))
        self.store.refresh()
        self.store.refresh()
        self.assertEqual(1, sum("/my/assets/" in c[1] for c in self.calls))
        release.set()
        self.assertEqual(8, self.wait()["asset_count"])
        self.assertEqual(4, peak[0])

    def test_close_discards_inflight_inventory(self):
        entered, release = threading.Event(), threading.Event()
        self.before_accounts = lambda: (entered.set(), release.wait(3))
        self.addCleanup(release.set)
        self.store.login(self.login_payload)
        self.assertTrue(entered.wait(2))
        self.store.close()
        release.set()
        self.assertEqual(0, self.store.snapshot()["asset_count"])
        with self.assertRaisesRegex(JumpServerError, "停止"):
            self.store.refresh()

    def test_validation_rejects_unsafe_endpoints_and_secret_configuration(self):
        for url in ("http://outside.example.test", "https://user:password@example.test", "https://@example.test", "https://example.test?a=1", "file:///tmp/test", "https://example.test/#fragment"):
            with self.subTest(url=url), self.assertRaises(JumpServerError):
                self.store.configure({"base_url": url})
        for value in ({"password": "never-save"}, {"session_token": "never-save"}, {"username": "user#account"}, {"username": "x" * 101}, {"ssh_port": True}, {"ssh_host": "-oProxyCommand"}, {"identity_file": "missing-key"}):
            with self.subTest(value=value), self.assertRaises(JumpServerError):
                self.store.configure(value)
        self.assertEqual([], self.calls)
        self.store.configure({"base_url": "https://initial.example.test"})
        updated = self.store.configure({"base_url": "https://updated.example.test"})
        self.assertEqual("updated.example.test", updated["ssh_host"])
        self.assertEqual("http://127.0.0.1:19999", self.store.configure({"base_url": "http://127.0.0.1:19999"})["base_url"])

    def test_plaintext_token_is_never_a_fallback(self):
        with patch("fl_dashboard.resources.jumpserver.os", SimpleNamespace(name="posix")):
            store = JumpServerInventory(self.root, requester=self.requester)
        self.addCleanup(store.close)
        store.login(self.login_payload)
        self.assertTrue(self.wait(store)["authenticated"])
        self.assertIsNone(json.loads(store.path.read_text(encoding="utf-8"))["encrypted_token"])
        store._encrypt = lambda value: (_ for _ in ()).throw(RuntimeError("raw crypto detail"))
        with self.assertRaisesRegex(JumpServerError, "安全保存"):
            store.login(self.login_payload)
        self.assertNotIn("raw crypto detail", json.dumps(store.snapshot()))
        self.assertNotIn("fixture-private-session-token", store.path.read_text(encoding="utf-8"))

    def test_default_requester_does_not_follow_redirects_and_bounds_output(self):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/sensitive")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"x" * 128)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:" + str(server.server_port)
        status, body = _request("GET", url + "/redirect", headers={}, json_body=None, timeout=2, max_bytes=64)
        self.assertEqual(302, status)
        self.assertEqual(["/redirect"], hits)
        with self.assertRaisesRegex(JumpServerError, "超过上限"):
            _request("GET", url + "/large", headers={}, json_body=None, timeout=2, max_bytes=64)


if __name__ == "__main__":
    unittest.main()
