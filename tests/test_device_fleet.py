import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fl_dashboard.resources.fleet import DeviceFleet, MAX_OUTPUT, MAX_HOSTS, STALE_SECONDS


def view():
    return {"reachable": True, "verdict": "free", "backend": "cuda", "backend_label": "NVIDIA / 英伟达", "error": None,
            "node": {"mem_pct": 12.5}, "accel": {"family": "nvidia", "total": 1, "free": 1, "busy": 0,
            "unknown": 0, "residual": 0, "cards": [{"index": 0, "state": "free", "busy": False, "mem_used": 0}]}}


class FakeSync:
    def __init__(self, path):
        self.path = path
        self.state = {"file_fingerprint": "first-version"}
        self.calls = 0

    def synchronize(self, manager, snapshot):
        self.calls += 1
        return {**snapshot, "ssh_config_sync": {"status": "synced"}}


class DeviceFleetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.owner = self.root / "owner"
        self.owner.mkdir()
        self.config = self.root / "ssh-config"
        self.config.write_text("Host device-demo\n  HostName example.test\n", encoding="utf-8")
        self.profiles = [{"id": "first", "label": "NVIDIA demo", "backend": "cuda", "ssh_alias": "device-demo", "role": "compute"}]
        self.manager = SimpleNamespace(root=self.owner, snapshot=Mock(side_effect=lambda **kwargs: copy.deepcopy({
            "machine_profiles": self.profiles, "sessions": [{"ssh_alias": "device-demo", "status": "running"}]})))
        self.projects = [SimpleNamespace(id="vllm", sessions=self.manager, monitor=SimpleNamespace(config={}))]
        self.sync = FakeSync(self.config)
        self.now = 1000.0
        self.calls = []
        self.parse = patch("fl_dashboard.resources.fleet.parse_telemetry", side_effect=lambda *args: (view(), None)).start()
        self.addCleanup(patch.stopall)
        self.collector = self.make()

    def runner(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"fixture", b"")

    def make(self, runner=None, projects=None, **kwargs):
        projects = projects or self.projects
        fleet = DeviceFleet(SimpleNamespace(projects=projects), {p.id: self.sync for p in projects},
                            runner=runner or self.runner, ssh_executable="ssh-fixture",
                            clock=lambda: self.now, monotonic=lambda: self.now, **kwargs)
        self.addCleanup(fleet.close)
        return fleet

    def wait(self, fleet=None, project="vllm"):
        fleet = fleet or self.collector
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = fleet.snapshot(project)
            if not value["refreshing"]:
                return value
            time.sleep(.005)
        self.fail("Fixture refresh did not finish")

    def test_snapshot_is_cache_only_and_refresh_uses_fixed_strict_ssh(self):
        self.assertEqual([], self.collector.snapshot("vllm")["hosts"])
        self.manager.snapshot.assert_not_called()
        self.assertEqual([], self.calls)
        self.collector.refresh("vllm")
        result = self.wait()
        self.assertEqual(1, self.manager.snapshot.call_count)
        self.assertEqual(1, result["summary"]["free_cards"])
        self.assertEqual(300, result["refresh_interval"])
        self.assertEqual(1, result["hosts"][0]["task_count"])
        argv, kwargs = self.calls[0]
        self.assertEqual(["device-demo", "bash -s"], argv[-2:])
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ClearAllForwardings=yes", "ForwardAgent=no", "ForwardX11=no", "ControlMaster=no", "ControlPath=none", "PermitLocalCommand=no"):
            self.assertIn(option, argv)
        self.assertIn("-T", argv)
        self.assertEqual(str(self.config), argv[argv.index("-F") + 1])
        self.assertIsInstance(kwargs["input"], bytes)
        self.assertEqual(35, kwargs["timeout"])
        self.assertEqual(MAX_OUTPUT, kwargs["output_limit"])
        for _ in range(3):
            self.collector.snapshot("vllm")
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, self.manager.snapshot.call_count)
        self.assertNotIn(str(self.owner), str(result))

    def test_roles_removed_aliases_and_display_backend_sort(self):
        self.profiles += [dict(self.profiles[0], id="gate", ssh_alias="jump-demo", role="gateway"),
                          dict(self.profiles[0], id="dev", ssh_alias="dev-demo", role="development"),
                          dict(self.profiles[0], id="corex", ssh_alias="iluvatar", backend="corex"),
                          dict(self.profiles[0], id="enflame", ssh_alias="enflame", backend="enflame")]
        self.collector.refresh("vllm")
        result = self.wait()
        self.assertEqual(["first", "enflame", "corex"], [x["id"] for x in result["hosts"]])
        self.profiles = self.profiles[1:]
        self.now += 6
        self.collector.refresh("vllm")
        self.assertNotIn("first", [x["id"] for x in self.wait()["hosts"]])

    def test_stale_and_failure_retain_values_without_advertising_free_capacity(self):
        self.collector.refresh("vllm")
        self.wait()
        self.now += STALE_SECONDS + 1
        result = self.collector.snapshot("vllm")
        self.assertTrue(result["hosts"][0]["stale"])
        self.assertEqual(0, result["summary"]["free_cards"])
        self.assertEqual(1, result["summary"]["unknown_cards"])
        self.assertEqual(1, result["hosts"][0]["last_known_accel"]["free"])
        self.collector._runner = Mock(side_effect=subprocess.TimeoutExpired("fixture", 35))
        self.collector.refresh("vllm")
        result = self.wait()
        self.assertEqual("down", result["hosts"][0]["verdict"])
        self.assertEqual(0, result["summary"]["free_cards"])
        self.assertEqual(1, result["summary"]["total_cards"])
        self.assertIn("超时", result["hosts"][0]["error"])

    def test_include_fingerprint_change_does_not_reuse_old_host_data(self):
        self.collector.refresh("vllm")
        self.wait()
        entered, release = threading.Event(), threading.Event()
        def runner(*args, **kwargs):
            entered.set()
            release.wait(3)
            return subprocess.CompletedProcess([], 0, b"fixture", b"")
        self.collector._runner = runner
        self.sync.state["file_fingerprint"] = "changed-included-file"
        self.collector.refresh("vllm")
        self.assertTrue(entered.wait(3))
        during = self.collector.snapshot("vllm")
        self.assertEqual(0, during["summary"]["free_cards"])
        self.assertIsNone(during["hosts"][0]["updated_at"])
        release.set()
        self.assertEqual(1, self.wait()["summary"]["free_cards"])

    def test_saved_manual_connection_uses_its_owner_config_and_rejects_outside_path(self):
        managed = self.owner / "machine.conf"
        managed.write_text("Host manual-demo\n", encoding="utf-8")
        self.profiles[0].update(ssh_connection={"host": "example.test"}, ssh_config_path=str(managed))
        self.collector.refresh("vllm")
        self.wait()
        argv = self.calls[0][0]
        self.assertEqual(1, argv.count("-F"))
        self.assertEqual(str(managed), argv[argv.index("-F") + 1])
        self.profiles[0]["ssh_config_path"] = str(self.config)
        self.collector.refresh("vllm")
        result = self.wait()
        self.assertIsNone(result["error"])
        self.assertIn("已保存连接配置无效", result["hosts"][0]["error"])
        self.assertEqual("unknown", result["hosts"][0]["verdict"])
        self.assertEqual(0, result["summary"]["free_cards"])
        self.assertEqual(1, len(self.calls))

    def test_invalid_managed_profile_is_isolated_from_healthy_devices(self):
        bad = dict(self.profiles[0], id="bad", ssh_alias="bad-demo",
                   ssh_connection={"host": "example.test"}, ssh_config_path=str(self.owner / "missing.conf"))
        self.profiles.append(bad)
        for invalid_path in (self.owner / "missing.conf", self.config):
            with self.subTest(path_kind="missing" if invalid_path.parent == self.owner else "outside-owner"):
                bad["ssh_config_path"] = str(invalid_path)
                self.now += 6
                self.calls.clear()
                self.collector.refresh("vllm")
                result = self.wait()
                self.assertIsNone(result["error"])
                self.assertEqual(1, result["summary"]["free_cards"])
                self.assertEqual(1, len(self.calls))
                self.assertEqual("device-demo", self.calls[0][0][-2])
                broken = next(row for row in result["hosts"] if row["id"] == "bad")
                self.assertEqual("unknown", broken["verdict"])
                self.assertIsNone(broken["reachable"])
                self.assertEqual(0, broken["accel"]["free"])
                self.assertIn("已保存连接配置无效", broken["error"])
                healthy = next(row for row in result["hosts"] if row["id"] == "first")
                self.assertEqual("free", healthy["verdict"])

    def test_missing_import_file_keeps_managed_devices_and_invalidates_alias_cache(self):
        managed = self.owner / "standalone.conf"
        managed.write_text("Host manual-demo\n  HostName example.test\n", encoding="utf-8")
        self.profiles.append(dict(self.profiles[0], id="manual", ssh_alias="manual-demo",
                                  ssh_connection={"host": "example.test"}, ssh_config_path=str(managed)))
        self.collector.refresh("vllm")
        self.assertEqual(2, self.wait()["summary"]["free_cards"])
        self.calls.clear()
        self.config.unlink()
        self.sync.synchronize = lambda manager, snapshot: {**snapshot, "ssh_config_sync": {"status": "missing"}}
        self.collector.refresh("vllm")
        result = self.wait()
        self.assertIsNone(result["error"])
        self.assertEqual(1, result["summary"]["free_cards"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual("manual-demo", self.calls[0][0][-2])
        self.assertEqual(str(managed), self.calls[0][0][self.calls[0][0].index("-F") + 1])
        alias = next(row for row in result["hosts"] if row["id"] == "first")
        self.assertEqual("unknown", alias["verdict"])
        self.assertIsNone(alias["reachable"])
        self.assertEqual(0, alias["accel"]["free"])
        self.assertIn("SSH 配置不可用", alias["error"])
        manual = next(row for row in result["hosts"] if row["id"] == "manual")
        self.assertEqual("free", manual["verdict"])

    def test_inventory_and_parser_errors_do_not_leak_raw_diagnostics(self):
        self.parse.side_effect = ValueError("PRIVATE_DIAGNOSTIC_MARKER")
        self.collector.refresh("vllm")
        result = self.wait()
        self.assertEqual("unknown", result["hosts"][0]["verdict"])
        self.assertEqual(0, result["summary"]["free_cards"])
        self.assertNotIn("PRIVATE_DIAGNOSTIC_MARKER", str(result))
        self.manager.snapshot.side_effect = ValueError("PRIVATE_DIAGNOSTIC_MARKER")
        self.collector.refresh("vllm")
        self.assertNotIn("PRIVATE_DIAGNOSTIC_MARKER", str(self.wait()))

    def test_missing_sessions_unknown_ids_and_output_limit_never_execute_tasks(self):
        for identity in ("-oProxyCommand=x", "a/b", "with space", 1):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.collector.refresh("vllm", identity)
        self.collector.refresh("vllm", "missing")
        self.assertTrue(self.wait()["error"])
        self.assertEqual([], self.calls)
        self.collector._runner = lambda *a, **kw: subprocess.CompletedProcess([], 0, b"x" * (MAX_OUTPUT + 1), b"")
        self.collector.refresh("vllm")
        self.assertEqual(0, self.wait()["summary"]["free_cards"])
        self.projects[0].sessions = None
        self.collector.refresh("vllm")
        self.assertTrue(self.wait()["error"])

    def test_shared_connection_inflight_is_deduplicated_between_projects(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def runner(*args, **kwargs):
            calls.append(1)
            entered.set()
            release.wait(3)
            return subprocess.CompletedProcess([], 0, b"fixture", b"")
        projects = self.projects + [SimpleNamespace(id="sglang", sessions=self.manager, monitor=None)]
        fleet = self.make(runner, projects)
        fleet.refresh("vllm")
        self.assertTrue(entered.wait(3))
        fleet.refresh("sglang")
        deadline = time.monotonic() + 3
        while fleet.snapshot("sglang")["progress"]["total"] == 0 and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(1, len(calls))
        release.set()
        self.assertEqual(1, self.wait(fleet)["progress"]["done"])
        self.assertEqual(1, self.wait(fleet, "sglang")["progress"]["done"])

    def test_global_workers_are_bounded_and_repeated_refresh_does_not_duplicate(self):
        self.profiles = [dict(self.profiles[0], id="host" + str(i), ssh_alias="host" + str(i)) for i in range(8)]
        release, four = threading.Event(), threading.Event()
        guard = threading.Lock()
        counts = {"running": 0, "maximum": 0, "calls": 0}
        def runner(*args, **kwargs):
            with guard:
                counts["running"] += 1
                counts["calls"] += 1
                counts["maximum"] = max(counts["maximum"], counts["running"])
                if counts["running"] == 4:
                    four.set()
            release.wait(3)
            with guard:
                counts["running"] -= 1
            return subprocess.CompletedProcess([], 0, b"fixture", b"")
        fleet = self.make(runner)
        fleet.refresh("vllm")
        self.assertTrue(four.wait(3))
        fleet.refresh("vllm")
        self.assertEqual(4, counts["calls"])
        release.set()
        self.assertEqual(8, self.wait(fleet)["progress"]["done"])
        self.assertEqual(4, counts["maximum"])
        fleet.refresh("vllm")
        self.wait(fleet)
        self.assertEqual(8, counts["calls"])

    def jumpserver_fixture(self):
        catalog = {"enabled": True, "generation": 1, "authenticated": True, "error": None}
        inventory = SimpleNamespace(catalog=Mock(side_effect=lambda: copy.deepcopy(catalog)))
        managed = self.owner / "jumpserver.conf"
        managed.write_text("Host jms-fixture\n  HostName example.test\n", encoding="utf-8")
        profiles = [dict(self.profiles[0], id="jms-fixture", ssh_alias="jms-fixture", source="jumpserver",
                         ssh_connection={"host": "example.test"}, ssh_config_path=str(managed))]
        bridge = SimpleNamespace(profiles=Mock(side_effect=lambda *args: copy.deepcopy(profiles)))
        return catalog, inventory, profiles, bridge

    def test_jumpserver_source_uses_only_managed_bridge_profiles(self):
        catalog, inventory, profiles, bridge = self.jumpserver_fixture()
        profiles.append(dict(profiles[0], id="jms-unusable", ssh_alias="jms-unusable", ssh_connection=None,
                             ssh_config_path=None, configuration_error="未找到可用的授权 SSH 账号"))
        fleet = self.make(inventory=inventory, profile_source=bridge)
        self.config.unlink()  # A local SSH configuration is unnecessary for this source.
        self.assertEqual([], fleet.snapshot("vllm")["hosts"])
        self.manager.snapshot.assert_not_called()
        bridge.profiles.assert_not_called()
        fleet.refresh("vllm")
        value = self.wait(fleet)
        self.assertEqual(0, self.sync.calls)
        self.assertEqual(1, bridge.profiles.call_count)
        self.assertEqual(1, self.manager.snapshot.call_count)
        self.assertEqual(["jms-fixture", "jms-unusable"], sorted(row["id"] for row in value["hosts"]))
        self.assertEqual(1, value["summary"]["free_cards"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(str(self.owner / "jumpserver.conf"), self.calls[0][0][self.calls[0][0].index("-F") + 1])
        unusable = next(row for row in value["hosts"] if row["id"] == "jms-unusable")
        self.assertIn("授权 SSH 账号", unusable["error"])
        self.assertEqual("unknown", unusable["verdict"])

    def test_jumpserver_generation_and_authentication_errors_immediately_invalidate_capacity(self):
        catalog, inventory, profiles, bridge = self.jumpserver_fixture()
        fleet = self.make(inventory=inventory, profile_source=bridge)
        fleet.refresh("vllm")
        self.assertEqual(1, self.wait(fleet)["summary"]["free_cards"])
        before_snapshots = self.manager.snapshot.call_count
        catalog["generation"] += 1
        value = fleet.snapshot("vllm")
        self.assertEqual(before_snapshots, self.manager.snapshot.call_count)
        self.assertEqual(0, value["summary"]["free_cards"])
        self.assertTrue(value["hosts"][0]["stale"])
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def runner(*args, **kwargs):
            entered.set()
            release.wait(3)
            return subprocess.CompletedProcess([], 0, b"fixture", b"")
        fleet._runner = runner
        fleet.refresh("vllm")
        self.assertTrue(entered.wait(2))
        self.assertEqual(0, fleet.snapshot("vllm")["summary"]["free_cards"])
        release.set()
        self.assertEqual(1, self.wait(fleet)["summary"]["free_cards"])
        catalog["error"] = "fixture failure without generation increment"
        value = fleet.snapshot("vllm")
        self.assertEqual(0, value["summary"]["free_cards"])
        self.assertTrue(value["hosts"][0]["stale"])
        fleet._runner = Mock()
        fleet.refresh("vllm")
        value = self.wait(fleet)
        fleet._runner.assert_not_called()
        self.assertTrue(value["error"])
        self.assertEqual(0, value["summary"]["free_cards"])

    def test_ssh_source_excludes_retained_jumpserver_owner_profiles(self):
        catalog, inventory, profiles, bridge = self.jumpserver_fixture()
        self.profiles.extend(profiles)  # Simulate owner records kept for existing tasks.
        catalog["enabled"] = False
        fleet = self.make(inventory=inventory, profile_source=bridge)
        fleet.refresh("vllm")
        value = self.wait(fleet)
        self.assertEqual(["first"], [row["id"] for row in value["hosts"]])
        self.assertEqual(["device-demo"], [call[0][-2] for call in self.calls])
        bridge.profiles.assert_not_called()
        self.assertEqual(2, len(self.profiles))  # Historical owner records stay intact.

    def test_switching_sources_clears_old_rows_and_restores_ssh_path(self):
        catalog, inventory, profiles, bridge = self.jumpserver_fixture()
        fleet = self.make(inventory=inventory, profile_source=bridge)
        fleet.refresh("vllm")
        self.wait(fleet)
        self.profiles.extend(copy.deepcopy(profiles))
        catalog["enabled"] = False
        self.assertEqual([], fleet.snapshot("vllm")["hosts"])
        fleet.refresh("vllm")
        value = self.wait(fleet)
        self.assertEqual(["first"], [row["id"] for row in value["hosts"]])
        self.assertEqual(1, self.sync.calls)
        catalog["enabled"] = True
        self.assertEqual([], fleet.snapshot("vllm")["hosts"])
        fleet.refresh("vllm")
        self.assertEqual(["jms-fixture"], [row["id"] for row in self.wait(fleet)["hosts"]])
        self.assertEqual(1, self.sync.calls)

    def test_source_switch_cancels_old_queue_and_discards_running_old_result(self):
        catalog, inventory, profiles, bridge = self.jumpserver_fixture()
        catalog["enabled"] = False
        self.profiles = [dict(self.profiles[0], id="old" + str(i), ssh_alias="old" + str(i)) for i in range(30)]
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        aliases = []
        def runner(argv, **kwargs):
            aliases.append(argv[-2])
            if argv[-2].startswith("old"):
                entered.set()
                release.wait(3)
            return subprocess.CompletedProcess([], 0, b"fixture", b"")
        fleet = self.make(runner, inventory=inventory, profile_source=bridge, max_workers=1)
        fleet.refresh("vllm")
        self.assertTrue(entered.wait(2))
        old_keys = {row["key"] for row in fleet._states["vllm"]["profiles"]}
        catalog["enabled"] = True
        fleet.refresh("vllm")
        deadline = time.monotonic() + 2
        while not bridge.profiles.called and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(bridge.profiles.called)
        self.assertEqual(0, fleet.snapshot("vllm")["summary"]["free_cards"])
        release.set()
        value = self.wait(fleet)
        self.assertEqual(["old0", "jms-fixture"], aliases)
        self.assertEqual(["jms-fixture"], [row["id"] for row in value["hosts"]])
        self.assertEqual(1, value["summary"]["free_cards"])
        self.assertFalse(old_keys & set(fleet._cache))

    def test_source_generation_changed_during_metadata_cannot_publish_old_profiles(self):
        catalog, inventory, profiles, bridge = self.jumpserver_fixture()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        captured = copy.deepcopy(profiles)
        calls = []
        def prepare(*args):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(3)
                return captured
            return copy.deepcopy(profiles)
        bridge.profiles.side_effect = prepare
        fleet = self.make(inventory=inventory, profile_source=bridge)
        fleet.refresh("vllm")
        self.assertTrue(entered.wait(2))
        catalog["generation"] += 1
        profiles[0].update(id="jms-current", ssh_alias="jms-current")
        fleet.refresh("vllm")
        release.set()
        value = self.wait(fleet)
        self.assertEqual(["jms-current"], [row["id"] for row in value["hosts"]])
        self.assertEqual(["jms-current"], [call[0][-2] for call in self.calls])

    def test_host_limit_rejects_whole_inventory_instead_of_silently_truncating(self):
        self.profiles = [dict(self.profiles[0], id="host" + str(i), ssh_alias="host" + str(i)) for i in range(MAX_HOSTS + 1)]
        self.collector.refresh("vllm")
        value = self.wait()
        self.assertTrue(value["error"])
        self.assertEqual([], value["hosts"])
        self.assertEqual([], self.calls)

    def test_disk_cache_restores_history_without_probing_or_restoring_free_capacity(self):
        cache_path = self.root / "device-cache.json"
        fleet = self.make(cache_path=cache_path)
        telemetry = view()
        telemetry["accel"]["cards"][0]["procs"] = [{"cmdline": "SECRET_COMMAND", "pid": 12}]
        telemetry["node"]["raw_connection"] = "PRIVATE_CONNECTION"
        self.parse.return_value = telemetry, None
        self.parse.side_effect = None
        fleet.refresh("vllm")
        self.assertEqual(1, self.wait(fleet)["summary"]["free_cards"])
        fleet.close()  # Flush the final snapshot without waiting for the batch writer.
        persisted = cache_path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET_COMMAND", persisted)
        self.assertNotIn("PRIVATE_CONNECTION", persisted)
        self.assertNotIn(str(self.owner), persisted)
        self.calls.clear()
        self.manager.snapshot.reset_mock()
        restored = self.make(cache_path=cache_path)
        value = restored.snapshot("vllm")
        self.assertEqual(["first"], [row["id"] for row in value["hosts"]])
        self.assertTrue(value["hosts"][0]["stale"])
        self.assertEqual("unknown", value["hosts"][0]["verdict"])
        self.assertEqual(0, value["summary"]["free_cards"])
        self.assertEqual(1, value["summary"]["total_cards"])
        self.assertEqual(12.5, value["hosts"][0]["node"]["mem_pct"])
        self.manager.snapshot.assert_not_called()
        self.assertEqual([], self.calls)
        restored.refresh("vllm")
        self.assertEqual(1, self.wait(restored)["summary"]["free_cards"])
        self.assertEqual(1, len(self.calls))

    def test_disk_cache_never_supplies_connections_and_matches_selected_source(self):
        cache_path = self.root / "device-cache.json"
        fleet = self.make(cache_path=cache_path)
        fleet.refresh("vllm")
        self.wait(fleet)
        fleet.close()
        catalog, inventory, profiles, bridge = self.jumpserver_fixture()
        other_source = self.make(cache_path=cache_path, inventory=inventory, profile_source=bridge)
        self.assertEqual([], other_source.snapshot("vllm")["hosts"])
        bridge.profiles.assert_not_called()
        self.calls.clear()
        restored = self.make(cache_path=cache_path)
        self.profiles = []
        restored.refresh("vllm")
        self.assertEqual([], self.wait(restored)["hosts"])
        self.assertEqual([], self.calls)

    def test_disk_cache_failures_do_not_break_live_telemetry(self):
        fleet = self.make(cache_path=self.root / "device-cache.json")
        fleet._disk_cache.save = Mock(side_effect=OSError("PRIVATE_STORAGE_ERROR"))
        fleet.refresh("vllm")
        value = self.wait(fleet)
        self.assertEqual(1, value["summary"]["free_cards"])
        self.assertNotIn("PRIVATE_STORAGE_ERROR", str(value))
        fleet.close()

    def test_actual_runner_bounds_stdout_and_blocked_stdin_and_closes_own_process(self):
        # Local fixture processes only: no SSH or external programs execute.
        fleet = self.make()
        with self.assertRaises(ValueError):
            fleet._run_probe([sys.executable, "-c", "import sys;sys.stdout.write('x'*20000)"], input=b"", timeout=3, output_limit=1024)
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            fleet._run_probe([sys.executable, "-c", "import time;time.sleep(5)"], input=b"x" * 2_000_000, timeout=.1, output_limit=1024)
        self.assertLess(time.monotonic() - started, 3)
        errors = []
        def own_process():
            try:
                fleet._run_probe([sys.executable, "-c", "import time;time.sleep(5)"], input=b"", timeout=6, output_limit=1024)
            except Exception as exc:
                errors.append(type(exc))
        worker = threading.Thread(target=own_process)
        worker.start()
        deadline = time.monotonic() + 3
        while not fleet._processes and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(fleet._processes)
        fleet.close()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(set(), fleet._processes)
        with self.assertRaises(ValueError):
            fleet.refresh("vllm")


if __name__ == "__main__":
    unittest.main()
