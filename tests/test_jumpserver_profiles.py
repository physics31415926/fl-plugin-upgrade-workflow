import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fl_dashboard.agents.codex_sessions import CodexSessionManager, SessionError
from fl_dashboard.resources.jumpserver_profiles import BACKEND_SOURCE, JumpServerProfiles, profile_id

ASSET = "11111111-2222-4333-8444-555555555555"
SECOND = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def catalog(**overrides):
    return {"enabled": True, "authenticated": True, "refreshing": False, "updated_at": "now", "error": None,
            "base_url": "https://jump.example.test:8443/", "username": "fixture.user",
            "ssh_host": "", "ssh_port": 2222, "identity_file": "", "default_account": "", "generation": 1,
            "assets": [{"id": ASSET, "name": "a100-lab", "accounts": ["worker"], "platform": "Linux",
                        "tags": [], "ssh_supported": True}], **overrides}


class Inventory:
    def __init__(self, data=None):
        self.data = data or catalog()

    def catalog(self):
        return copy.deepcopy(self.data)


class Manager:
    def __init__(self):
        self.rows, self.writes, self.snapshots = {}, [], 0

    def snapshot(self, **kwargs):
        self.snapshots += 1
        return {"machine_profiles": copy.deepcopy(list(self.rows.values()))}

    def save_profile(self, row):
        self.writes.append(copy.deepcopy(row))
        self.rows[row["id"]] = {**copy.deepcopy(row), "ssh_config_path": "/fixture/managed.conf"}
        return copy.deepcopy(self.rows[row["id"]])


class JumpServerProfilesTests(unittest.TestCase):
    def setUp(self):
        self.inventory, self.manager = Inventory(), Manager()
        self.bridge = JumpServerProfiles(self.inventory)

    def rows(self):
        return self.bridge.profiles("vllm", self.manager)

    def test_import_uses_uuid_stable_identity_isolated_gateway_and_name_hint(self):
        row = self.rows()[0]
        self.assertEqual(row["id"], profile_id(self.inventory.data["base_url"], ASSET))
        self.assertEqual(row["ssh_alias"], row["id"])
        self.assertEqual(row["source"], "jumpserver")
        self.assertEqual(row["label"], "a100-lab")
        self.assertEqual(row["backend"], "cuda")
        self.assertEqual(row["backend_source"], BACKEND_SOURCE)
        self.assertEqual(row["remote_workspace"], "")
        self.assertEqual(row["model_paths"], {})
        self.assertEqual(row["ssh_connection"], {"host": "jump.example.test", "user": "fixture.user#worker#" + ASSET,
                         "port": 2222, "identity_file": "", "jump_alias": "", "inherit_ssh_config": False})
        self.assertIsNone(row["configuration_error"])
        self.rows()
        self.assertEqual(len(self.manager.writes), 1)
        self.assertEqual(self.manager.snapshots, 2)

    def test_identity_normalizes_url_but_separates_servers_and_not_projects(self):
        self.assertEqual(profile_id("https://JUMP.example.test:443/", ASSET), profile_id("https://jump.example.test", ASSET))
        self.assertNotEqual(profile_id("https://jump.example.test", ASSET), profile_id("https://other.example.test", ASSET))
        self.assertNotEqual(profile_id("https://jump.example.test/one", ASSET), profile_id("https://jump.example.test/two", ASSET))
        first = self.rows()[0]
        second = self.bridge.profiles("sglang", Manager())[0]
        self.assertEqual(first["id"], second["id"])

    def test_user_workspace_models_and_verified_backend_survive_name_connection_changes(self):
        row = self.rows()[0]
        self.manager.rows[row["id"]].update(remote_workspace="/work/plugin", model_paths={"model": "/models/one"},
                                             backend="ascend", backend_source="硬件核验", notes="Keep note")
        self.inventory.data["assets"][0]["name"] = "new-a100-name"
        self.inventory.data["ssh_host"] = "gateway.example.test"
        result = self.rows()[0]
        self.assertEqual(result["remote_workspace"], "/work/plugin")
        self.assertEqual(result["model_paths"], {"model": "/models/one"})
        self.assertEqual(result["backend"], "ascend")
        self.assertEqual(result["notes"], "Keep note")
        self.assertEqual(result["ssh_connection"]["host"], "gateway.example.test")
        self.assertEqual(result["id"], row["id"])
        self.rows()
        self.assertEqual(len(self.manager.writes), 2)

    def test_missing_or_ambiguous_account_never_defaults_to_root(self):
        for accounts in ([], ["root", "worker"], ["@INPUT"], ["bad#name"]):
            with self.subTest(accounts=accounts):
                self.inventory.data["assets"][0]["accounts"] = accounts
                result = self.rows()[0]
                self.assertTrue(result["configuration_error"])
                self.assertIsNone(result["ssh_connection"])
                self.assertIsNone(result["ssh_config_path"])
        self.assertEqual(self.manager.writes, [])
        self.inventory.data["assets"][0]["accounts"] = ["root", "worker"]
        self.inventory.data["default_account"] = "worker"
        self.assertIn("#worker#", self.rows()[0]["ssh_connection"]["user"])

    def test_monitor_and_task_accounts_are_independent_and_monitor_never_saves(self):
        self.inventory.data.update(default_account="secure", task_account="root")
        self.inventory.data["assets"][0]["accounts"] = ["secure", "root"]
        task = self.rows()[0]
        saved = copy.deepcopy(self.manager.rows)
        self.assertIn("#root#", task["ssh_connection"]["user"])
        monitor = self.bridge.profiles("vllm", self.manager, purpose="monitor")[0]
        self.assertIn("#secure#", monitor["ssh_connection"]["user"])
        self.assertIsNone(monitor["ssh_config_path"])
        self.assertEqual(task["id"], monitor["id"])
        self.assertEqual(saved, self.manager.rows)
        self.assertEqual(1, len(self.manager.writes))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.bridge.profiles("vllm", self.manager, purpose="monitor" if i % 2 else "task"), range(10)))
        self.assertEqual(saved, self.manager.rows)
        self.assertEqual(1, len(self.manager.writes))
        self.inventory.data["task_account"] = ""
        self.assertIn("#secure#", self.rows()[0]["ssh_connection"]["user"])

    def test_account_selection_requires_authorization_for_each_purpose(self):
        self.inventory.data.update(default_account="secure", task_account="not-authorized")
        self.inventory.data["assets"][0]["accounts"] = ["secure", "root"]
        self.assertIn("任务执行账号", self.rows()[0]["configuration_error"])
        monitor = self.bridge.profiles("vllm", self.manager, purpose="monitor")[0]
        self.assertIsNone(monitor["configuration_error"])
        self.inventory.data.update(default_account="not-authorized", task_account="root")
        self.assertIsNone(self.rows()[0]["configuration_error"])
        self.assertIn("设备采集账号", self.bridge.profiles("vllm", self.manager, purpose="monitor")[0]["configuration_error"])
        self.inventory.data.update(default_account="secure", task_account="root")
        self.inventory.data["assets"][0]["accounts"] = ["secure"]
        self.assertIn("平台未返回所选任务执行账号（root）", self.rows()[0]["configuration_error"])
        self.assertIn("#secure#", self.bridge.profiles("vllm", self.manager, purpose="monitor")[0]["ssh_connection"]["user"])
        self.inventory.data["assets"][0]["accounts"] = ["root"]
        monitor = self.bridge.profiles("vllm", self.manager, purpose="monitor")[0]
        self.assertIn("平台未返回所选设备采集账号（secure）", monitor["configuration_error"])
        self.assertIsNone(monitor["ssh_connection"])
        self.assertIn("#root#", self.rows()[0]["ssh_connection"]["user"])
        self.inventory.data.update(default_account="", task_account="")
        self.assertIn("#root#", self.rows()[0]["ssh_connection"]["user"])
        self.assertIn("#root#", self.bridge.profiles("vllm", self.manager, purpose="monitor")[0]["ssh_connection"]["user"])
        with self.assertRaises(ValueError):
            self.bridge.profiles("vllm", self.manager, purpose="invalid")

    def test_revoked_assets_disappear_without_deleting_owner_profiles(self):
        original = self.rows()[0]
        self.inventory.data["assets"] = []
        self.assertEqual(self.rows(), [])
        self.assertIn(original["id"], self.manager.rows)
        self.assertEqual(len(self.manager.writes), 1)

    def test_inactive_assets_are_hidden_but_owner_profiles_remain(self):
        original = self.rows()[0]
        saved = copy.deepcopy(self.manager.rows)
        self.inventory.data["assets"][0]["is_active"] = False
        self.inventory.data["assets"].append({**self.inventory.data["assets"][0], "id": SECOND, "is_active": True})
        for purpose in ("task", "monitor"):
            with self.subTest(purpose=purpose):
                rows = self.bridge.profiles("vllm", self.manager, purpose=purpose)
                self.assertEqual([profile_id(self.inventory.data["base_url"], SECOND)], [row["id"] for row in rows])
        self.assertEqual(saved[original["id"]], self.manager.rows[original["id"]])
        self.assertEqual(2, len(self.manager.rows))
        for invalid in (None, 0, "false"):
            self.inventory.data["assets"][1]["is_active"] = invalid
            self.assertEqual([], self.rows())

    def test_expired_authentication_or_refresh_error_returns_blocked_cached_rows(self):
        original = self.rows()[0]
        for values in ({"authenticated": False}, {"error": "private remote error content"}):
            self.inventory.data = catalog(**values)
            row = self.rows()[0]
            self.assertTrue(row["configuration_error"])
            self.assertNotIn("private remote", row["configuration_error"])
            self.assertIsNone(row["ssh_connection"])
            self.assertIsNone(row["last_check"])
        self.assertEqual(len(self.manager.writes), 1)
        self.assertIsNotNone(self.manager.rows[original["id"]]["ssh_connection"])

    def test_filters_nonssh_invalid_and_duplicate_assets_and_unknown_names(self):
        good = self.inventory.data["assets"][0]
        self.inventory.data["assets"] = [good, copy.deepcopy(good), {**good, "id": SECOND, "ssh_supported": False},
                                           {**good, "id": "../not-a-uuid"}, {**good, "id": SECOND, "name": "unrecognized"}]
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["backend"], "")
        self.assertEqual(rows[1]["backend_recognition"], "unknown")
        self.assertEqual(self.manager.snapshots, 1)

    def test_per_asset_save_failure_does_not_break_other_assets_or_leak_error(self):
        self.inventory.data["assets"].append({**self.inventory.data["assets"][0], "id": SECOND})
        original = self.manager.save_profile
        def save(row):
            if row["id"] == profile_id(self.inventory.data["base_url"], ASSET):
                raise SessionError("private RPC body")
            return original(row)
        with patch.object(self.manager, "save_profile", side_effect=save):
            rows = self.rows()
        self.assertTrue(rows[0]["configuration_error"])
        self.assertNotIn("private RPC", rows[0]["configuration_error"])
        self.assertIsNone(rows[1]["configuration_error"])

    def test_simultaneous_workbench_and_fleet_imports_save_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.rows(), range(12)))
        self.assertEqual(len(self.manager.writes), 1)
        self.assertTrue(all(result[0]["id"] == results[0][0]["id"] for result in results))
        self.assertEqual(self.manager.snapshots, 12)

    def test_disabled_source_never_touches_manager(self):
        self.inventory.data["enabled"] = False
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.manager.snapshots, 0)

    def test_invalid_gateway_values_are_nonconnectable(self):
        for change in ({"username": "worker#inject"}, {"ssh_host": "host\nProxyCommand bad"},
                       {"ssh_port": True}, {"ssh_port": 0}, {"identity_file": "relative.pem"}):
            self.inventory.data = catalog(**change)
            self.assertTrue(self.rows()[0]["configuration_error"])
        self.assertEqual(self.manager.writes, [])


class JumpServerSSHConfigTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(CodexSessionManager)
        self.user = "fixture.user#worker#" + ASSET
        self.connection = {"host": "gateway.example.test", "user": self.user, "port": 2222, "inherit_ssh_config": False}

    def test_valid_koko_user_is_quoted_and_user_config_not_included(self):
        connection = self.manager._ssh_connection(self.connection)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "device.conf"
            self.manager._write_ssh_config(target, "jms-fixture", connection)
            text = target.read_text(encoding="utf-8")
        self.assertIn('  User "' + self.user + '"', text)
        self.assertNotIn("Include", text)
        self.assertIn("StrictHostKeyChecking yes", text)
        self.assertIn("BatchMode yes", text)

    def test_rejects_malformed_koko_names_and_inherit_flag_types(self):
        for user in ("user#account", "user#account#asset-name", "user##" + ASSET, "user#account#" + ASSET + "#extra",
                     "-oProxyCommand#account#" + ASSET, "user#account;cmd#" + ASSET, "user#account\n#" + ASSET,
                     'user#account"#' + ASSET, "user#$(cmd)#" + ASSET):
            with self.subTest(user=user), self.assertRaises(SessionError):
                self.manager._ssh_connection({**self.connection, "user": user})
        for flag in ("false", 0, None, []):
            with self.subTest(flag=flag), self.assertRaises(SessionError):
                self.manager._ssh_connection({**self.connection, "inherit_ssh_config": flag})

    def test_ordinary_managed_config_preserves_existing_shape_and_include(self):
        connection = self.manager._ssh_connection({"host": "gateway.example.test", "user": "worker", "port": 22})
        self.assertNotIn("inherit_ssh_config", connection)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "device.conf"
            self.manager._write_ssh_config(target, "old-alias", connection)
            text = target.read_text(encoding="utf-8")
        self.assertIn("  User worker\n", text)
        self.assertIn('Include "' + (Path.home() / ".ssh" / "config").as_posix() + '"', text)

    def test_real_profile_save_creates_isolated_config_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = self.manager
            manager.root, manager._lock, manager._profiles = Path(temporary), threading.RLock(), {}
            manager.backends = {"cuda"}
            manager.snapshot = lambda **_: {"machine_profiles": copy.deepcopy(list(manager._profiles.values())), "backends": ["cuda"]}
            bridge = JumpServerProfiles(Inventory())
            with patch.object(manager, "save_profile", wraps=manager.save_profile) as save:
                first = bridge.profiles("vllm", manager)[0]
                second = bridge.profiles("vllm", manager)[0]
                self.assertEqual(save.call_count, 1)
            self.assertIsNone(first["configuration_error"])
            self.assertEqual(first["id"], second["id"])
            self.assertNotIn("Include", Path(first["ssh_config_path"]).read_text())


if __name__ == "__main__":
    unittest.main()
