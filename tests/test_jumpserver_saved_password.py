
"""Saved JumpServer credentials: synthetic accounts and mocked HTTP only."""
import base64
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fl_dashboard.resources.jumpserver import (
    AUTO_LOGIN_COOLDOWN, AUTO_LOGIN_PAUSED, AUTO_LOGIN_RETRY,
    JumpServerError, JumpServerInventory, NETWORK_ERROR,
)
from test_jumpserver import asset, detail, is_detail


class SavedPasswordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = 1000.0
        self.calls = []
        self.login_count = 0
        self.login_status = 200
        self.fail_login = False
        self.expired_tokens = set()
        self.deny_details = False
        self.login_gate = None
        self.payload = {"base_url": "https://saved.example.test", "username": "fixture-user",
                        "password": "synthetic-saved-password", "remember_password": True}
        self.store = self.make()

    def make(self, directory=None, **kwargs):
        store = JumpServerInventory(
            directory or self.root, encrypt=lambda value: b"fixture:" + value[::-1],
            decrypt=lambda value: value.removeprefix(b"fixture:")[::-1],
            requester=self.requester, monotonic=lambda: self.clock, **kwargs)
        self.addCleanup(store.close)
        return store

    def requester(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = urlsplit(url).path
        if path.endswith("authentication/auth/"):
            self.login_count += 1
            if self.login_gate:
                self.login_gate()
            if self.fail_login:
                raise RuntimeError("PRIVATE_RAW_NETWORK_DIAGNOSTIC")
            return self.login_status, {"token": "fixture-token-" + str(self.login_count),
                                       "detail": "PRIVATE_UPSTREAM_CONTENT"}
        token = kwargs["headers"].get("Authorization", "").removeprefix("Bearer ")
        if token in self.expired_tokens:
            return 401, {"detail": "PRIVATE_UPSTREAM_CONTENT"}
        if is_detail(path):
            return (403, None) if self.deny_details else (200, detail(asset()))
        return 200, {"count": 1, "next": None, "results": [asset()]}

    def wait(self, store=None):
        store = store or self.store
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = store.snapshot()
            if not value["refreshing"]:
                return value
            time.sleep(.005)
        self.fail("Mock inventory did not settle")

    def login(self, store=None, **extra):
        store = store or self.store
        store.login({**self.payload, **extra})
        return self.wait(store)

    def disk(self):
        return json.loads(self.store.path.read_text(encoding="utf-8"))

    def expire(self):
        self.expired_tokens.add("fixture-token-" + str(self.login_count))

    def test_encrypted_identity_bound_password_is_opt_in_and_never_public(self):
        value = self.login()
        self.assertTrue(value["password_saved"])
        self.assertTrue(value["can_remember_password"])
        public = json.dumps(self.store.catalog())
        persisted = self.store.path.read_text(encoding="utf-8")
        for forbidden in ("synthetic-saved-password", "fixture-token-1", "PRIVATE_UPSTREAM_CONTENT"):
            self.assertNotIn(forbidden, public)
            self.assertNotIn(forbidden, persisted)
        self.assertNotIn("encrypted_credential", public)
        self.assertNotIn("auto_login_blocked", public)
        encrypted = self.disk()["encrypted_credential"]
        sealed = json.loads(base64.b64decode(encrypted).removeprefix(b"fixture:")[::-1])
        self.assertEqual({key: self.payload[key] for key in ("base_url", "username", "password")}, sealed)
        self.login(remember_password=False)
        self.assertFalse(self.store.snapshot()["password_saved"])
        self.assertIsNone(self.disk()["encrypted_credential"])

    def test_default_login_does_not_save_password_and_bool_is_strict(self):
        payload = {key: value for key, value in self.payload.items() if key != "remember_password"}
        self.store.login(payload)
        self.assertFalse(self.wait()["password_saved"])
        self.assertIsNone(self.disk()["encrypted_credential"])
        count = len(self.calls)
        for value in ("true", 1, None, []):
            with self.subTest(value=value), self.assertRaises(JumpServerError):
                self.store.login({**self.payload, "remember_password": value})
        self.assertEqual(count, len(self.calls))

    def test_constructor_stays_offline_and_resume_refreshes_saved_token_once(self):
        self.login()
        self.store.close()
        count = len(self.calls)
        restored = self.make()
        self.assertEqual(count, len(self.calls))
        self.assertFalse(restored.snapshot()["authenticated"])
        restored.resume()
        self.assertTrue(self.wait(restored)["authenticated"])
        self.assertEqual(1, self.login_count)

    def test_missing_token_restores_from_encrypted_password_after_restart(self):
        self.login()
        self.store.close()
        record = self.disk()
        record["encrypted_token"] = None
        self.store.path.write_text(json.dumps(record), encoding="utf-8")
        count = len(self.calls)
        restored = self.make()
        self.assertEqual(count, len(self.calls))
        self.assertTrue(restored.snapshot()["can_refresh"])
        restored.resume()
        value = self.wait(restored)
        self.assertTrue(value["authenticated"])
        self.assertTrue(value["password_saved"])
        self.assertEqual(2, self.login_count)
        self.assertEqual(1, value["asset_count"])

    def test_expired_token_reauthenticates_once_then_refetches_permissions(self):
        previous = self.login()["assets"]
        self.expire()
        self.store.refresh()
        result = self.wait()
        self.assertTrue(result["authenticated"])
        self.assertEqual(previous, result["assets"])
        self.assertEqual(2, self.login_count)
        latest_detail = [call for call in self.calls if is_detail(urlsplit(call[1]).path)][-1]
        self.assertEqual("Bearer fixture-token-2", latest_detail[2]["headers"]["Authorization"])

    def test_permission_denial_after_reauthentication_pauses_without_looping(self):
        previous = self.login()["assets"]
        self.deny_details = True
        self.store.refresh()
        result = self.wait()
        self.assertEqual(2, self.login_count)
        self.assertTrue(result["password_saved"])
        self.assertFalse(result["can_refresh"])
        self.assertEqual(AUTO_LOGIN_PAUSED, result["error"])
        self.assertEqual(previous, result["assets"])
        self.store.resume()
        with self.assertRaisesRegex(JumpServerError, AUTO_LOGIN_PAUSED):
            self.store.refresh()
        self.assertEqual(2, self.login_count)

    def test_rejected_password_stays_paused_across_restart_until_explicit_login(self):
        self.login()
        self.expire()
        self.login_status = 401
        self.store.refresh()
        result = self.wait()
        self.assertEqual(AUTO_LOGIN_PAUSED, result["error"])
        self.assertFalse(result["can_refresh"])
        self.assertTrue(result["password_saved"])
        self.assertTrue(self.disk()["auto_login_blocked"])
        self.assertNotIn("PRIVATE_UPSTREAM_CONTENT", json.dumps(result))
        for _ in range(3):
            self.store.resume()
            with self.assertRaisesRegex(JumpServerError, AUTO_LOGIN_PAUSED):
                self.store.refresh()
        self.store.close()
        restored = self.make()
        restored.resume()
        self.assertEqual(2, self.login_count)
        self.login_status = 200
        self.assertTrue(self.login(restored)["authenticated"])
        self.assertFalse(self.disk()["auto_login_blocked"])
        self.assertEqual(3, self.login_count)

    def test_transient_failure_preserves_password_and_throttles_login_retries(self):
        self.login()
        self.expire()
        self.fail_login = True
        self.store.refresh()
        result = self.wait()
        self.assertTrue(result["password_saved"])
        self.assertTrue(result["can_refresh"])
        self.assertFalse(result["authenticated"])
        self.assertEqual(NETWORK_ERROR, result["error"])
        self.assertNotIn("PRIVATE_RAW_NETWORK_DIAGNOSTIC", json.dumps(result))
        self.assertIsNone(self.disk()["encrypted_token"])
        for _ in range(3):
            self.store.resume()
            with self.assertRaisesRegex(JumpServerError, AUTO_LOGIN_RETRY):
                self.store.refresh()
        self.assertEqual(2, self.login_count)
        self.clock += AUTO_LOGIN_COOLDOWN + 1
        self.fail_login = False
        self.store.resume()
        self.assertTrue(self.wait()["authenticated"])
        self.assertEqual(3, self.login_count)

    def test_logout_and_identity_change_remove_saved_password_from_disk(self):
        for change in ({"username": "different-user"}, {"base_url": "https://other.example.test"}, None):
            with self.subTest(change=change):
                self.login()
                result = self.store.configure(change) if change else self.store.logout()
                self.assertFalse(result["password_saved"])
                self.assertFalse(result["can_refresh"])
                self.assertIsNone(self.disk()["encrypted_credential"])
                self.assertIsNone(self.disk()["encrypted_token"])
                self.assertFalse(self.disk()["auto_login_blocked"])
                self.assertFalse(self.make().snapshot()["password_saved"])

    def test_disabled_source_never_auto_logs_in_but_retains_opt_in_for_reenable(self):
        self.login()
        self.expire()
        self.store.configure({"enabled": False})
        self.store.close()
        restored = self.make()
        count = len(self.calls)
        restored.resume()
        restored.refresh()
        self.assertEqual(count, len(self.calls))
        self.assertTrue(restored.snapshot()["password_saved"])
        restored.configure({"enabled": True})
        restored.resume()
        self.assertTrue(self.wait(restored)["authenticated"])
        self.assertEqual(2, self.login_count)

    def test_logout_during_automatic_login_cannot_resurrect_credentials(self):
        self.login()
        self.expire()
        entered, release = threading.Event(), threading.Event()
        self.login_gate = lambda: (entered.set(), release.wait(3))
        self.addCleanup(release.set)
        self.store.refresh()
        self.assertTrue(entered.wait(2))
        self.store.logout()
        release.set()
        self.store._worker.submit(lambda: None).result(timeout=4)
        result = self.store.snapshot()
        self.assertFalse(result["enabled"])
        self.assertFalse(result["password_saved"])
        self.assertFalse(result["authenticated"])
        self.assertIsNone(self.disk()["encrypted_credential"])
        self.assertIsNone(self.disk()["encrypted_token"])

    def test_concurrent_refreshes_share_one_automatic_login(self):
        self.login()
        self.expire()
        entered, release = threading.Event(), threading.Event()
        self.login_gate = lambda: (entered.set(), release.wait(3))
        self.addCleanup(release.set)
        self.store.refresh()
        self.assertTrue(entered.wait(2))
        for _ in range(5):
            self.store.refresh()
            self.store.resume()
        release.set()
        self.assertTrue(self.wait()["authenticated"])
        self.assertEqual(2, self.login_count)

    def test_encrypted_password_cannot_be_reused_for_another_identity(self):
        self.login()
        self.store.close()
        original = self.disk()
        for field, value in (("username", "different-user"), ("base_url", "https://other.example.test")):
            with self.subTest(field=field):
                record = json.loads(json.dumps(original))
                record["settings"][field] = value
                record["encrypted_token"] = None
                self.store.path.write_text(json.dumps(record), encoding="utf-8")
                count = len(self.calls)
                restored = self.make()
                self.assertFalse(restored.snapshot()["password_saved"])
                restored.resume()
                self.assertEqual(count, len(self.calls))

    def test_no_encryption_support_never_falls_back_to_plaintext(self):
        with patch("fl_dashboard.resources.jumpserver.os", SimpleNamespace(name="posix")):
            store = JumpServerInventory(self.root / "unsupported", requester=self.requester)
        self.addCleanup(store.close)
        self.assertFalse(store.snapshot()["can_remember_password"])
        with self.assertRaisesRegex(JumpServerError, "不支持安全保存"):
            store.login(self.payload)
        self.assertEqual([], self.calls)
        self.assertFalse(store.snapshot()["password_saved"])
        store.login({**self.payload, "remember_password": False})
        self.assertTrue(self.wait(store)["authenticated"])
        self.assertNotIn(self.payload["password"], store.path.read_text(encoding="utf-8"))
        self.assertIsNone(json.loads(store.path.read_text(encoding="utf-8"))["encrypted_credential"])

    def test_failed_login_or_encryption_never_persists_password(self):
        self.login_status = 401
        with self.assertRaises(JumpServerError):
            self.store.login(self.payload)
        self.assertFalse(self.store.snapshot()["password_saved"])
        self.assertIsNone(self.disk()["encrypted_credential"])
        self.login_status = 200
        self.store._encrypt = lambda data: (_ for _ in ()).throw(RuntimeError("PRIVATE_CRYPTO_DETAIL"))
        with self.assertRaisesRegex(JumpServerError, "安全保存"):
            self.store.login(self.payload)
        self.assertFalse(self.store.snapshot()["password_saved"])
        self.assertIsNone(self.disk()["encrypted_credential"])
        self.assertNotIn("PRIVATE_CRYPTO_DETAIL", json.dumps(self.store.snapshot()))
        self.assertNotIn(self.payload["password"], self.store.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
