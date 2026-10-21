import copy
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from fl_dashboard.services.flflow import make_server
from fl_dashboard.projects.project_registry import ProjectContext, ProjectRegistry
from fl_dashboard.tasks.ssh_profile_sync import SOURCE, SSHProfileSync, read_config
from fl_dashboard.tasks.device_recognition import ALIAS_BACKEND_SOURCE, alias_backend, hardware_backend


class Profiles:
    def __init__(self, profiles=()):
        self.profiles = copy.deepcopy(list(profiles))
        self.writes = []

    def snapshot(self, **kwargs):
        return {"machine_profiles": copy.deepcopy(self.profiles), "sessions": [{"id": "a" * 32, "status": "running"}]}

    def save_profile(self, body):
        self.writes.append(copy.deepcopy(body))
        existing = next((index for index, profile in enumerate(self.profiles) if profile.get("id") == body.get("id")), None)
        if existing is None:
            self.profiles.append(copy.deepcopy(body))
        else:
            self.profiles[existing] = copy.deepcopy(body)
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

    def test_device_names_are_classified_and_unrecognized_aliases_stay_unknown(self):
        expected = {"a100_lab": "cuda", "a800_lab": "cuda", "h20_lab": "cuda", "h800_lab": "cuda",
                    "ascend_910b": "ascend", "metax_c550_lab": "metax", "tianshu_lab": "corex",
                    "hy_lab": "hygon", "mthreads_lab": "musa", "suiyuan_lab": "enflame",
                    "tsingmicro_lab": "tsingmicro", "xiwang_lab": "sunrise", "kunlunxin_lab": "kunlunxin",
                    "810e_lab": "thead", "gpu": "", "hy_a100": "", "p800_lab": "", "s60_lab": ""}
        self.config.write_text("Host " + " ".join(expected) + "\n", encoding="utf-8")
        result = self.snapshot()
        profiles = {row["ssh_alias"]: row for row in result["machine_profiles"]}
        persisted = {row["ssh_alias"]: row for row in self.manager.snapshot()["machine_profiles"]}
        for alias, backend in expected.items():
            with self.subTest(alias=alias):
                self.assertEqual(profiles[alias]["backend"], backend)
                self.assertEqual(persisted[alias]["backend"], backend)
                self.assertEqual(profiles[alias]["backend_recognition"], "inferred" if backend else "unknown")
                self.assertEqual(profiles[alias]["backend_source"], ALIAS_BACKEND_SOURCE if backend else "")
        self.assertEqual(result["ssh_config_sync"]["unrecognized"], 4)
        self.assertEqual(result["ssh_config_sync"]["recognized"], 14)
        self.assertIsNone(alias_backend("musa-notes-nvidia"))
        self.assertIsNone(alias_backend("example-a1000"))
        self.assertIsNone(hardware_backend("hy"), "alias abbreviations never identify actual hardware")

    def test_existing_empty_import_is_classified_once_with_the_same_saved_identity(self):
        self.config.write_text("Host h20_lab\n", encoding="utf-8")
        profile = {"id": "existing", "ssh_alias": "h20_lab", "source": SOURCE, "backend": "",
                   "notes": "retained", "remote_workspace": "/work/existing"}
        self.manager = Profiles([profile])
        first = self.snapshot()
        saved = self.manager.profiles[0]
        self.assertEqual(saved["id"], profile["id"])
        self.assertEqual(saved["backend"], "cuda")
        self.assertEqual(saved["notes"], "retained")
        self.assertEqual(saved["remote_workspace"], "/work/existing")
        self.assertEqual(first["ssh_config_sync"]["classified"], 1)
        self.assertEqual(self.snapshot()["ssh_config_sync"]["classified"], 0)
        self.assertEqual(len(self.manager.writes), 1)

    def test_duplicate_imports_prefer_configured_profile_without_deleting_owner_records(self):
        self.config.write_text("Host h20_lab\n", encoding="utf-8")
        empty = {"id": "imported", "ssh_alias": "h20_lab", "source": SOURCE, "backend": ""}
        configured = {"id": "existing", "ssh_alias": "h20_lab", "source": SOURCE,
                      "backend": "metax", "backend_source": "confirmed hardware", "remote_workspace": "/work/plugin"}
        for profiles in ([empty, configured], [configured, empty]):
            with self.subTest(order=[p["id"] for p in profiles]):
                self.manager = Profiles(profiles)
                result = self.snapshot()
                self.assertEqual(len(result["machine_profiles"]), 1)
                visible = result["machine_profiles"][0]
                self.assertEqual(visible["id"], "existing")
                self.assertEqual(visible["backend"], "metax")
                self.assertEqual(visible["backend_recognition"], "configured")
                self.assertEqual(self.manager.profiles, profiles)
                self.assertEqual(self.manager.writes, [])
                self.assertEqual(result["sessions"], self.manager.snapshot()["sessions"])

    def test_saved_noncompute_role_wins_over_imported_compute_duplicate(self):
        self.config.write_text("Host h20_lab\n", encoding="utf-8")
        for role in ("development", "gateway"):
            for backend in ("", "cuda"):
                compute = {"id": "compute-copy", "ssh_alias": "h20_lab", "source": SOURCE,
                           "role": "compute", "backend": backend}
                restricted = {"id": "saved-role", "ssh_alias": "h20_lab", "source": SOURCE,
                              "role": role, "backend": "", "notes": "User-classified role"}
                for profiles in ([compute, restricted], [restricted, compute]):
                    with self.subTest(role=role, backend=backend, order=[p["id"] for p in profiles]):
                        self.manager = Profiles(profiles)
                        result = self.snapshot()
                        self.assertEqual(len(result["machine_profiles"]), 1)
                        visible = result["machine_profiles"][0]
                        self.assertEqual(visible["id"], "saved-role")
                        self.assertEqual(visible["role"], role)
                        self.assertEqual(visible["backend"], "")
                        self.assertEqual(visible["notes"], "User-classified role")
                        self.assertEqual(self.manager.profiles, profiles)
                        self.assertEqual(self.manager.writes, [])
                        self.assertEqual(result["ssh_config_sync"]["recognized"], 0)
                        self.assertEqual(result["ssh_config_sync"]["unrecognized"], 0)

    def test_manual_profile_wins_over_duplicate_import_but_keeps_original_records(self):
        manual = {"id": "manual", "ssh_alias": "gpu", "source": "manual", "backend": "ascend"}
        imported = {"id": "import", "ssh_alias": "gpu", "source": SOURCE, "backend": ""}
        self.manager = Profiles([imported, manual])
        self.assertEqual(self.snapshot()["machine_profiles"], [manual])
        self.assertEqual(self.manager.profiles, [imported, manual])
        self.assertEqual(self.manager.writes, [])

    def test_deleted_alias_is_counted_once_even_though_owner_retains_task_records(self):
        first = self.snapshot()
        self.config.write_text("Host replacement\n", encoding="utf-8")
        changed = self.snapshot()
        unchanged = self.snapshot()
        self.assertEqual(changed["ssh_config_sync"]["removed"], 1)
        self.assertEqual(changed["ssh_config_sync"]["added"], 1)
        self.assertEqual(unchanged["ssh_config_sync"]["removed"], 0)
        self.assertEqual(unchanged["ssh_config_sync"]["added"], 0)
        self.assertFalse(unchanged["ssh_config_sync"]["changed"])
        self.assertEqual(first["sessions"], unchanged["sessions"])
        self.assertIn("gpu", [p["ssh_alias"] for p in self.manager.profiles])

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
