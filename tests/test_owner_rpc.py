from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from fl_dashboard.projects.project_registry import ProjectError, RemoteSessionManager
from fl_dashboard.services.owner_rpc import OwnerRPCError, owner_rpc_token


class OwnerRPCTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "owner-private" / "rpc-credential.json"

    def test_random_credential_is_stable_and_windows_storage_is_encrypted(self):
        token = owner_rpc_token(self.path)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", token) is not None)
        self.assertTrue(token == owner_rpc_token(self.path))
        raw = self.path.read_text(encoding="utf-8")
        record = json.loads(raw)
        if os.name == "nt":
            self.assertFalse(token in raw)
            self.assertEqual("windows-dpapi-current-user", record["protection"])
            self.assertNotIn("token", record)
        else:
            self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(self.path.parent.stat().st_mode))
        self.assertEqual([], list(self.path.parent.glob("*.tmp")))

    def test_concurrent_initializers_all_receive_one_complete_credential(self):
        with ThreadPoolExecutor(max_workers=8) as workers:
            tokens = list(workers.map(lambda _: owner_rpc_token(self.path), range(24)))
        self.assertEqual(1, len(set(tokens)))
        self.assertTrue(tokens[0] == owner_rpc_token(self.path))
        self.assertEqual([], list(self.path.parent.glob("*.tmp")))

    def test_malformed_or_oversized_file_is_never_replaced(self):
        self.path.parent.mkdir()
        for data in (b"", b"broken-json-secret-marker", b"{}", b"x" * 20000,
                     b'{"schema_version":true,"token":"not-a-token"}'):
            with self.subTest(size=len(data)):
                self.path.write_bytes(data)
                with self.assertRaises(OwnerRPCError) as caught:
                    owner_rpc_token(self.path)
                self.assertFalse("secret-marker" in str(caught.exception))
                self.assertTrue(data == self.path.read_bytes())

    def test_relative_empty_and_directory_paths_are_rejected(self):
        for path in ("relative.json", "", None, self.root):
            with self.subTest(path_type=type(path).__name__), self.assertRaises(OwnerRPCError):
                owner_rpc_token(path)

    def test_encryption_failure_has_no_plaintext_fallback(self):
        with patch("fl_dashboard.services.owner_rpc.WINDOWS", True), patch(
                "fl_dashboard.services.owner_rpc._dpapi", side_effect=RuntimeError("RAW_PRIVATE_MATERIAL")):
            with self.assertRaises(OwnerRPCError) as caught:
                owner_rpc_token(self.path)
        self.assertFalse("RAW_PRIVATE_MATERIAL" in str(caught.exception))
        self.assertFalse(self.path.exists())
        self.assertEqual([], list(self.path.parent.glob("*.tmp")))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI case")
    def test_unreadable_dpapi_record_is_not_overwritten(self):
        owner_rpc_token(self.path)
        before = self.path.read_bytes()
        with patch("fl_dashboard.services.owner_rpc._dpapi", side_effect=RuntimeError("RAW_PRIVATE_MATERIAL")):
            with self.assertRaises(OwnerRPCError):
                owner_rpc_token(self.path)
        self.assertTrue(before == self.path.read_bytes())

    def test_atomic_publication_failure_leaves_no_partial_credential(self):
        with patch("fl_dashboard.services.owner_rpc.os.link", side_effect=OSError("unsupported publication")):
            with self.assertRaises(OwnerRPCError):
                owner_rpc_token(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual([], list(self.path.parent.glob("*.tmp")))

    def test_symlink_target_is_rejected(self):
        owner_rpc_token(self.path)
        link = self.root / "linked-credential.json"
        try:
            link.symlink_to(self.path)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        with self.assertRaises(OwnerRPCError):
            owner_rpc_token(link)

    @unittest.skipIf(os.name == "nt", "POSIX permission enforcement")
    def test_insecure_existing_posix_permissions_fail_closed(self):
        owner_rpc_token(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(OwnerRPCError):
            owner_rpc_token(self.path)
        self.assertEqual(0o644, stat.S_IMODE(self.path.stat().st_mode))
        self.path.chmod(0o600)
        self.path.parent.chmod(0o755)
        with self.assertRaises(OwnerRPCError):
            owner_rpc_token(self.path)

    def manager(self, **extra):
        return RemoteSessionManager({"sessions_api_url": "http://127.0.0.1:8765",
                                     "sessions_dir": str(self.root / "sessions"),
                                     "evidence_dir": str(self.root / "evidence"), **extra}, self.root)

    def connection(self):
        connection = Mock()
        response = Mock(status=200)
        response.read.return_value = b'{"sessions":[],"machine_profiles":[]}'
        connection.getresponse.return_value = response
        return connection

    def test_proxy_uses_opt_in_credential_for_get_and_post_without_serializing_it(self):
        manager = self.manager(sessions_owner_token_file=str(self.path))
        token = owner_rpc_token(self.path)
        connection = self.connection()
        with patch("fl_dashboard.projects.project_registry.HTTPConnection", return_value=connection):
            manager.snapshot(compact=True)
            manager.save_profile({"id": "fixture-profile"})
        calls = connection.request.call_args_list
        self.assertEqual(2, len(calls))
        for call in calls:
            self.assertTrue(call.args[3].get("X-FL-Session-Owner") == token)
            self.assertFalse(token in (call.args[2] or b"").decode("utf-8"))
            self.assertFalse(token in call.args[1])
        self.assertFalse(token in json.dumps(manager.config))
        self.assertEqual("GET", calls[0].args[0])
        self.assertEqual("POST", calls[1].args[0])

    def test_legacy_proxy_sends_no_owner_header_and_creates_no_credentials(self):
        manager = self.manager()
        connection = self.connection()
        with patch("fl_dashboard.projects.project_registry.HTTPConnection", return_value=connection):
            manager.snapshot(compact=True)
            manager.save_profile({"id": "fixture-profile"})
        for call in connection.request.call_args_list:
            self.assertNotIn("X-FL-Session-Owner", call.args[3])
        self.assertFalse(self.path.exists())

    def test_explicit_invalid_credential_path_and_nonloopback_owner_fail_before_rpc(self):
        for config in ({"sessions_owner_token_file": "relative.json"},
                       {"sessions_owner_token_file": None},
                       {"sessions_owner_token_file": str(self.path), "sessions_api_url": "http://elsewhere.example.test:8765"}):
            with self.subTest(config_keys=list(config)), self.assertRaises(ProjectError):
                self.manager(**config)
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir()
        self.path.write_bytes(b"invalid-private-credential")
        with self.assertRaises(ProjectError):
            self.manager(sessions_owner_token_file=str(self.path))
        self.assertTrue(self.path.read_bytes() == b"invalid-private-credential")


if __name__ == "__main__":
    unittest.main()
