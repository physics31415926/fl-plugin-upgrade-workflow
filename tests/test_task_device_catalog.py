import copy
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fl_dashboard.agents.codex_sessions import CodexSessionManager
from fl_dashboard.resources.fleet import DeviceFleet, STALE_SECONDS
from fl_dashboard.resources.jumpserver_profiles import JumpServerProfiles
from fl_dashboard.resources.telemetry import DEVICE_LABELS
from fl_dashboard.tasks.ssh_profile_sync import SOURCE, read_config


ASSET = "11111111-2222-4333-8444-555555555555"


def telemetry(backend="metax", family="metax"):
    return {"reachable": True, "verdict": "unknown", "backend": backend,
            "backend_label": DEVICE_LABELS[backend], "collection_complete": True,
            "error": "无法确认空闲容量", "node": {},
            "accel": {"family": family, "card_name": "Fixture accelerator", "total": 1,
                      "free": 0, "busy": 0, "unknown": 1, "residual": 0,
                      "cards": [{"index": 0, "name": "Fixture accelerator", "busy": None,
                                 "state": "unknown", "procs": []}]}}


class Sync:
    def __init__(self, path):
        self.path, self.state = path, {}

    def synchronize(self, manager, snapshot):
        self.state["file_fingerprint"] = read_config(self.path)[1]
        return {**snapshot, "ssh_config_sync": {"status": "synced"}}


class TaskDeviceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.ssh = self.root / "ssh-config"
        self.ssh.write_text("Host fixture\n  HostName fixture.example.test\n", encoding="utf-8")
        self.rows = [{"id": "fixture", "ssh_alias": "fixture", "label": "Current device",
                      "source": SOURCE, "role": "compute", "backend": "", "hardware": "",
                      "remote_workspace": "/current/workspace", "model_paths": {"model": "/current/model"}}]
        self.manager = object.__new__(CodexSessionManager)
        self.manager.root, self.manager.backends, self.manager.config = self.owner, set(CodexSessionManager.BACKENDS), {}
        self.manager._lock, self.manager._profiles = threading.RLock(), {}
        self.manager.snapshot = Mock(side_effect=lambda **kw: {
            "machine_profiles": copy.deepcopy(self.rows), "sessions": [], "backends": sorted(self.manager.backends)})
        self.project = SimpleNamespace(id="vllm", monitor=SimpleNamespace(config={}), sessions=self.manager)
        self.sync, self.now = Sync(self.ssh), 1000.0
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, b"fixture", b""))
        self.view = telemetry()
        self.parse = patch("fl_dashboard.resources.fleet.parse_telemetry",
                           side_effect=lambda *args: (copy.deepcopy(self.view), None))
        self.parse.start()
        self.addCleanup(self.parse.stop)
        self.probe = patch("fl_dashboard.resources.fleet.probe_script", return_value="true")
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def make(self, **kwargs):
        fleet = DeviceFleet(SimpleNamespace(projects=[self.project]), {"vllm": self.sync},
                            runner=self.runner, ssh_executable="fixture-only",
                            clock=lambda: self.now, monotonic=lambda: self.now, **kwargs)
        self.addCleanup(fleet.close)
        return fleet

    def collect(self, fleet):
        fleet.refresh("vllm")
        deadline = time.monotonic() + 3
        while fleet.snapshot("vllm")["refreshing"]:
            if time.monotonic() > deadline:
                self.fail("Fixture did not finish")
            time.sleep(.005)

    def jumpserver(self):
        self.catalog = {"enabled": True, "authenticated": True, "error": None, "generation": 1,
                        "base_url": "https://jump.example.test", "username": "fixture-user",
                        "ssh_host": "", "ssh_port": 2222, "identity_file": "",
                        "default_account": "secure", "task_account": "root",
                        "assets": [{"id": ASSET, "name": "unclassified", "ssh_supported": True,
                                    "is_active": True, "accounts": ["root", "secure"]}]}
        self.inventory = SimpleNamespace(catalog=Mock(side_effect=lambda: copy.deepcopy(self.catalog)))
        self.manager.snapshot.side_effect = lambda **kw: {
            "machine_profiles": copy.deepcopy(list(self.manager._profiles.values())),
            "sessions": [], "backends": sorted(self.manager.backends)}
        self.bridge = JumpServerProfiles(self.inventory)
        self.task_rows = self.bridge.profiles("vllm", self.manager)
        return {"inventory": self.inventory, "profile_source": self.bridge}

    def test_only_collected_current_intersection_is_projected_without_owner_writes(self):
        fleet = self.make()
        self.collect(fleet)
        current = copy.deepcopy(self.rows)
        current[0].update(remote_workspace="/new/workspace", model_paths={"new": "/new/model"})
        current.append({**current[0], "id": "not-collected"})
        before = copy.deepcopy(current)
        self.manager.snapshot.reset_mock()
        calls = self.runner.call_count
        result = fleet.task_devices("vllm", current)
        self.assertEqual(["fixture"], [row["id"] for row in result])
        row = result[0]
        self.assertEqual("metax", row["backend"])
        self.assertEqual("Fixture accelerator", row["hardware"])
        self.assertEqual("/new/workspace", row["remote_workspace"])
        self.assertEqual({"new": "/new/model"}, row["model_paths"])
        self.assertTrue(row["device_observation"]["verified"])
        self.assertFalse(row["device_observation"]["stale"])
        self.assertEqual({"backend", "backend_label", "hardware", "updated_at", "stale", "verified"},
                         set(row["device_observation"]))
        self.assertEqual(before, current)
        row["model_paths"]["new"] = "changed copy"
        self.assertEqual("/new/model", current[0]["model_paths"]["new"])
        self.manager.snapshot.assert_not_called()
        self.assertEqual(calls, self.runner.call_count)
        self.assertEqual({}, self.manager._profiles)
        self.assertEqual([], fleet.task_devices("vllm", []))
        self.assertEqual([], fleet.task_devices("vllm", [{**current[0], "role": "gateway"}]))

    def test_unknown_availability_can_supply_a_fresh_hardware_hint(self):
        fleet = self.make()
        self.collect(fleet)
        self.assertEqual("unknown", fleet.snapshot("vllm")["hosts"][0]["verdict"])
        self.assertEqual("metax", fleet.task_backend("vllm", self.rows[0]))
        self.view["collection_complete"] = False
        self.now += 6
        self.collect(fleet)
        row = fleet.task_devices("vllm", self.rows)[0]
        self.assertEqual("metax", row["backend"])
        self.assertTrue(row["device_observation"]["stale"])
        self.assertFalse(row["device_observation"]["verified"])
        self.assertIsNone(fleet.task_backend("vllm", self.rows[0]))

    def test_empty_probe_does_not_promote_a_name_hint(self):
        self.rows[0].update(backend="cuda", backend_source="SSH 名称推断（待硬件核验）")
        self.view.update(backend=None)
        self.view["accel"].update(family="unknown", total=0, cards=[])
        fleet = self.make()
        self.collect(fleet)
        row = fleet.task_devices("vllm", self.rows)[0]
        self.assertEqual("", row["device_observation"]["backend"])
        self.assertFalse(row["device_observation"]["verified"])
        self.assertIsNone(fleet.task_backend("vllm", self.rows[0]))

    def test_time_expiry_keeps_display_and_prevents_automatic_hint(self):
        fleet = self.make()
        self.collect(fleet)
        self.now += STALE_SECONDS + 1
        row = fleet.task_devices("vllm", self.rows)[0]
        self.assertEqual("metax", row["backend"])
        self.assertTrue(row["device_observation"]["stale"])
        self.assertFalse(row["device_observation"]["verified"])
        self.assertIsNone(fleet.task_backend("vllm", self.rows[0]))

    def test_ssh_alias_endpoint_change_or_removal_cannot_reuse_old_hardware(self):
        fleet = self.make()
        self.collect(fleet)
        for content in ("Host fixture\n  HostName changed.example.test\n", "Host other\n  HostName other.test\n"):
            with self.subTest(content=content):
                self.ssh.write_text(content, encoding="utf-8")
                self.assertEqual([], fleet.task_devices("vllm", self.rows))
                self.assertIsNone(fleet.task_backend("vllm", self.rows[0]))

    def test_managed_connection_change_invalidates_projection(self):
        path = self.owner / "managed.conf"
        path.write_text("Host fixture\n HostName original.test\n", encoding="utf-8")
        self.rows[0].update(source="manual", ssh_connection={"host": "original.test", "user": "worker", "port": 22},
                            ssh_config_path=str(path))
        fleet = self.make()
        self.collect(fleet)
        self.assertEqual("metax", fleet.task_backend("vllm", self.rows[0]))
        path.write_text("Host fixture\n HostName changed.test\n", encoding="utf-8")
        self.assertEqual([], fleet.task_devices("vllm", self.rows))

    def test_jumpserver_secure_monitor_maps_to_root_task_without_copying_connection(self):
        fleet = self.make(**self.jumpserver())
        self.collect(fleet)
        before = copy.deepcopy(self.manager._profiles)
        self.manager.snapshot.reset_mock()
        row = fleet.task_devices("vllm", self.task_rows)[0]
        self.assertIn("#root#", row["ssh_connection"]["user"])
        self.assertEqual(self.task_rows[0]["ssh_config_path"], row["ssh_config_path"])
        self.assertEqual("metax", fleet.task_backend("vllm", self.task_rows[0]))
        self.assertEqual(before, self.manager._profiles)
        # Automatic creation resolves the persisted owner profile, which has no
        # transient JumpServer asset/generation fields from the listing.
        persisted = next(iter(self.manager._profiles.values()))
        self.assertNotIn("jumpserver_asset_id", persisted)
        self.assertEqual("metax", fleet.task_backend("vllm", persisted))
        self.manager.snapshot.assert_not_called()

    def test_unusable_task_account_can_show_hardware_but_cannot_supply_task_hint(self):
        fleet = self.make(**self.jumpserver())
        self.collect(fleet)
        self.catalog["assets"][0]["accounts"] = ["secure"]
        profiles = self.bridge.profiles("vllm", self.manager)
        self.assertTrue(profiles[0]["configuration_error"])
        self.assertEqual("metax", fleet.task_devices("vllm", profiles)[0]["backend"])
        self.assertIsNone(fleet.task_backend("vllm", profiles[0]))
        self.assertIsNone(fleet.task_backend("vllm", self.task_rows[0]))

    def test_jumpserver_scope_asset_and_connection_changes_do_not_reuse_history(self):
        for change in ("username", "base_url", "ssh_host", "removed", "inactive"):
            with self.subTest(change=change):
                fleet = self.make(**self.jumpserver())
                self.collect(fleet)
                if change == "removed":
                    self.catalog["assets"] = []
                elif change == "inactive":
                    self.catalog["assets"][0]["is_active"] = False
                else:
                    self.catalog[change] = {"username": "other-user", "base_url": "https://other.test",
                                            "ssh_host": "changed-gateway.test"}[change]
                # Even without a generation bump, current scope/connection wins.
                self.assertEqual([], fleet.task_devices("vllm", self.task_rows))
                self.assertIsNone(fleet.task_backend("vllm", self.task_rows[0]))
                fleet.close()

    def test_jumpserver_catalog_revision_requires_revalidation(self):
        fleet = self.make(**self.jumpserver())
        self.collect(fleet)
        self.catalog["generation"] += 1
        row = fleet.task_devices("vllm", self.task_rows)[0]
        self.assertEqual("metax", row["backend"])
        self.assertTrue(row["device_observation"]["stale"])
        self.assertFalse(row["device_observation"]["verified"])
        self.assertIsNone(fleet.task_backend("vllm", self.task_rows[0]))

    def test_disk_history_uses_exact_current_connection_but_is_never_verified(self):
        settings = self.jumpserver()
        path = self.root / "cache.json"
        fleet = self.make(**settings, cache_path=path)
        self.collect(fleet)
        fleet.close()
        calls = self.runner.call_count
        restored = self.make(**settings, cache_path=path)
        row = restored.task_devices("vllm", self.task_rows)[0]
        self.assertEqual("metax", row["backend"])
        self.assertTrue(row["device_observation"]["stale"])
        self.assertFalse(row["device_observation"]["verified"])
        self.assertIsNone(restored.task_backend("vllm", self.task_rows[0]))
        self.assertEqual(calls, self.runner.call_count)
        self.catalog["default_account"] = "root"
        self.assertEqual([], restored.task_devices("vllm", self.task_rows))

    def test_disk_history_matches_when_monitor_and_task_both_use_root(self):
        settings = self.jumpserver()
        self.catalog["default_account"] = "root"
        path = self.root / "same-account-cache.json"
        fleet = self.make(**settings, cache_path=path)
        self.collect(fleet)
        self.assertEqual("metax", fleet.task_backend("vllm", self.task_rows[0]))
        fleet.close()
        restored = self.make(**settings, cache_path=path)
        row = restored.task_devices("vllm", self.task_rows)[0]
        self.assertEqual("metax", row["backend"])
        self.assertTrue(row["device_observation"]["stale"])
        self.assertFalse(row["device_observation"]["verified"])
        self.assertIsNone(restored.task_backend("vllm", self.task_rows[0]))

    def test_expired_authorization_keeps_matching_history_without_task_proof(self):
        fleet = self.make(**self.jumpserver())
        self.collect(fleet)
        self.catalog["authenticated"] = False
        profiles = self.bridge.profiles("vllm", self.manager)
        self.assertIsNone(profiles[0]["ssh_connection"])
        row = fleet.task_devices("vllm", profiles)[0]
        self.assertEqual("metax", row["backend"])
        self.assertTrue(row["device_observation"]["stale"])
        self.assertFalse(row["device_observation"]["verified"])
        self.assertIsNone(fleet.task_backend("vllm", profiles[0]))

    def test_project_backend_limits_and_amd_resource_only_are_preserved(self):
        for backend, family, configured in (("rocm", "amd", ["rocm", "metax"]), ("metax", "metax", ["cuda"])):
            with self.subTest(backend=backend):
                self.view = telemetry(backend, family)
                self.project.monitor.config["session_backends"] = configured
                fleet = self.make()
                self.collect(fleet)
                row = fleet.task_devices("vllm", self.rows)[0]
                self.assertEqual(backend, row["backend"])
                self.assertFalse(row["task_supported"])
                # Return unsupported hardware too, so automatic creation rejects
                # it instead of falling back to a stale saved backend.
                self.assertEqual(backend, fleet.task_backend("vllm", self.rows[0]))

    def test_uncollected_or_timestamp_missing_device_is_not_selectable(self):
        fleet = self.make()
        self.assertEqual([], fleet.task_devices("vllm", self.rows))
        self.collect(fleet)
        next(iter(fleet._cache.values()))["updated_at"] = None
        self.assertEqual([], fleet.task_devices("vllm", self.rows))


if __name__ == "__main__":
    unittest.main()
