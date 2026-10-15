import unittest
from branch_monitor import BranchMonitor

class BackendScopeTests(unittest.TestCase):
    def monitor(self):
        monitor = object.__new__(BranchMonitor)
        monitor.backend_ci_platforms = {"corex": "iluvatar"}
        monitor.github_repo = "example/plugin"
        monitor.branch = "dev/test"
        return monitor

    def test_only_target_backend_decides_ci(self):
        monitor = self.monitor()
        jobs = [{"name": name, "status": "completed", "conclusion": result} for name, result in (
            ("test-ascend / unit / Unit (ascend)", "success"),
            ("test-ascend / functional / Functional (ascend)", "success"),
            ("test-ascend / notify / Notify (ascend)", "failure"),
            ("test-musa / unit / Unit (musa)", "failure"),
            ("test-cuda / unit / Unit (cuda)", "cancelled"))]
        self.assertEqual(monitor._ci_status(monitor._ci_test_jobs("ascend", jobs)), "passed")
        self.assertEqual(monitor._ci_status(monitor._ci_test_jobs("corex", jobs)), "unknown")
        self.assertEqual(monitor._ci_status(monitor._ci_test_jobs("musa", jobs)), "failed")

    def test_backend_checks_after_first_page_are_not_lost(self):
        monitor = self.monitor()
        calls = []
        def api(path, force):
            calls.append(path)
            if "page=2" in path:
                return {"check_runs": [{"name": "test-ascend / unit / Unit (ascend)", "status": "completed", "conclusion": "success"}]}
            return {"check_runs": [{"name": "test-cuda / unit", "status": "completed", "conclusion": "failure"}] * 100}
        monitor._api = api
        errors = []
        result = monitor._backend_ci("ascend", {"status": "open"}, [], {}, {"head": {"sha": "a"*40}}, False, errors)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(calls), 2)
        self.assertFalse(errors)

    def test_polling_refreshes_expired_remote_state(self):
        import threading
        import time
        monitor = self.monitor()
        monitor._auto_refresh_lock = threading.Lock()
        monitor._auto_refresh_times = {monitor.branch: time.monotonic()-61}
        calls = []
        monitor.refresh_branch = lambda branch=None: calls.append(branch) or {"fresh": True}
        self.assertEqual(monitor.status(), {"fresh": True})
        self.assertEqual(calls, [None])
        self.assertLess(time.monotonic()-monitor._auto_refresh_times[monitor.branch], 1)
        self.assertFalse(monitor._auto_refresh_lock.locked())

    def test_polling_refresh_failure_releases_lock_and_backs_off(self):
        import threading
        import time
        monitor = self.monitor()
        monitor._auto_refresh_lock = threading.Lock()
        monitor._auto_refresh_times = {}
        def fail(branch=None):
            raise OSError("offline")
        monitor.refresh_branch = fail
        with self.assertRaises(OSError):
            monitor.status()
        self.assertFalse(monitor._auto_refresh_lock.locked())
        self.assertLess(time.monotonic()-monitor._auto_refresh_times[monitor.branch], 1)
