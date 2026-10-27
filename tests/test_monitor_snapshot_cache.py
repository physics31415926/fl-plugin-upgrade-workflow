from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fl_dashboard.monitoring.branch_monitor import MonitorError
from fl_dashboard.monitoring.snapshot_cache import MonitorSnapshotCache


class Monitor:
    branch = "main"
    engine = "example"

    def __init__(self, root):
        self.config = {"resource_state_dir": str(root), "branch": self.branch, "engine": self.engine}
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.error = None
        self.errors = []
        self.observed = "2026-01-01T00:00:00Z"

    def status(self, branch=None):
        self.calls.append(("status", branch))
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test collector was not released")
        if self.error:
            raise self.error
        return {"branch": branch or self.branch, "branches": ["main", "upgrade"],
                "head": "a" * 40, "observed_at": self.observed, "errors": self.errors[:],
                "engine": self.engine, "backends": [{"id": "cuda", "ci": {"status": "passed"}}]}

    def refresh_branch(self, branch=None):
        self.calls.append(("refresh", branch))
        return self.status(branch)


class MonitorSnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.monitor = Monitor(self.root)
        self.caches = []

    def tearDown(self):
        self.monitor.release.set()
        for cache in self.caches:
            cache.close()
        self.temp.cleanup()

    def cache(self, monitor=None, **kwargs):
        cache = MonitorSnapshotCache(monitor or self.monitor, **kwargs)
        self.caches.append(cache)
        return cache

    def complete(self, cache, branch="main"):
        with cache._lock:
            flight = cache._entries[branch].flight
        if flight is not None:
            self.assertTrue(flight.wait(5))
        return cache.get(branch)

    def expire(self, cache, branch="main"):
        with cache._lock:
            entry = cache._entries[branch]
            entry.last_attempt = time.monotonic() - 120
            entry.last_success = time.monotonic() - 120
            entry.last_manual = None

    def test_cold_get_is_immediate_and_only_one_background_collection_runs(self):
        self.monitor.release.clear()
        cache = self.cache()
        started = time.monotonic()
        first = cache.get()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(first["collection"]["has_snapshot"])
        self.assertTrue(first["collection"]["refreshing"])
        self.assertIsNone(first["observed_at"])
        self.assertTrue(self.monitor.entered.wait(2))
        for _ in range(12):
            self.assertFalse(cache.get()["collection"]["has_snapshot"])
        self.assertEqual(self.monitor.calls, [("status", "main")])
        self.monitor.release.set()
        result = self.complete(cache)
        self.assertTrue(result["collection"]["has_snapshot"])
        self.assertFalse(result["collection"]["stale"])

    def test_expired_get_returns_last_good_while_collecting(self):
        cache = self.cache()
        old = cache.refresh()
        self.expire(cache)
        self.monitor.release.clear()
        self.monitor.observed = "2026-01-02T00:00:00Z"
        result = cache.get()
        self.assertEqual(result["observed_at"], old["observed_at"])
        self.assertEqual(result["backends"], old["backends"])
        self.assertTrue(result["collection"]["refreshing"])
        self.assertTrue(result["collection"]["cached"])
        self.monitor.release.set()
        self.assertEqual(self.complete(cache)["observed_at"], self.monitor.observed)

    def test_error_and_partial_result_preserve_whole_last_good_and_timestamp(self):
        cache = self.cache()
        good = cache.refresh()
        persisted = cache.path.read_bytes()
        self.monitor.error = RuntimeError("private credentials must not appear in collection error")
        self.expire(cache)
        cache.get()
        failed = self.complete(cache)
        self.assertEqual(failed["observed_at"], good["observed_at"])
        self.assertEqual(failed["backends"], good["backends"])
        self.assertTrue(failed["collection"]["stale"])
        self.assertNotIn("private", failed["collection"]["error"])
        self.monitor.error = None
        self.monitor.observed = "2026-01-03T00:00:00Z"
        self.monitor.errors = ["fetch failed"]
        self.expire(cache)
        partial = cache.refresh()
        self.assertEqual(partial["observed_at"], good["observed_at"])
        self.assertEqual(partial["errors"], [])
        self.assertEqual(cache.path.read_bytes(), persisted)

    def test_remote_error_has_safe_actionable_reason_and_preserves_cache(self):
        cache = self.cache()
        good = cache.refresh()
        persisted = cache.path.read_bytes()
        for code, expected in (("remote_missing", "监控远端配置已失效"),
                               ("remote_ambiguous", "找到多个匹配的 Git 远端"),
                               ("unrecognized", "状态采集未完成")):
            with self.subTest(code=code):
                self.monitor.error = MonitorError("private token and repository path", code=code)
                self.expire(cache)
                failed = cache.refresh()
                self.assertIn(expected, failed["collection"]["error"])
                self.assertNotIn("private", json.dumps(failed))
                self.assertEqual(failed["backends"], good["backends"])
                self.assertEqual(failed["observed_at"], good["observed_at"])
                self.assertEqual(cache.path.read_bytes(), persisted)
        self.monitor.error = None
        self.expire(cache)
        self.assertIsNone(cache.refresh()["collection"]["error"])

    def test_first_partial_is_visible_but_is_not_persisted_as_good(self):
        self.monitor.errors = ["CI lookup unavailable"]
        cache = self.cache()
        value = cache.refresh()
        self.assertTrue(value["collection"]["has_snapshot"])
        self.assertTrue(value["collection"]["stale"])
        self.assertTrue(value["collection"]["error"])
        self.assertEqual(len(value["backends"]), 1)
        self.assertFalse(cache.path.exists())

    def test_restart_restores_snapshot_and_revalidates_without_waiting(self):
        first = self.cache()
        old = first.refresh()
        first.close()
        self.monitor.release.clear()
        second = self.cache()
        value = second.get()
        self.assertEqual(value["observed_at"], old["observed_at"])
        self.assertTrue(value["collection"]["has_snapshot"])
        self.assertTrue(value["collection"]["refreshing"])
        self.assertTrue(value["collection"]["stale"])
        self.monitor.release.set()
        self.complete(second)

    def test_branch_config_namespace_and_monitor_identity_are_isolated(self):
        first = self.cache()
        first.refresh("main")
        first.refresh("upgrade")
        restarted = self.cache()
        self.assertEqual(set(restarted._entries), {"main", "upgrade"})
        self.assertEqual(restarted._entries["upgrade"].snapshot["branch"], "upgrade")
        changed = Monitor(self.root)
        changed.config["github_repo"] = "example/another"
        other = self.cache(changed)
        public = self.cache(namespace="public")
        self.assertNotEqual(first.path, other.path)
        self.assertNotEqual(first.path, public.path)
        self.assertFalse(other._entries)
        self.assertFalse(public._entries)
        self.assertNotIn("resource_state_dir", first.path.read_text(encoding="utf-8"))

    def test_manual_refresh_joins_current_collection_and_has_cooldown(self):
        self.monitor.release.clear()
        cache = self.cache()
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(cache.refresh)
            self.assertTrue(self.monitor.entered.wait(2))
            second = pool.submit(cache.refresh)
            third = pool.submit(cache.refresh)
            self.monitor.release.set()
            for future in (first, second, third):
                self.assertTrue(future.result(3)["collection"]["has_snapshot"])
        cache.refresh()
        self.assertEqual(sum(kind == "refresh" for kind, _ in self.monitor.calls), 1)

    def test_transform_is_applied_before_persistence_and_not_on_restore(self):
        calls = []
        def transform(snapshot):
            calls.append(1)
            snapshot["backends"][0]["acceptance_tables"] = [{"rows": [["case", "passed"]]}]
            return snapshot
        first = self.cache(namespace="public", transform=transform)
        old = first.refresh()
        self.assertEqual(calls, [1])
        second = self.cache(namespace="public", transform=lambda value: self.fail("load transformed twice"))
        self.assertEqual(second._entries["main"].snapshot["backends"], old["backends"])

    def test_corrupt_cache_and_unwritable_cache_do_not_hide_collection(self):
        first = self.cache()
        first.path.parent.mkdir(parents=True)
        for body in ("{", "null", '{"version":999}', '{"version":1,"snapshots":[]}'):
            first.path.write_text(body, encoding="utf-8")
            self.assertFalse(self.cache()._entries)
        with patch("fl_dashboard.monitoring.snapshot_cache.os.replace", side_effect=OSError("disk unavailable")):
            self.assertTrue(first.refresh()["collection"]["has_snapshot"])
        self.assertEqual(list(first.path.parent.glob("*.tmp")), [])

    def test_invalid_snapshot_cache_schema_and_nan_are_discarded(self):
        cache = self.cache()
        cache.path.parent.mkdir(parents=True)
        good = self.monitor.status("main")
        for snapshot in ({}, {**good, "backends": None}, {**good, "branches": None},
                         {**good, "branch": "other"}, {**good, "head": 42},
                         {**good, "observed_at": float("nan")}):
            with self.subTest(snapshot=snapshot):
                cache.path.write_text(json.dumps({"version": 1, "fingerprint": cache.fingerprint,
                                                "snapshots": {"main": snapshot}}), encoding="utf-8")
                self.assertFalse(self.cache()._entries)

    def test_size_limit_branch_limit_and_invalid_branch_are_bounded(self):
        cache = self.cache(refresh_interval=0)
        cache.refresh()
        with patch("fl_dashboard.monitoring.snapshot_cache.MAX_CACHE_BYTES", 10):
            self.assertFalse(self.cache()._entries)
        for branch in ("../secret", "-option", "bad//ref", "", ["main"]):
            with self.assertRaises(ValueError):
                cache.get(branch)
        for number in range(11):
            cache.refresh("branch-" + str(number))
        with self.assertRaisesRegex(ValueError, "limit"):
            cache.get("thirteenth")
        self.assertEqual(cache.get("main")["branch"], "main")

    def test_returned_values_cannot_mutate_cached_snapshot(self):
        cache = self.cache()
        value = cache.refresh()
        value["backends"][0]["ci"]["status"] = "failed"
        value["collection"]["error"] = "changed"
        self.assertEqual(cache.get()["backends"][0]["ci"]["status"], "passed")
        self.assertIsNone(cache.get()["collection"]["error"])


if __name__ == "__main__":
    unittest.main()
