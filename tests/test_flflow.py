"""Local contract tests. They create disposable Git repositories; no GPU is used."""

from __future__ import annotations

import importlib.util
from http.client import HTTPConnection
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "flflow.py"
SPEC = importlib.util.spec_from_file_location("flflow", MODULE_PATH)
assert SPEC and SPEC.loader
flflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flflow)

TEST_CODE = r"""
import json, os, sys
from pathlib import Path
skipped = int(sys.argv[2])
record = {
    'platform': os.environ['FLFLOW_PLATFORM'],
    'scope': os.environ['FLFLOW_SCOPE'],
    'executed': 1,
    'passed': 0 if skipped else 1,
    'failed': 0,
    'skipped': skipped,
    'target_vllm_commit': os.environ['FLFLOW_TARGET_VLLM_COMMIT'],
    'plugin_commit': os.environ['FLFLOW_PLUGIN_COMMIT'],
    'status': 'failed' if skipped else 'passed',
    'issues': ['skipped'] if skipped else [],
}
record['cases'] = [{
    'executed': record['executed'],
    'passed': record['passed'],
    'failed': record['failed'],
    'skipped': record['skipped'],
    'errors': 0,
    'exit_code': 0,
    'issues': record['issues'],
}]
Path(sys.argv[1]).write_text(json.dumps(record), encoding='utf-8')
"""


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)
    return proc.stdout.strip()


class FlflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "plugin"
        self.upstream = self.root / "upstream"
        for path in (self.repo, self.upstream):
            path.mkdir()
            git(path, "init")
            git(path, "config", "user.name", "Test")
            git(path, "config", "user.email", "test@example.invalid")
            (path / "README.md").write_text("baseline\n", encoding="utf-8")
            git(path, "add", "README.md")
            git(path, "commit", "-m", "baseline")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        self.target_sha = git(self.upstream, "rev-parse", "HEAD")
        self.config_path = self.root / "config.json"

    def config(self, *, skipped: int = 0, backend_change: bool = False) -> None:
        pass_command = [sys.executable, "-c", "pass"]
        backend_adapt = [sys.executable, "-c", "from pathlib import Path; Path('backend.txt').write_text('change')"] if backend_change else pass_command
        def tests():
            return [{
                "scope": "smoke",
                "argv": [sys.executable, "-c", TEST_CODE, "{evidence_file}", str(skipped)],
                "evidence_file": "evidence/{platform}/{scope}-{attempt}.json",
            }]
        value = {
            "repo": str(self.repo), "upstream_repo": str(self.upstream),
            "work_dir": str(self.root / "work"), "plugin_base_sha": self.base_sha,
            "target_vllm_ref": self.target_sha, "target_vllm_commit": self.target_sha,
            "platforms": [
                {"name": "nvidia", "kind": "cuda", "adapt_command": pass_command, "test_commands": tests()},
                {"name": "metax", "kind": "backend", "adapt_command": backend_adapt, "test_commands": tests()},
            ],
        }
        self.config_path.write_text(json.dumps(value), encoding="utf-8")

    def test_pass_requires_real_evidence_and_shared_commit(self) -> None:
        self.config()
        state = flflow.Engine(self.config_path).start()
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["candidate_commit"], self.base_sha)
        for platform in state["platforms"].values():
            self.assertEqual(platform["status"], "passed")
            evidence = platform["attempts"][0]["tests"]["smoke"]["evidence"]
            self.assertEqual(evidence["executed"], 1)
            self.assertEqual(evidence["skipped"], 0)

    def test_skipped_test_is_rejected(self) -> None:
        self.config(skipped=1)
        state = flflow.Engine(self.config_path).start()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["platforms"]["nvidia"]["status"], "failed")
        self.assertEqual(state["platforms"]["metax"]["status"], "blocked")

    def test_backend_change_requires_fanin(self) -> None:
        self.config(backend_change=True)
        state = flflow.Engine(self.config_path).start()
        self.assertEqual(state["status"], "ready_for_integration")
        self.assertTrue(state["integration_required"])
        self.assertIsNone(state["candidate_commit"])
        self.assertEqual(state["platforms"]["metax"]["status"], "passed")

    def test_dry_run_cannot_pass(self) -> None:
        self.config()
        state = flflow.Engine(self.config_path).start(dry_run=True)
        self.assertEqual(state["status"], "simulated")
        self.assertFalse((self.root / "work" / "worktrees").exists())

    def test_preflight_mismatch_blocks(self) -> None:
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["plugin_base_sha"] = "0" * 40
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        state = flflow.Engine(self.config_path).start()
        self.assertEqual(state["status"], "blocked")
        self.assertIn("differs", state["error"])

    def test_missing_command_fails_validation(self) -> None:
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        del config["platforms"][0]["adapt_command"]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(flflow.WorkflowError):
            flflow.Engine(self.config_path)

    def test_zero_exit_without_evidence_is_rejected(self) -> None:
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["platforms"][0]["test_commands"][0]["argv"] = [sys.executable, "-c", "pass"]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        state = flflow.Engine(self.config_path).start()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["platforms"]["nvidia"]["status"], "failed")

    def test_retry_failed_backend_uses_new_worktree(self) -> None:
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        marker = self.root / "backend-skipped.txt"
        marker.write_text("1", encoding="utf-8")
        code = TEST_CODE.replace("skipped = int(sys.argv[2])", "skipped = int(Path(sys.argv[2]).read_text())")
        config["platforms"][1]["test_commands"][0]["argv"][2] = code
        config["platforms"][1]["test_commands"][0]["argv"][-1] = str(marker)
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(flflow.Engine(self.config_path).start()["status"], "failed")
        marker.write_text("0", encoding="utf-8")
        self.config_path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
        state = flflow.Engine(self.config_path).retry_failed()
        self.assertEqual(state["status"], "passed")
        attempts = state["platforms"]["metax"]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0]["worktree"], attempts[1]["worktree"])

    def test_retry_rejects_new_cuda_scope_after_backend_failure(self) -> None:
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["platforms"][1]["test_commands"][0]["argv"][-1] = "1"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        first = flflow.Engine(self.config_path).start()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["platforms"]["nvidia"]["status"], "passed")

        added_scope = dict(config["platforms"][0]["test_commands"][0])
        added_scope["scope"] = "functional"
        added_scope["evidence_file"] = "evidence/{platform}/{scope}-{attempt}.json"
        config["platforms"][0]["test_commands"].append(added_scope)
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(flflow.WorkflowError, "Config changed"):
            flflow.Engine(self.config_path).retry_failed()
        persisted = flflow.Engine(self.config_path).status()
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(len(persisted["platforms"]["nvidia"]["attempts"]), 1)
        self.assertEqual(len(persisted["platforms"]["metax"]["attempts"]), 1)

    def test_retry_rejects_changed_acceptance_tool_digest(self) -> None:
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["platforms"][1]["test_commands"][0]["argv"][-1] = "1"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        engine = flflow.Engine(self.config_path)
        self.assertEqual(engine.start()["status"], "failed")
        state_path = self.root / "work" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["protected_tool_sha256"] = {"strict_test.py": "0" * 64}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(flflow.WorkflowError, "Acceptance tools changed"):
            flflow.Engine(self.config_path).retry_failed()

    def test_adapt_cannot_commit_tracked_acceptance_test_change(self) -> None:
        tests_dir = self.repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "run.py").write_text("original\n", encoding="utf-8")
        git(self.repo, "add", "tests/run.py")
        git(self.repo, "commit", "-m", "add acceptance test")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["platforms"][0]["adapt_command"] = [
            sys.executable, "-c", "from pathlib import Path; Path('tests/run.py').write_text('weakened')",
        ]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        state = flflow.Engine(self.config_path).start()
        attempt = state["platforms"]["nvidia"]["attempts"][0]
        self.assertEqual(state["status"], "failed")
        self.assertIn("tests/run.py", attempt["error"])
        self.assertIsNone(attempt["plugin_commit"])

    def test_adapt_cannot_add_untracked_acceptance_test(self) -> None:
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["platforms"][0]["adapt_command"] = [
            sys.executable, "-c", "from pathlib import Path; Path('tests').mkdir(); Path('tests/test_new.py').write_text('pass')",
        ]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        state = flflow.Engine(self.config_path).start()
        attempt = state["platforms"]["nvidia"]["attempts"][0]
        self.assertEqual(state["status"], "failed")
        self.assertIn("tests/test_new.py", attempt["error"])
        self.assertIsNone(attempt["plugin_commit"])

    def test_adapt_cannot_change_ci_platform_config(self) -> None:
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["platforms"][0]["adapt_command"] = [
            sys.executable, "-c",
            "from pathlib import Path; Path('.github/configs').mkdir(parents=True); Path('.github/configs/platforms.yml').write_text('cuda: false')",
        ]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        state = flflow.Engine(self.config_path).start()
        self.assertEqual(state["status"], "failed")
        self.assertIn(".github/configs/platforms.yml", state["platforms"]["nvidia"]["attempts"][0]["error"])

    def test_http_rejects_rebound_host_and_cross_origin_post(self) -> None:
        self.config()
        engine = flflow.Engine(self.config_path)
        server = flflow.make_server(engine, "127.0.0.1", 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        port = server.server_port

        def request(method: str, headers: dict[str, str] | None = None) -> int:
            conn = HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                conn.request(method, "/api/start" if method == "POST" else "/api/status",
                             body="{}" if method == "POST" else None, headers=headers or {})
                response = conn.getresponse()
                response.read()
                return response.status
            finally:
                conn.close()

        try:
            self.assertEqual(request("GET"), 200)
            self.assertEqual(request("GET", {"Host": f"evil.example:{port}"}), 403)
            self.assertEqual(request("POST", {
                "Content-Type": "application/json", "Origin": "http://evil.example",
            }), 403)
            self.assertEqual(request("POST", {
                "Content-Type": "application/json", "Host": f"evil.example:{port}",
            }), 403)
            self.assertEqual(engine.status()["status"], "idle")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_branch_monitor_uses_separate_read_and_refresh_routes(self) -> None:
        self.config()

        class FakeMonitor:
            def status(self, branch=None):
                return {"branch": branch or "0.4.0-dev", "head": "a" * 40}

            def refresh_branch(self, branch=None):
                return {"branch": branch or "0.4.0-dev", "refreshed": True}

            def read_evidence(self, branch, backend, model, mode, scenario):
                return {"branch": branch, "backend": backend, "model": model,
                        "mode": mode, "scenario": scenario, "summary": {"passed": True}}

        server = flflow.make_server(flflow.Engine(self.config_path), "127.0.0.1", 0, FakeMonitor())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        port = server.server_port
        try:
            connection = HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/api/branch-status?branch=0.4.0-dev")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["head"], "a" * 40)
            connection.close()

            connection = HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/api/branch-evidence?branch=0.4.0-dev&backend=cuda&model=Qwen3.6-27B&mode=eager&scenario=text_single")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["scenario"], "text_single")
            connection.close()

            connection = HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("POST", "/api/branch-refresh", body='{"branch":"0.4.0-dev"}',
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["refreshed"])
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_one_unexecuted_case_rejects_positive_aggregate(self) -> None:
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        code = TEST_CODE.replace(
            "Path(sys.argv[1]).write_text",
            "record['cases'].append({'executed':0,'passed':0,'failed':0,'skipped':0,'issues':['missing JUnit']})\nPath(sys.argv[1]).write_text",
        )
        config["platforms"][0]["test_commands"][0]["argv"][2] = code
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        state = flflow.Engine(self.config_path).start()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["platforms"]["nvidia"]["status"], "failed")

    def test_pause_waits_for_running_command_then_resume(self) -> None:
        self.config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["platforms"][0]["adapt_command"] = [sys.executable, "-c", "import time; time.sleep(1.5)"]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        engine = flflow.Engine(self.config_path)
        outcome = {}
        worker = threading.Thread(target=lambda: outcome.update(engine.start()), daemon=True)
        worker.start()

        def wait_for(predicate, timeout: float = 8.0) -> dict:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = engine.status()
                if predicate(state):
                    return state
                time.sleep(0.05)
            self.fail("Timed out waiting for workflow state")

        wait_for(lambda state: bool(state["platforms"]["nvidia"]["attempts"] and
                  state["platforms"]["nvidia"]["attempts"][0]["commands"] and
                  state["platforms"]["nvidia"]["attempts"][0]["commands"][0]["status"] == "running"))
        flflow.Engine(self.config_path).request("pause")
        paused = wait_for(lambda state: state["status"] == "paused")
        self.assertEqual(paused["platforms"]["nvidia"]["attempts"][0]["commands"][0]["status"], "passed")
        flflow.Engine(self.config_path).request("resume")
        worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["status"], "passed")


if __name__ == "__main__":
    unittest.main()
