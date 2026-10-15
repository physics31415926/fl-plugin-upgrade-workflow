import copy
import subprocess
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from fl_dashboard.agents.codex_sessions import CodexSessionManager, SessionError
import test_codex_sessions as fixtures

FakeClient = fixtures.FakeClient


class ProjectSessionsTest(unittest.TestCase):
    git = fixtures.SessionsTest.git
    create = fixtures.SessionsTest.create
    start = fixtures.SessionsTest.start

    def setUp(self):
        fixtures.SessionsTest.setUp(self)
        self.second_repo = self.base / "sglang-plugin"
        subprocess.check_output(["git", "clone", "--quiet", str(self.repo), str(self.second_repo)],
                                stderr=subprocess.STDOUT)
        self.second = CodexSessionManager({**self.config, "repo": str(self.second_repo), "engine": "sglang",
            "sessions_dir": "runs/sglang-sessions", "evidence_dir": "runs/sglang-evidence"}, self.base, FakeClient)
        lease_lock = threading.RLock()
        self.managers = [self.manager, self.second]
        for manager in self.managers:
            manager.machine_lock = lease_lock
            manager.peer_managers = self.managers

    def tearDown(self):
        self.second.close()
        for worktree in (self.second.root / "worktrees").glob("*"):
            subprocess.check_output(["git", "-C", str(self.second_repo), "worktree", "remove", "--force", str(worktree)],
                                    stderr=subprocess.STDOUT)
        fixtures.SessionsTest.tearDown(self)

    def test_snapshot_exposes_engine_label_and_keeps_legacy_default(self):
        old = self.manager.snapshot()
        new = self.second.snapshot()
        self.assertEqual((old["engine"], old["project_label"], old["default_branch"]),
                         ("vllm", "vllm-plugin-FL", "0.4.0-dev"))
        self.assertEqual((new["engine"], new["project_label"], new["default_branch"]),
                         ("sglang", "sglang-plugin-FL", self.config["branch"]))
        self.assertEqual(new["case_sources"], ["examples", "tests/e2e_tests/concurrent"])
        custom = CodexSessionManager({**self.config, "engine": "sglang", "project_label": "SGLang progress",
            "sessions_dir": "runs/custom-sessions"}, self.base, FakeClient)
        try:
            self.assertEqual(custom.snapshot()["project_label"], "SGLang progress")
        finally:
            custom.close()

    def test_sglang_prompt_uses_native_examples_concurrency_and_real_ci(self):
        session = self.second.create(self.payload)
        prompt = session["prompt"]
        for source in ("sglang-plugin-FL", "SGLang", "examples/", "tests/e2e_tests/concurrent",
                       "tests/models/", "tests/platforms", "tests/run.py", ".github/workflows",
                       "text/vl/mixed", "CONCURRENT_N", "expected", ".flflow-progress.json",
                       "evidence_paths", "tmux", "Git blob raw bytes"):
            self.assertIn(source, prompt)
        self.assertNotIn("vllm-upgrade-flagos", prompt)
        self.assertNotIn("vllm-plugin-fl-setup-flagos", prompt)
        self.assertNotIn("tools/adaptation-gate-cases", prompt)
        self.assertNotIn('"port": 8000', prompt)
        self.assertIn('"engine": "sglang"', prompt)
        self.assertIn("evidence_type=sglang_raw_requests", prompt)
        self.assertIn("source_blob", prompt)
        self.assertIn("definition_files", prompt)
        self.assertIn("sglang_evidence.py", prompt)
        self.assertIn('"--scenario", "all"', prompt)
        self.assertIn('"--commit", "<actual-tested-40-hex-commit>"', prompt)
        self.assertIn("<model>/<mode>/<run_id>/manifest.json", prompt)
        self.assertNotIn("Qwen3.6-35B-A3B", prompt)
        self.assertIn("未显式配置的模型和模式以所选分支原生 case/YAML 为准", prompt)
        self.assertIn("不能证明真实模型验收通过", prompt)

    def test_vllm_missing_gate_reports_real_test_evidence_without_inventing_gate(self):
        prompt = self.create()["prompt"]
        self.assertIn("tools/adaptation-gate-cases", prompt)
        self.assertIn("未配置 Gate", prompt)
        self.assertIn("不得制造", prompt)
        self.assertIn(".github/workflows", prompt)
        self.assertIn("vllm-upgrade-flagos", prompt)
        self.assertNotIn('"port": 8000', prompt)

    def test_vllm_configured_gate_directory_controls_prompt_archive_source(self):
        path = self.repo / "acceptance/cases"
        path.mkdir(parents=True)
        (path / "scenario.py").write_text("# actual case\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "Configure acceptance cases")
        self.manager.case_dir = "acceptance/cases"
        prompt = self.create()["prompt"]
        self.assertIn("从验证机器原样复制 acceptance/cases", prompt)
        self.assertIn("summary.passed", prompt)
        self.assertIn('"port": 8000', prompt)
        self.assertNotIn("tools/adaptation-gate-cases", prompt)

    def test_prepared_machine_is_reserved_across_repositories(self):
        first = self.create()
        before = copy.deepcopy(self.manager._sessions[first["id"]])
        with self.assertRaisesRegex(SessionError, "machine already"):
            self.second.create(self.payload)
        self.assertEqual(self.second.snapshot()["sessions"], [])
        self.assertEqual(self.manager._sessions[first["id"]], before)
        self.assertEqual(list((self.second.root / "worktrees").glob("*")), [])
        self.manager.interrupt(first["id"])
        second = self.second.create(self.payload)
        self.assertEqual(second["status"], "prepared")
        self.assertNotEqual(first["worktree"], second["worktree"])

    def test_active_peer_cannot_be_bypassed_and_rejected_resume_preserves_state(self):
        first = self.create()
        client = self.start(first)
        with self.assertRaisesRegex(SessionError, "machine already"):
            self.second.create(self.payload)
        client.callback({"method": "turn/completed", "params": {
            "threadId": "thread-123", "turn": {"id": "turn-1", "status": "completed"}}})
        second = self.second.create(self.payload)
        before = copy.deepcopy(self.manager._sessions[first["id"]])
        with self.assertRaisesRegex(SessionError, "reserved"):
            self.manager.start(first["id"])
        self.assertEqual(self.manager._sessions[first["id"]], before)
        self.assertEqual(self.second._copy(second["id"])["status"], "prepared")
        self.second.interrupt(second["id"])
        self.start(first)
        self.assertEqual(self.manager._copy(first["id"])["status"], "running")
        self.manager.interrupt(first["id"])

    def test_direct_host_identity_is_reserved_even_with_different_aliases(self):
        profiles = []
        for manager, alias in zip(self.managers, ("first_gpu", "second_gpu")):
            profiles.append(manager.save_profile({**self.payload, "ssh_alias": alias,
                "ssh_connection": {"host": "GPU.EXAMPLE.TEST" if manager is self.manager else "gpu.example.test",
                                   "user": "compute", "port": 2222}}))
        success = {"ok": True, "checks": [], "error": None}
        with patch.object(self.manager, "_check_connection", return_value=success), \
                patch.object(self.second, "_check_connection", return_value=success):
            self.manager.create({"profile_id": profiles[0]["id"]})
            with self.assertRaisesRegex(SessionError, "machine already"):
                self.second.create({"profile_id": profiles[1]["id"]})
        self.assertEqual(self.second.snapshot()["sessions"], [])

    def test_different_machines_run_independently_with_separate_evidence(self):
        first = self.create()
        second = self.second.create({**self.payload, "ssh_alias": "other_gpu"})
        self.start(first)
        self.second.start(second["id"])
        self.second._workers[second["id"]].join(5)
        self.assertEqual(self.manager._copy(first["id"])["status"], "running")
        self.assertEqual(self.second._copy(second["id"])["status"], "running")
        self.assertNotEqual(self.manager.evidence_dir, self.second.evidence_dir)
        self.manager.interrupt(first["id"])
        self.second.interrupt(second["id"])

    def test_live_remote_allocations_override_stale_proxy_session_cache(self):
        peer = SimpleNamespace(_sessions={}, allocation_sessions=lambda: [
            {"id": "a" * 32, "status": "waiting_approval", "ssh_alias": self.payload["ssh_alias"],
             "ssh_connection": None}])
        self.manager.peer_managers = [self.manager, peer]
        with self.assertRaisesRegex(SessionError, "machine already"):
            self.create()
        self.assertEqual(self.manager.snapshot()["sessions"], [])

    def test_unavailable_remote_dashboard_blocks_new_machine_dispatch(self):
        def unavailable():
            raise SessionError("Existing dashboard is unavailable")

        self.manager.peer_managers = [self.manager, SimpleNamespace(allocation_sessions=unavailable)]
        with self.assertRaisesRegex(SessionError, "dashboard is unavailable"):
            self.create()
        self.assertEqual(self.manager.snapshot()["sessions"], [])

    def test_unavailable_remote_dashboard_does_not_change_resume_state(self):
        first = self.create()
        self.manager.interrupt(first["id"])
        before = copy.deepcopy(self.manager._sessions[first["id"]])

        def unavailable():
            raise SessionError("Existing dashboard is unavailable")

        self.manager.peer_managers = [self.manager, SimpleNamespace(allocation_sessions=unavailable)]
        with self.assertRaisesRegex(SessionError, "dashboard is unavailable"):
            self.manager.start(first["id"])
        self.assertEqual(self.manager._sessions[first["id"]], before)

    def test_simultaneous_cross_repo_preparation_creates_only_one_machine_lease(self):
        barrier = threading.Barrier(2)
        results = []

        def synchronized(original):
            def git(*args):
                if args[0] == "check-ref-format":
                    barrier.wait(timeout=5)
                return original(*args)
            return git

        def prepare(manager):
            try:
                results.append(manager.create(self.payload))
            except SessionError as exc:
                results.append(exc)

        with patch.object(self.manager, "_git", side_effect=synchronized(self.manager._git)), \
                patch.object(self.second, "_git", side_effect=synchronized(self.second._git)):
            workers = [threading.Thread(target=prepare, args=(manager,)) for manager in self.managers]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
                self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(isinstance(result, SessionError) for result in results), 1)
        self.assertEqual(sum(len(manager.snapshot()["sessions"]) for manager in self.managers), 1)

    def test_invalid_engine_and_case_path_cannot_change_prompt_target(self):
        for engine in ("other", [], None):
            with self.subTest(engine=engine), self.assertRaises(SessionError):
                CodexSessionManager({**self.config, "engine": engine}, self.base, FakeClient)
        for case_dir in ("../cases", "/cases", "C:\\cases", "cases//other", "cases/./other", None):
            with self.subTest(case_dir=case_dir), self.assertRaises(SessionError):
                CodexSessionManager({**self.config, "case_dir": case_dir}, self.base, FakeClient)
        for sources in ([], "examples", ["../examples"], [None], ["examples//cases"]):
            with self.subTest(sources=sources), self.assertRaises(SessionError):
                CodexSessionManager({**self.config, "case_sources": sources}, self.base, FakeClient)


if __name__ == "__main__":
    unittest.main()
