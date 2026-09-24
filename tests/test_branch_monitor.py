"""Branch monitor gates must reflect exact branch and case evidence."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from branch_monitor import BranchMonitor, MonitorError


SCENARIOS = {
    "text_single": 1,
    "text_concurrent_8": 8,
    "image_single": 1,
    "image_concurrent_8": 8,
    "mixed_concurrent_8": 8,
}
MODELS = ["Qwen3.6-27B", "Qwen3.6-35B-A3B"]
MODES = ["eager", "graph"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


class BranchMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "plugin"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        self._write(".github/configs/platforms.yml", "platforms:\n  cuda:\n    enabled: true\n  ascend:\n    enabled: false\n")
        self._write("pyproject.toml", '[project]\nname = "plugin"\ndependencies = ["vllm[audio]==0.28.0"]\n')
        self._write("tools/adaptation-gate-cases/test_text.py",
                    'from case_utils import run_case\n\ndef helper():\n    run_case("bogus_single")\n'
                    'def test_text_single():\n    run_case("text_single")\n'
                    'def test_text_concurrent_8():\n    run_case("text_concurrent_8")\n')
        self._write("tools/adaptation-gate-cases/test_image.py",
                    'from case_utils import run_case\n\ndef test_image_single():\n    run_case("image_single")\n'
                    'def test_image_concurrent_8():\n    run_case("image_concurrent_8")\n')
        self._write("tools/adaptation-gate-cases/test_mix_text_image.py",
                    'from case_utils import run_case\n\ndef test_mix():\n    run_case("mixed_concurrent_8")\n')
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/upstream/0.4.0-dev", self.head)
        self.evidence = self.root / "evidence"
        self.config = {"repo": str(self.repo), "remote": "upstream", "branch": "0.4.0-dev",
                       "github_repo": "flagos-ai/vllm-plugin-FL", "evidence_dir": str(self.evidence),
                       "models": MODELS, "modes": MODES, "backend_prs": {"cuda": 484},
                       "merged_commits": {"cuda": self.head}}

    def _write(self, path: str, content: str) -> None:
        destination = self.repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    def _transport(self, url: str) -> dict:
        if "/actions/runs?" in url:
            return {"workflow_runs": [{"id": 1, "name": "CI", "head_sha": self.head,
                                       "status": "completed", "conclusion": "failure",
                                       "created_at": "2026-09-24T00:00:00Z", "html_url": "https://example.test/run/1"}]}
        if "/actions/runs/1/jobs" in url:
            return {"jobs": [
                {"name": "test-cuda / unit / Unit tests (cuda)", "status": "completed", "conclusion": "success"},
                {"name": "test-cuda / e2e / E2E (cuda)", "status": "completed", "conclusion": "failure"},
                {"name": "test-cuda / notify / Notify (cuda)", "status": "completed", "conclusion": "success"},
                {"name": "test-ascend", "status": "completed", "conclusion": "skipped"}]}
        if "/pulls/484" in url:
            return {"number": 484, "state": "closed", "merged": True, "draft": False,
                    "base": {"ref": "0.4.0-dev"}, "merge_commit_sha": self.head,
                    "html_url": "https://example.test/pr/484", "title": "NVIDIA upgrade"}
        raise AssertionError(f"Unexpected URL: {url}")

    def _monitor(self) -> BranchMonitor:
        return BranchMonitor(self.config, transport=self._transport)

    def _backend(self, status: dict, name: str = "cuda") -> dict:
        return next(backend for backend in status["backends"] if backend["id"] == name)

    def _write_run(self, model: str, mode: str, *, commit: str | None = None) -> Path:
        run = self.evidence / "cuda" / model / mode / "run-1"
        results = run / "results"
        results.mkdir(parents=True, exist_ok=True)
        manifest = {"schema_version": 1, "run_id": f"{model}-{mode}", "backend": "cuda",
                    "branch": "0.4.0-dev", "commit": commit or self.head, "model": model,
                    "mode": mode, "port": 8000 if mode == "eager" else 8001,
                    "results_dir": "results"}
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        for scenario, requests in SCENARIOS.items():
            result = {"schema_version": 1,
                      "case": {"scenario": scenario, "model_path": f"/models/{model}",
                               "served_model_name": f"/models/{model}", "port": str(manifest["port"]),
                               "concurrency": requests},
                      "input": [{} for _ in range(requests)],
                      "output": [{"passed": True, "checks": {"quality": True}} for _ in range(requests)],
                      "summary": {"request_count": requests, "passed_count": requests,
                                  "failed_count": 0, "passed": True}}
            (results / f"{scenario}.json").write_text(json.dumps(result), encoding="utf-8")
        return results

    def test_merged_nvidia_and_failed_ci_do_not_imply_gate_pass(self) -> None:
        status = self._monitor().status()
        cuda = self._backend(status)
        self.assertEqual(status["vllm_version"], "0.28.0")
        self.assertEqual(len(status["cases"]), 5)
        self.assertEqual(cuda["integration"]["status"], "merged")
        self.assertEqual(cuda["ci"]["status"], "failed")
        self.assertEqual(cuda["gate"]["status"], "missing")
        self.assertEqual(cuda["gate"]["passed"], 0)
        self.assertEqual(cuda["gate"]["total"], 20)
        self.assertEqual(cuda["gate"]["requests_total"], 104)
        self.assertEqual(self._backend(status, "ascend")["gate"]["status"], "missing")

    def test_old_commit_evidence_is_stale(self) -> None:
        self._write_run(MODELS[0], "eager", commit="a" * 40)
        gate = self._backend(self._monitor().status())["gate"]
        self.assertEqual(gate["status"], "stale")
        self.assertEqual(gate["passed"], 0)
        self.assertEqual(gate["missing"], 20)

    def test_complete_matrix_passes_exact_current_head(self) -> None:
        for model in MODELS:
            for mode in MODES:
                self._write_run(model, mode)
        cuda = self._backend(self._monitor().status())
        self.assertEqual(cuda["gate"]["status"], "passed")
        self.assertEqual(cuda["gate"]["passed"], 20)
        self.assertEqual(cuda["gate"]["requests_passed"], 104)
        self.assertTrue(all(row["status"] == "passed" for row in cuda["results"]))
        self.assertTrue(cuda["results"][0]["evidence_url"].startswith("/api/branch-evidence?"))
        evidence = self._monitor().read_evidence("0.4.0-dev", "cuda", MODELS[0], "eager", "text_single")
        self.assertEqual(evidence["manifest"]["commit"], self.head)
        self.assertTrue(evidence["result"]["summary"]["passed"])

    def test_false_summary_or_request_rejects_case(self) -> None:
        results = self._write_run(MODELS[0], "eager")
        path = results / "text_single.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["summary"]["passed"] = False
        path.write_text(json.dumps(value), encoding="utf-8")
        cuda = self._backend(self._monitor().status())
        self.assertEqual(cuda["gate"]["status"], "failed")
        self.assertEqual(next(row for row in cuda["results"] if row["scenario"] == "text_single" and row["mode"] == "eager")["status"], "failed")
        value["summary"]["passed"] = True
        value["output"][0]["passed"] = False
        path.write_text(json.dumps(value), encoding="utf-8")
        cuda = self._backend(self._monitor().status())
        self.assertEqual(cuda["gate"]["status"], "failed")
        value["output"][0]["passed"] = True
        value["output"][0]["checks"]["quality"] = False
        path.write_text(json.dumps(value), encoding="utf-8")
        cuda = self._backend(self._monitor().status())
        self.assertEqual(cuda["gate"]["status"], "failed")

    def test_transport_failure_preserves_local_branch_and_unknown_ci(self) -> None:
        def offline(_: str) -> dict:
            raise OSError("offline")
        status = BranchMonitor(self.config, transport=offline).status()
        self.assertEqual(status["head"], self.head)
        self.assertEqual(self._backend(status)["integration"]["status"], "merged")
        self.assertEqual(self._backend(status)["ci"]["status"], "unknown")
        self.assertEqual(self._backend(status)["gate"]["status"], "missing")
        self.assertTrue(status["errors"])

    def test_partial_ci_and_foreign_pr_do_not_count_as_pass_or_candidate(self) -> None:
        self.assertEqual(BranchMonitor._ci_status([
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "skipped"}]), "partial")
        git(self.repo, "update-ref", "refs/remotes/upstream/main", self.head)
        config = dict(self.config, branch="main", merged_commits={})
        status = BranchMonitor(config, transport=self._transport).status()
        self.assertEqual(self._backend(status)["integration"]["status"], "unknown")
        self.assertIn("test", status["cases"][0])
        self.assertIn("file", status["cases"][0])

    def test_branch_without_gate_cases_is_monitorable(self) -> None:
        for path in (self.repo / "tools/adaptation-gate-cases").glob("test_*.py"):
            path.unlink()
        git(self.repo, "add", "-u")
        git(self.repo, "commit", "-qm", "branch without gate cases")
        other_head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/upstream/main", other_head)
        status = self._monitor().status(branch="main")
        self.assertEqual(status["cases"], [])
        self.assertEqual(self._backend(status)["gate"]["status"], "missing")
        self.assertEqual(self._backend(status)["gate"]["total"], 0)
        with self.assertRaises(MonitorError):
            self._monitor().read_evidence("main", "cuda", MODELS[0], "eager", "text_single")


if __name__ == "__main__":
    unittest.main()
