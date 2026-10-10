import copy
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from flflow import make_server
from project_registry import ProjectContext, ProjectRegistry
from ssh_profile_sync import SOURCE, SSHProfileSync, read_config


class Profiles:
    def __init__(self, profiles=()):
        self.profiles = copy.deepcopy(list(profiles))
        self.writes = []

    def snapshot(self, **kwargs):
        return {"machine_profiles": copy.deepcopy(self.profiles), "sessions": [{"id": "a" * 32, "status": "running"}]}

    def save_profile(self, body):
        self.writes.append(copy.deepcopy(body))
        self.profiles.append(copy.deepcopy(body))
        return copy.deepcopy(body)


class SSHSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config"
        self.config.write_text("Host gpu\n HostName old.example.test\n User worker\n Port 2222\n", encoding="utf-8")
        self.state = self.root / "state.json"
        self.sync = SSHProfileSync(self.config, self.state)
        self.manager = Profiles()

    def snapshot(self):
        return self.sync.synchronize(self.manager, self.manager.snapshot())

    def test_new_aliases_are_imported_without_guessing_backend_or_workspace(self):
        result = self.snapshot()
        profile = result["machine_profiles"][0]
        self.assertEqual("gpu", profile["ssh_alias"])
        self.assertEqual("", profile["backend"])
        self.assertEqual("", profile["remote_workspace"])
        self.assertEqual(SOURCE, profile["source"])
        self.assertEqual(2222, profile["ssh_config_connection"]["port"])
        self.assertEqual(1, result["ssh_config_sync"]["added"])
        self.snapshot()
        self.assertEqual(1, len(self.manager.writes), "ordinary refresh never duplicates imports")

    def test_same_alias_changes_are_visible_and_invalidate_checks(self):
        self.snapshot()
        self.manager.profiles[0].update(label="Saved hardware", backend="cuda", remote_workspace="/work/plugin",
                                       model_paths={"Model": "/models/model"}, last_check={"ok": True}, last_connection_check={"ok": True})
        self.config.write_text("Host gpu\n HostName new.example.test\n Port 2200\n", encoding="utf-8")
        result = self.snapshot()
        profile = result["machine_profiles"][0]
        self.assertEqual("new.example.test", profile["ssh_config_connection"]["host"])
        self.assertEqual(2200, profile["ssh_config_connection"]["port"])
        self.assertTrue(profile["ssh_config_changed"])
        self.assertIsNone(profile["last_check"])
        self.assertIsNone(profile["last_connection_check"])
        self.assertEqual("Saved hardware", profile["label"])
        self.assertEqual("/work/plugin", profile["remote_workspace"])
        self.assertEqual({"Model": "/models/model"}, profile["model_paths"])
        self.assertEqual(1, result["ssh_config_sync"]["updated"])

    def test_deleted_imports_disappear_without_deleting_active_session_profiles(self):
        first = self.snapshot()
        self.config.write_text("Host replacement\n HostName new.example.test\n", encoding="utf-8")
        result = self.snapshot()
        self.assertEqual(["replacement"], [p["ssh_alias"] for p in result["machine_profiles"]])
        self.assertEqual(1, result["ssh_config_sync"]["removed"])
        self.assertEqual(first["sessions"], result["sessions"])
        self.assertEqual("gpu", self.manager.profiles[0]["ssh_alias"], "owner keeps existing task references")

    def test_hand_saved_alias_or_direct_profile_is_never_overwritten_or_hidden(self):
        manual = {"id": "manual", "ssh_alias": "gpu", "source": "manual", "ssh_connection": {"host": "direct.example.test"}}
        self.manager = Profiles([manual])
        self.assertEqual([manual], self.snapshot()["machine_profiles"])
        self.config.write_text("", encoding="utf-8")
        self.assertEqual([manual], self.snapshot()["machine_profiles"])
        self.assertEqual([], self.manager.writes)

    def test_include_changes_and_first_value_wildcards_are_resolved(self):
        include = self.root / "gpu.conf"
        self.config.write_text("Include *.conf\nHost *\n User fallback\n Port 22\n", encoding="utf-8")
        include.write_text("Host gpu other\n HostName initial.example.test\n User specific\n", encoding="utf-8")
        result = self.snapshot()
        self.assertEqual(2, result["ssh_config_sync"]["aliases"])
        self.assertEqual("specific", result["machine_profiles"][0]["ssh_config_connection"]["user"])
        include.write_text("Host gpu\n HostName updated.example.test\n User specific\n", encoding="utf-8")
        result = self.snapshot()
        self.assertEqual(1, result["ssh_config_sync"]["removed"])
        self.assertEqual(1, result["ssh_config_sync"]["updated"])
        self.assertEqual("updated.example.test", result["machine_profiles"][0]["ssh_config_connection"]["host"])

    def test_private_key_bytes_and_proxy_commands_are_not_read_or_projected(self):
        key = self.root / "identity.pem"
        key.write_text("EXPLICIT_DUMMY_KEY_CONTENT")
        self.config.write_text(f'Host gpu\n IdentityFile "{key}"\n ProxyCommand never_execute DUMMY_PRIVATE_TOKEN\n', encoding="utf-8")
        result = self.snapshot()
        encoded = json.dumps(result)
        self.assertNotIn("EXPLICIT_DUMMY_KEY_CONTENT", encoded)
        self.assertNotIn("DUMMY_PRIVATE_TOKEN", encoded)
        self.assertTrue(result["machine_profiles"][0]["ssh_config_connection"]["uses_proxy_command"])

    def test_invalid_or_missing_config_preserves_profiles_with_safe_error(self):
        self.snapshot()
        before = copy.deepcopy(self.manager.profiles)
        self.config.write_text('Host gpu\n User "unterminated\n')
        result = self.snapshot()
        self.assertEqual("error", result["ssh_config_sync"]["status"])
        self.assertEqual(before, result["machine_profiles"])
        self.assertNotIn(str(self.root), result["ssh_config_sync"]["error"])
        self.config.unlink()
        self.assertEqual("missing", self.snapshot()["ssh_config_sync"]["status"])

    def test_include_cycle_is_rejected_without_partial_import(self):
        self.config.write_text("Host gpu\nInclude config\n")
        self.assertEqual("error", self.snapshot()["ssh_config_sync"]["status"])
        self.assertEqual([], self.manager.writes)

    def test_match_exec_is_not_executed_and_wildcard_aliases_are_not_imported(self):
        self.config.write_text("Host gpu *.test !excluded\n User worker\nMatch exec never_execute\n User forbidden\n")
        aliases, _ = read_config(self.config)
        self.assertEqual(["gpu"], list(aliases))
        self.assertEqual("worker", aliases["gpu"]["connection"]["user"])

    def test_stale_checks_survive_restart_until_rechecked(self):
        self.snapshot()
        self.config.write_text("Host gpu\n HostName changed.example.test\n")
        self.snapshot()
        restarted = SSHProfileSync(self.config, self.state)
        self.assertTrue(restarted.synchronize(self.manager, self.manager.snapshot())["machine_profiles"][0]["ssh_config_changed"])
        restarted.checked("gpu")
        self.assertFalse(restarted.synchronize(self.manager, self.manager.snapshot())["machine_profiles"][0]["ssh_config_changed"])

    def test_http_refresh_imports_changes_and_manual_sync_disallows_client_paths(self):
        monitor = type("Monitor", (), {"config": {"ssh_import_config": str(self.config), "sessions_dir": str(self.root)}})()
        project = ProjectContext("vllm", "Plugin", monitor=monitor, sessions=self.manager)
        server = make_server(None, "127.0.0.1", 0, registry=ProjectRegistry([project]))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/codex-sessions")
        response = connection.getresponse()
        self.assertEqual(200, response.status)
        self.assertEqual(1, len(json.loads(response.read())["machine_profiles"]))
        self.config.write_text("Host gpu newly_added\n HostName current.example.test\n")
        connection.request("POST", "/api/codex-machines/sync-ssh", body="{}", headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(200, response.status)
        data = json.loads(response.read())
        self.assertEqual(2, len(data["profiles"]))
        self.assertEqual(1, data["ssh_config_sync"]["added"])
        connection.request("POST", "/api/codex-machines/sync-ssh", body='{"path":"/untrusted"}', headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(400, response.status)
        response.read()


if __name__ == "__main__":
    unittest.main()
