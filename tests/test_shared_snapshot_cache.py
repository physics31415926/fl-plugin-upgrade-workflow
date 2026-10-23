"""Public snapshot cache contract: immediate reads and safe restart recovery."""
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from fl_dashboard.projects.project_registry import ProjectContext, ProjectRegistry
from fl_dashboard.services.share_server import ProjectSnapshotCache, SnapshotCache, make_server
from test_share_server import source_snapshot, wait_for_snapshot


class SnapshotMonitor:
    branch = "0.4.0-dev"
    engine = "vllm"

    def __init__(self, runtime, marker="a"):
        self.config = {"resource_state_dir": str(runtime)}
        self.marker = marker
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail = False

    def branch_options(self):
        return {"branches": [self.branch, "main"]}

    def status(self, branch=None):
        self.calls.append(branch)
        self.started.set()
        if not self.release.wait(3):
            raise RuntimeError("test collection was not released")
        if self.fail:
            raise RuntimeError("explicit_dummy_private_error C:/Users/private-owner 192.168.1.20")
        value = source_snapshot()
        value.update(branch=branch or self.branch, errors=[], head=self.marker * 40)
        value["backends"][0]["acceptance"] = {
            "status": "accepted", "source": "pr_description",
            "description_blocks": [
                {"type": "heading", "text": "Validation results"},
                {"type": "table", "rows": [["Case", "Result"], ["text single", "passed"]]}],
            "raw_body": "explicit_dummy_private_pr_body",
            "provenance": {"path": "C:/Users/private-owner/evidence"}}
        return value

    refresh_branch = status


class SharedSnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def cache(self, monitor, **kwargs):
        cache = SnapshotCache(monitor, refresh_interval=0, **kwargs)
        self.addCleanup(cache.close)
        return cache

    def test_cold_nondefault_branch_http_read_returns_while_collection_waits(self):
        monitor = SnapshotMonitor(self.root)
        monitor.release.clear()
        cache = self.cache(monitor)
        self.addCleanup(monitor.release.set)
        self.assertEqual(monitor.calls, [])
        index = self.root / "index.html"
        index.write_text("<html>test</html>", encoding="utf-8")
        server = make_server(cache, "127.0.0.1", 0, index_path=index)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=1)
            connection.request("GET", "/api/branch-status?branch=main")
            response = connection.getresponse()
            value = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertTrue(monitor.started.wait(1))
            self.assertFalse(monitor.release.is_set())
            self.assertEqual(value["branch"], "main")
            self.assertIn("main", value["branches"])
            self.assertTrue(value["read_only"])
            self.assertIsNone(value["head"])
            self.assertFalse(value["collection"]["has_snapshot"])
            self.assertTrue(value["collection"]["refreshing"])
            self.assertNotIn("config", value)
            monitor.release.set()
            self.assertEqual(wait_for_snapshot(cache, "main")["head"], "a" * 40)
        finally:
            monitor.release.set()
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_disk_restore_preserves_public_tables_and_error_keeps_last_good_observation(self):
        first = self.cache(SnapshotMonitor(self.root))
        original = wait_for_snapshot(first)
        tables = original["backends"][0]["acceptance"]["acceptance_tables"]
        self.assertTrue(tables)
        first.close()
        files = list(self.root.rglob("*.json"))
        self.assertEqual(len(files), 1)
        persisted = files[0].read_text(encoding="utf-8")
        for private in ("source_thread", "private-owner", "private_pr_body", "provenance", "192.168.1.20"):
            self.assertNotIn(private, persisted)
        monitor = SnapshotMonitor(self.root)
        monitor.release.clear()
        monitor.fail = True
        restored = self.cache(monitor)
        self.addCleanup(monitor.release.set)
        ready = restored.get()
        self.assertTrue(ready["collection"]["cached"])
        self.assertTrue(ready["collection"]["has_snapshot"])
        self.assertEqual(ready["observed_at"], original["observed_at"])
        self.assertEqual(ready["backends"][0]["acceptance"]["acceptance_tables"], tables)
        monitor.release.set()
        failed = wait_for_snapshot(restored)
        self.assertTrue(failed["collection"]["error"])
        self.assertTrue(failed["collection"]["stale"])
        self.assertEqual(failed["observed_at"], original["observed_at"])
        self.assertEqual(failed["backends"], original["backends"])
        self.assertNotIn("explicit_dummy_private_error", json.dumps(failed))
        self.assertNotIn("private-owner", json.dumps(failed))

    def test_project_namespaces_keep_same_monitor_configuration_separate(self):
        for project_id, marker in (("vllm", "a"), ("sglang", "b")):
            seeded = self.cache(SnapshotMonitor(self.root, marker), namespace="public-" + project_id)
            wait_for_snapshot(seeded)
            seeded.close()
        monitors = [SnapshotMonitor(self.root, "c"), SnapshotMonitor(self.root, "c")]
        projects = []
        for project_id, monitor in zip(("vllm", "sglang"), monitors):
            monitor.release.clear()
            projects.append(ProjectContext(project_id, project_id, monitor=monitor))
        cache = ProjectSnapshotCache(ProjectRegistry(projects), refresh_interval=0)
        self.addCleanup(cache.close)
        for monitor in monitors:
            self.addCleanup(monitor.release.set)
        rows = {row["id"]: row["status"] for row in cache.statuses()["projects"]}
        self.assertEqual(rows["vllm"]["head"], "a" * 40)
        self.assertEqual(rows["sglang"]["head"], "b" * 40)
        self.assertEqual(len(list(self.root.rglob("*.json"))), 2)

    def test_allowed_branches_and_twelve_branch_limit_are_enforced(self):
        monitor = SnapshotMonitor(self.root)
        monitor.branch_options = lambda: {"branches": ["test" + str(n) for n in range(13)]}
        cache = self.cache(monitor)
        with self.assertRaisesRegex(ValueError, "not configured"):
            cache.get("unconfigured")
        for number in range(12):
            cache.get("test" + str(number))
        with self.assertRaisesRegex(ValueError, "limit"):
            cache.get("test12")
        with self.assertRaises(ValueError):
            cache.refresh({"branch": "main"})


if __name__ == "__main__":
    unittest.main()
