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
