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

    def test_configured_backend_without_registry_or_pr_remains_visible_and_unverified(self) -> None:
        config = dict(self.config, backend_labels={"cuda": "NVIDIA", "thead": "THead PPU"})
        monitor = BranchMonitor(config, transport=self._transport)
        status = monitor.status()
        self.assertEqual([item["id"] for item in status["backends"]], ["cuda", "ascend", "thead"])
        thead = self._backend(status, "thead")
        self.assertEqual(thead["label"], "THead PPU")
        self.assertFalse(thead["ci_enabled"])
        self.assertIsNone(thead["pr"])
        self.assertEqual(thead["integration"]["status"], "unknown")
        self.assertEqual(thead["ci"]["status"], "unknown")
        self.assertEqual(thead["gate"]["status"], "missing")
        self.assertEqual(thead["gate"]["passed"], 0)
        self.assertEqual(thead["gate"]["missing"], 20)
        with self.assertRaisesRegex(MonitorError, "No current-HEAD evidence"):
            monitor.read_evidence("0.4.0-dev", "thead", MODELS[0], "eager", "text_single")

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

    def test_branch_ci_rechecks_exact_sha_after_empty_branch_page(self) -> None:
        def transient_empty(url: str) -> dict:
            if "/actions/runs?branch=" in url:
                return {"workflow_runs": []}
            if "/actions/runs?head_sha=" in url:
                return {"workflow_runs": [{"id": 1, "name": "CI", "head_sha": self.head,
                                           "head_branch": "0.4.0-dev", "status": "completed",
                                           "conclusion": "failure", "created_at": "2026-09-24T00:00:00Z"}]}
            return self._transport(url)

        status = BranchMonitor(self.config, transport=transient_empty).status()
        self.assertEqual(status["branch_ci"]["status"], "failed")
        self.assertEqual(self._backend(status)["ci"]["status"], "failed")
        self.assertFalse(status["errors"])

    def _corex_pr(self, number=566, **changes):
        value = {"number": number, "state": "open", "merged": False, "draft": False,
                 "base": {"ref": "0.4.0-dev"}, "head": {"ref": "adapt/iluvatar-corex-vllm-next", "sha": "b" * 40},
                 "title": "adapt(iluvatar): CoreX upgrade", "html_url": f"https://github.com/flagos-ai/vllm-plugin-FL/pull/{number}"}
        value.update(changes)
        return value

    def _corex_config(self):
        return {**self.config, "backend_prs": {"cuda": 484, "corex": 527},
                "backend_pr_discovery": {"corex": {"head_prefixes": ["adapt/iluvatar-corex-"], "title_terms": ["CoreX"]}}}

    def test_replacement_pr_matches_backend_and_target_branch_without_passing_gate(self):
        new = self._corex_pr()
        wrong_branch = self._corex_pr(567, base={"ref": "other"})
        wrong_backend = self._corex_pr(568, head={"ref": "adapt/metax-vllm-next", "sha": "c" * 40})
        def transport(url):
            if "/pulls?" in url:
                self.assertIn("base=0.4.0-dev", url)
                return [wrong_branch, wrong_backend, new]
            if "/pulls/527" in url:
                return self._corex_pr(527, state="closed", draft=True)
            if "/pulls/566" in url:
                return new
            if "/commits/" in url and "/check-runs" in url:
                return {"check_runs": []}
            return self._transport(url)
        status = BranchMonitor(self._corex_config(), transport=transport).status()
        corex = self._backend(status, "corex")
        self.assertEqual(corex["pr"]["number"], 566)
        self.assertEqual(corex["pr"]["state"], "open")
        self.assertFalse(corex["pr"]["draft"])
        self.assertEqual(corex["pr"]["selected_by"], "discovered")
        self.assertEqual(corex["integration"]["status"], "open")
        self.assertEqual(corex["gate"]["status"], "missing")
        self.assertFalse(status["errors"])

    def test_multiple_replacement_prs_do_not_select_one_arbitrarily(self):
        def transport(url):
            if "/pulls?" in url:
                return [self._corex_pr(), self._corex_pr(567)]
            if "/pulls/527" in url:
                return self._corex_pr(527, state="closed", draft=True)
            return self._transport(url)
        status = BranchMonitor(self._corex_config(), transport=transport).status()
        corex = self._backend(status, "corex")
        self.assertEqual(corex["pr"]["number"], 527)
        self.assertEqual(corex["integration"]["status"], "closed")
        self.assertTrue(any("Multiple open corex" in error for error in status["errors"]))

    def test_old_merged_pr_absent_from_head_does_not_suppress_replacement(self):
        def transport(url):
            if "/pulls?" in url:
                return [self._corex_pr()]
            if "/pulls/527" in url:
                return self._corex_pr(527, state="closed", merged=True, merge_commit_sha="f" * 40)
            if "/pulls/566" in url:
                return self._corex_pr()
            if "/commits/" in url and "/check-runs" in url:
                return {"check_runs": []}
            return self._transport(url)
        status = BranchMonitor(self._corex_config(), transport=transport).status()
        self.assertEqual(self._backend(status, "corex")["pr"]["number"], 566)
        self.assertEqual(self._backend(status, "corex")["integration"]["status"], "open")

    def test_discovery_failure_keeps_configured_pr_and_reports_error(self):
        def transport(url):
            if "/pulls?" in url:
                raise OSError("offline")
            if "/pulls/527" in url:
                return self._corex_pr(527, state="closed", draft=True)
            return self._transport(url)
        status = BranchMonitor(self._corex_config(), transport=transport).status()
        self.assertEqual(self._backend(status, "corex")["pr"]["number"], 527)
        self.assertTrue(any("discovery unavailable" in error for error in status["errors"]))

    def _write_audit(self) -> Path:
        record = {"schema_version": 1, "evidence_type": "audited_record", "backend": "cuda",
                  "branch": "0.4.0-dev", "tested_commit": self.head,
                  "tree": git(self.repo, "rev-parse", f"{self.head}^{{tree}}"),
                  "sources": [{"url": "https://example.test/acceptance"}],
                  "gate": {"status": "passed", "scenarios_passed": 20, "scenarios_total": 20,
                           "requests_total": 104,
                           "scenario_definitions": [{"scenario": name, "requests": count} for name, count in SCENARIOS.items()],
                           "groups": [{"model": model, "mode": mode, "scenarios_passed": 5,
                                       "scenarios_total": 5, "requests_total": 26} for model in MODELS for mode in MODES]}}
        path = self.evidence / "cuda" / "acceptance.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_historical_acceptance_requires_same_tree_and_keeps_provenance(self) -> None:
        self._write_audit()
        git(self.repo, "commit", "--allow-empty", "-qm", "merge-equivalent")
        merged = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/upstream/0.4.0-dev", merged)
        monitor = BranchMonitor(dict(self.config, github_repo=""))
        gate = self._backend(monitor.status())["gate"]
        self.assertEqual(gate["status"], "recorded_pass")
        self.assertEqual(gate["passed"], 20)
        self.assertIsNone(gate["requests_passed"])
        self.assertTrue(gate["tree_matches_head"])
        evidence = monitor.read_evidence("0.4.0-dev", "cuda", MODELS[0], "eager", "text_single")
        self.assertEqual(evidence["record"]["tested_commit"], self.head)
        self.assertIsNone(evidence["raw_result"])
        self._write("changed.py", "changed = True\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "new source")
        git(self.repo, "update-ref", "refs/remotes/upstream/0.4.0-dev", git(self.repo, "rev-parse", "HEAD"))
        gate = self._backend(monitor.status())["gate"]
        self.assertEqual(gate["status"], "stale")
        self.assertEqual(gate["passed"], 0)

    def test_historical_raw_json_is_checked_and_current_failure_takes_precedence(self) -> None:
        path = self._write_audit()
        record = json.loads(path.read_text(encoding="utf-8"))
        for group in record["gate"]["groups"]:
            results = self._write_run(group["model"], group["mode"])
            (results.parent / "manifest.json").unlink()
            group.update(raw_results_dir=str(results.relative_to(path.parent)),
                         port=8000 if group["mode"] == "eager" else 8001)
        path.write_text(json.dumps(record), encoding="utf-8")
        monitor = BranchMonitor(dict(self.config, github_repo=""))
        gate = self._backend(monitor.status())["gate"]
        self.assertEqual(gate["status"], "recorded_pass")
        self.assertEqual(gate["requests_passed"], 104)
        evidence = monitor.read_evidence("0.4.0-dev", "cuda", MODELS[0], "eager", "text_single")
        self.assertTrue(evidence["raw_result"]["summary"]["passed"])
        results = self._write_run(MODELS[0], "eager")
        result_path = results / "text_single.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["summary"]["passed"] = False
        result_path.write_text(json.dumps(result), encoding="utf-8")
        gate = self._backend(monitor.status())["gate"]
        self.assertEqual(gate["status"], "failed")

    def test_accepted_ci_and_latest_branch_ci_are_both_preserved(self) -> None:
        config = dict(self.config, accepted_ci_runs={"cuda": {"branch": "0.4.0-dev", "commit": self.head,
                                                             "run_id": 2, "attempt": 2}})
        def transport(url: str) -> dict:
            if "/actions/runs/2/attempts/2/jobs" in url:
                return {"jobs": [{"name": "test-cuda / unit / Unit tests (cuda)", "status": "completed", "conclusion": "success"}]}
            if url.endswith("/actions/runs/2/attempts/2"):
                return {"head_sha": self.head, "run_attempt": 2, "html_url": "https://example.test/run/2"}
            return self._transport(url)
        cuda = self._backend(BranchMonitor(config, transport=transport).status())
        self.assertEqual(cuda["ci"]["status"], "passed")
        self.assertEqual(cuda["ci"]["scope"], "accepted-tree")
        self.assertEqual(cuda["current_ci"]["status"], "failed")

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
