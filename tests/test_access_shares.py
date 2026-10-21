"""Capability grants must fail closed and revoke across service instances."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fl_dashboard.services.access_shares import AccessShareStore, validate_public_url


NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class AccessShareStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "access-shares.json"
        self.store = AccessShareStore(self.path, "https://dashboard.example.test/tools/")
        self.clock = patch("fl_dashboard.services.access_shares._utc_now", return_value=NOW)
        self.clock_mock = self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_creation_returns_token_once_and_persists_only_hash(self):
        created = self.store.create("  CI maintainers  ", 24)
        self.assertEqual(created["label"], "CI maintainers")
        self.assertEqual(created["expires_at"], "2030-01-03T03:04:05Z")
        self.assertEqual(len(created["token"]), 43)
        self.assertEqual(created["url"], "https://dashboard.example.test/tools/access#token=" + created["token"])
        self.assertTrue(created["active"])
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn(created["token"], persisted)
        self.assertNotIn('"token":', persisted)
        listed = self.store.list()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token", listed[0])
        self.assertNotIn("token_hash", listed[0])
        self.assertNotIn("url", listed[0])
        self.assertEqual(self.store.validate(created["token"]), listed[0])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_store_is_not_created_until_grant_is_issued(self):
        self.assertEqual(self.store.list(), [])
        self.assertIsNone(self.store.validate("x" * 43))
        self.assertFalse(self.path.exists())

    def test_revocation_is_immediate_in_another_instance_and_idempotent(self):
        reader = AccessShareStore(self.path)
        created = self.store.create()
        self.assertIsNotNone(reader.validate(created["token"]))
        revoked = self.store.revoke(created["id"])
        self.assertFalse(revoked["active"])
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["revoked_at"], "2030-01-02T03:04:05Z")
        self.assertIsNone(reader.validate(created["token"]))
        self.assertEqual(self.store.revoke(created["id"]), revoked)

    def test_expiry_boundary_and_dynamic_list_status(self):
        created = self.store.create(expires_in_hours=1)
        self.clock_mock.return_value = NOW + timedelta(hours=1) - timedelta(microseconds=1)
        self.assertIsNotNone(self.store.validate(created["token"]))
        self.clock_mock.return_value = NOW + timedelta(hours=1)
        self.assertIsNone(self.store.validate(created["token"]))
        self.assertEqual(self.store.list()[0]["status"], "expired")
        self.assertFalse(self.store.list()[0]["active"])

    def test_bad_tokens_never_authorize(self):
        created = self.store.create()
        changed = ("A" if created["token"][0] != "A" else "B") + created["token"][1:]
        for token in (None, 42, [], "", "x" * 42, "x" * 44, "!" * 43, created["token"] + "\n", changed):
            with self.subTest(kind=type(token).__name__):
                self.assertIsNone(self.store.validate(token))

    def test_invalid_create_input_rejected_without_touching_store(self):
        for lifetime in (0, -1, 169, 1.0, True, "24", None):
            with self.subTest(lifetime=lifetime), self.assertRaises(ValueError):
                self.store.create(expires_in_hours=lifetime)
        for label in (None, 12, "x" * 81, "line\nbreak", "tab\there", "null\0", "delete\x7f"):
            with self.subTest(label=repr(label)), self.assertRaises(ValueError):
                self.store.create(label=label)
        self.assertFalse(self.path.exists())

    def test_minimum_and_maximum_expiry_are_supported(self):
        for lifetime in (1, 168):
            created = self.store.create(expires_in_hours=lifetime)
            self.assertIsNotNone(self.store.validate(created["token"]))

    def test_invalid_revoke_id_does_not_change_existing_grant(self):
        created = self.store.create()
        before = self.path.read_bytes()
        for identity in (None, "", "../invalid", "F" * 32, "0" * 32):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.store.revoke(identity)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNotNone(self.store.validate(created["token"]))

    def test_corrupt_store_fails_closed_and_is_not_overwritten(self):
        created = self.store.create()
        for raw in (b"{", b"[]", b'{"version":2,"shares":[]}', b'{"version":1,"shares":[{}]}', b"\xff"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.assertIsNone(self.store.validate(created["token"]))
                with self.assertRaises(ValueError):
                    self.store.list()
                with self.assertRaises(ValueError):
                    self.store.create()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_invalid_record_fields_fail_closed(self):
        created = self.store.create()
        original = json.loads(self.path.read_text(encoding="utf-8"))
        for field, value in (("token_hash", "short"), ("expires_at", "forever"),
                             ("expires_at", "2035-01-01T00:00:00Z"),
                             ("revoked_at", "2020-01-01T00:00:00Z"), ("label", "bad\nlabel")):
            with self.subTest(field=field, value=value):
                changed = json.loads(json.dumps(original))
                changed["shares"][0][field] = value
                self.path.write_text(json.dumps(changed), encoding="utf-8")
                self.assertIsNone(self.store.validate(created["token"]))
        original["shares"].append(original["shares"][0])
        self.path.write_text(json.dumps(original), encoding="utf-8")
        self.assertIsNone(self.store.validate(created["token"]))

    def test_oversized_store_fails_closed(self):
        created = self.store.create()
        with patch("fl_dashboard.services.access_shares.MAX_STORE_BYTES", 10):
            self.assertIsNone(self.store.validate(created["token"]))
            with self.assertRaises(ValueError):
                self.store.list()

    def test_active_limit_and_bounded_history_preserve_live_grants(self):
        with patch("fl_dashboard.services.access_shares.MAX_ACTIVE_SHARES", 3), \
                patch("fl_dashboard.services.access_shares.MAX_STORED_SHARES", 5):
            live = [self.store.create() for _ in range(3)]
            with self.assertRaises(ValueError):
                self.store.create()
            self.store.revoke(live[1]["id"])
            self.store.revoke(live[2]["id"])
            for _ in range(7):
                temporary = self.store.create()
                self.store.revoke(temporary["id"])
            self.assertEqual(len(self.store.list()), 5)
            self.assertIsNotNone(self.store.validate(live[0]["token"]))
            self.assertEqual(sum(item["active"] for item in self.store.list()), 1)

    def test_two_instances_share_write_lock(self):
        another = AccessShareStore(self.path)
        with ThreadPoolExecutor(max_workers=6) as pool:
            created = list(pool.map(lambda i: (self.store if i % 2 else another).create(str(i)), range(18)))
        self.assertEqual(len(self.store.list()), 18)
        self.assertEqual(len({item["token"] for item in created}), 18)
        self.assertTrue(all(another.validate(item["token"]) is not None for item in created))

    def test_write_failure_does_not_destroy_existing_grants(self):
        created = self.store.create()
        before = self.path.read_bytes()
        with patch("fl_dashboard.services.access_shares.os.replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                self.store.create()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNotNone(self.store.validate(created["token"]))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


class PublicShareUrlTests(unittest.TestCase):
    def test_valid_urls_are_normalized(self):
        cases = {"https://Dashboard.Example.test:443/": "https://dashboard.example.test",
                 "https://192.168.1.2:8767/tools/": "https://192.168.1.2:8767/tools",
                 "https://[::1]:8767/": "https://[::1]:8767",
                 "https://localhost/test-dashboard/v2": "https://localhost/test-dashboard/v2"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(validate_public_url(value), expected)

    def test_invalid_url_cannot_leak_token_to_another_origin_or_url_component(self):
        values = (None, "", "http://dashboard.example.test", "https://user:pass@host.test",
                  "https://host.test/?redirect=elsewhere", "https://host.test/#existing", "https://host.test/?",
                  "https://host.test/#", "https://host.test/../admin", "https://host.test/%2fadmin",
                  "https://host.test//admin", "https://host.test\\@elsewhere.test", "https://host.test/\n",
                  "https://0.0.0.0", "https://[::]", "https://224.0.0.1", "https://host.test:0",
                  "https://host.test:99999", "https://host.test:abc", "https://-invalid.test", "https://host..test")
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_public_url(value)


if __name__ == "__main__":
    unittest.main()
