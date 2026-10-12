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

    def _transport(self, url: str) -> dict | list[dict]:
        if "/pulls?" in url:
            return []
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
        self.assertEqual(status["engine"], "vllm")
        self.assertEqual(status["upstream_version"], "0.28.0")
        self.assertEqual(status["case_dir"], "tools/adaptation-gate-cases")
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

    def _adaptation_pr(self, backend, number=901, **changes):
        value = {"number": number, "state": "open", "merged": False, "draft": True,
                 "base": {"ref": "0.4.0-dev"}, "head": {"ref": f"codex/backend-{backend}-demo", "sha": "b" * 40},
                 "title": "Update plugin backend", "html_url": f"https://example.test/pr/{number}"}
        value.update(changes)
        return value

    def _discovery_transport(self, open_prs, details=None):
        calls = []
        details = details if details is not None else {pr["number"]: pr for pr in open_prs}
        def transport(url):
            calls.append(url)
            if "/pulls?" in url:
                return open_prs
            for number, pr in details.items():
                if url.endswith(f"/pulls/{number}"):
                    return pr
            if "/commits/" in url and "/check-runs" in url:
                return {"check_runs": []}
            return self._transport(url)
        return transport, calls

    def test_default_discovery_finds_known_new_backend_drafts_without_selectors(self):
        candidates = [self._adaptation_pr(backend, number) for backend, number in
                      (("thead", 901), ("musa", 902), ("hygon", 903))]
        candidates.append(self._adaptation_pr("unlisted", 904))
        transport, calls = self._discovery_transport(candidates)
        config = dict(self.config, backend_labels={"thead": "THead", "musa": "MUSA", "hygon": "Hygon"})
        monitor = BranchMonitor(config, transport=transport)
        status = monitor.status()
        for backend, number in (("thead", 901), ("musa", 902), ("hygon", 903)):
            row = self._backend(status, backend)
            self.assertEqual(row["pr"]["number"], number)
            self.assertEqual(row["pr"]["selected_by"], "discovered")
            self.assertEqual(row["integration"]["status"], "draft")
            self.assertEqual(row["gate"]["status"], "missing")
        self.assertEqual(set(row["id"] for row in status["backends"]), {"cuda", "ascend", "thead", "musa", "hygon"})
        self.assertEqual(monitor.backend_pr_discovery, {})
        queries = [url for url in calls if "/pulls?" in url]
        self.assertEqual(len(queries), 1)
        self.assertIn("base=0.4.0-dev", queries[0])
        self.assertFalse(status["errors"])

    def test_default_discovery_can_find_registry_backend_without_label_configuration(self):
        transport, _ = self._discovery_transport([self._adaptation_pr("ascend")])
        status = BranchMonitor(self.config, transport=transport).status()
        self.assertEqual(self._backend(status, "ascend")["pr"]["number"], 901)
        self.assertFalse(status["errors"])

    def test_default_discovery_rejects_wrong_base_lookalike_title_and_closed_pr(self):
        candidates = [self._adaptation_pr("thead", 901, base={"ref": "other"}),
                      self._adaptation_pr("thead", 902, head={"ref": "codex/backend-theadish-demo"}),
                      self._adaptation_pr("thead", 903, head={"ref": "manual/the-adaptation"}, title="THead backend upgrade"),
                      self._adaptation_pr("thead", 904, state="closed")]
        transport, calls = self._discovery_transport(candidates)
        status = BranchMonitor(dict(self.config, backend_labels={"thead": "THead"}), transport=transport).status()
        self.assertIsNone(self._backend(status, "thead")["pr"])
        self.assertEqual(self._backend(status, "thead")["integration"]["status"], "unknown")
        self.assertFalse(any(url.endswith(f"/pulls/{number}") for url in calls for number in (901, 902, 903, 904)))
        self.assertFalse(status["errors"])
        monitor = BranchMonitor(self.config, transport=transport)
        self.assertIsNone(monitor._pr("thead", "dev/0.5.18", False, [],
                          [self._adaptation_pr("thead", base={"ref": "exp/0.5.18"})], self.head)[0])

    def test_default_discovery_warns_for_ambiguous_candidates_and_respects_explicit_number(self):
        candidates = [self._adaptation_pr("thead", 901), self._adaptation_pr("thead", 902)]
        transport, _ = self._discovery_transport(candidates)
        config = dict(self.config, backend_labels={"thead": "THead"})
        status = BranchMonitor(config, transport=transport).status()
        self.assertIsNone(self._backend(status, "thead")["pr"])
        self.assertTrue(any("Multiple open thead" in error for error in status["errors"]))
        config["backend_prs"] = {**config["backend_prs"], "thead": 902}
        selected = BranchMonitor(config, transport=transport).status()
        self.assertEqual(self._backend(selected, "thead")["pr"]["number"], 902)
        self.assertEqual(self._backend(selected, "thead")["pr"]["selected_by"], "configured")
        self.assertFalse(selected["errors"])

    def test_explicit_discovery_selector_overrides_canonical_defaults(self):
        custom = self._adaptation_pr("musa", 901, head={"ref": "custom/musa-update", "sha": "b" * 40}, title="MUSA update")
        transport, _ = self._discovery_transport([custom, self._adaptation_pr("musa", 902)])
        config = dict(self.config, backend_pr_discovery={"musa": {"head_prefixes": ["custom/musa-"], "title_terms": ["MUSA"]}})
        status = BranchMonitor(config, transport=transport).status()
        self.assertEqual(self._backend(status, "musa")["pr"]["number"], 901)
        self.assertFalse(status["errors"])

    def test_default_discovery_keeps_configured_open_and_current_tree_merged_prs(self):
        old = self._adaptation_pr("thead", 901, head={"ref": "maintainer/manual-branch", "sha": "b" * 40})
        candidate = self._adaptation_pr("thead", 902)
        config = dict(self.config, backend_prs={"cuda": 484, "thead": 901})
        for merged in (False, True):
            with self.subTest(merged=merged):
                details = {901: {**old, "state": "closed" if merged else "open", "merged": merged,
                                 "merge_commit_sha": self.head if merged else None}, 902: candidate}
                transport, _ = self._discovery_transport([candidate], details)
                status = BranchMonitor(config, transport=transport).status()
                row = self._backend(status, "thead")
                self.assertEqual(row["pr"]["number"], 901)
                self.assertEqual(row["pr"]["selected_by"], "configured")
                self.assertEqual(row["integration"]["status"], "merged" if merged else "draft")
                self.assertFalse(status["errors"])

    def test_default_discovery_preserves_verified_merged_mapping_without_configured_pr(self):
        candidate = self._adaptation_pr("thead", 901)
        transport, _ = self._discovery_transport([candidate])
        config = dict(self.config, merged_commits={**self.config["merged_commits"], "thead": self.head})
        status = BranchMonitor(config, transport=transport).status()
        row = self._backend(status, "thead")
        self.assertIsNone(row["pr"])
        self.assertEqual(row["integration"]["status"], "merged")
        self.assertFalse(status["errors"])
        config["backend_pr_discovery"] = {"thead": {"head_prefixes": ["codex/backend-thead-"]}}
        explicit = BranchMonitor(config, transport=transport).status()
        self.assertEqual(self._backend(explicit, "thead")["pr"]["number"], 901)
        self.assertEqual(self._backend(explicit, "thead")["integration"]["status"], "draft")

    def test_default_corex_discovery_accepts_only_explicit_iluvatar_alias(self):
        transport, _ = self._discovery_transport([self._adaptation_pr("iluvatar", 901)])
        status = BranchMonitor(dict(self.config, backend_labels={"corex": "iluvatar"}), transport=transport).status()
        self.assertEqual(self._backend(status, "corex")["pr"]["number"], 901)
        self.assertNotIn("iluvatar", {row["id"] for row in status["backends"]})
        self.assertFalse(status["errors"])

    def test_default_discovery_failure_is_reported_and_preserves_configured_integration(self):
        def transport(url):
            if "/pulls?" in url:
                raise OSError("offline")
            return self._transport(url)
        status = BranchMonitor(self.config, transport=transport).status()
        self.assertEqual(self._backend(status)["pr"]["number"], 484)
        self.assertEqual(self._backend(status)["integration"]["status"], "merged")
        self.assertTrue(any("discovery unavailable" in error for error in status["errors"]))

    def test_discovered_detail_rechecks_branch_and_selector_before_association(self):
        listed = self._adaptation_pr("thead", 901)
        for changes in ({"base": {"ref": "other"}}, {"head": {"ref": "codex/backend-musa-demo"}}, {"state": "closed"}):
            with self.subTest(changes=changes):
                transport, _ = self._discovery_transport([listed], {901: {**listed, **changes}})
                status = BranchMonitor(dict(self.config, backend_labels={"thead": "THead"}), transport=transport).status()
                self.assertIsNone(self._backend(status, "thead")["pr"])
                self.assertTrue(any("no longer matches" in error for error in status["errors"]))

    def _corex_pr(self, number=566, **changes):
        value = {"number": number, "state": "open", "merged": False, "draft": False,
                 "base": {"ref": "0.4.0-dev"}, "head": {"ref": "adapt/iluvatar-corex-vllm-next", "sha": "b" * 40},
                 "title": "adapt(iluvatar): CoreX upgrade", "html_url": f"https://github.com/flagos-ai/vllm-plugin-FL/pull/{number}"}
        value.update(changes)
        return value

    def _corex_config(self):
        return {**self.config, "backend_prs": {"cuda": 484, "corex": 527},
                "backend_pr_discovery": {"corex": {"head_prefixes": ["adapt/iluvatar-corex-"], "title_terms": ["CoreX"]}}}

    def test_corex_pr_ci_matches_iluvatar_checks_and_excludes_cuda(self) -> None:
        config = dict(self.config, backend_prs={"cuda": 484, "corex": 566})
        for conclusion, expected in (("failure", "failed"), ("success", "passed")):
            with self.subTest(conclusion=conclusion):
                def transport(url):
                    if "/pulls/566" in url:
                        return self._corex_pr()
                    if "/commits/" in url and "/check-runs" in url:
                        return {"check_runs": [
                            {"name": "test-iluvatar / unit / Unit tests (iluvatar)",
                             "status": "completed", "conclusion": conclusion,
                             "details_url": "https://example.test/iluvatar-unit"},
                            {"name": "test-cuda / unit / Unit tests (cuda)",
                             "status": "completed", "conclusion": "failure"},
                            {"name": "test-iluvatar / Setup (iluvatar)",
                             "status": "completed", "conclusion": "failure"},
                            {"name": "test-iluvatar / notify / Notify (iluvatar)",
                             "status": "completed", "conclusion": "failure"}]}
                    return self._transport(url)
                corex = self._backend(BranchMonitor(config, transport=transport).status(), "corex")
                self.assertEqual(corex["ci"]["status"], expected)
                self.assertEqual(corex["ci"]["scope"], "pr")
                self.assertEqual(corex["ci"]["url"], "https://example.test/iluvatar-unit")
                self.assertEqual(corex["gate"]["status"], "missing")

    def test_corex_registry_and_merged_ci_use_the_workflow_platform(self) -> None:
        self._write(".github/configs/platforms.yml",
                    "platforms:\n  cuda:\n    enabled: true\n  iluvatar:\n    enabled: true\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "enable workflow platform")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/upstream/0.4.0-dev", self.head)
        config = dict(self.config, merged_commits={"cuda": self.head, "corex": self.head},
                      backend_labels={"corex": "CoreX"})

        def transport(url):
            if "/actions/runs/1/jobs" in url:
                return {"jobs": [
                    {"name": "test-iluvatar / unit / Unit tests (iluvatar)",
                     "status": "completed", "conclusion": "success"},
                    {"name": "test-cuda / unit / Unit tests (cuda)",
                     "status": "completed", "conclusion": "failure"}]}
            return self._transport(url)

        status = BranchMonitor(config, transport=transport).status()
        self.assertEqual([item["id"] for item in status["backends"]], ["cuda", "corex"])
        corex = self._backend(status, "corex")
        self.assertTrue(corex["ci_enabled"])
        self.assertEqual(corex["ci"]["status"], "passed")
        self.assertEqual(corex["ci"]["scope"], "branch")
        self.assertEqual(self._backend(status)["ci"]["status"], "failed")

    def test_pr_ci_url_follows_aggregate_status_with_unordered_checks(self) -> None:
        config = dict(self.config, backend_prs={"cuda": 484, "corex": 566})
        for job_status, conclusion, expected in (("completed", "failure", "failed"),
                                                 ("in_progress", None, "running")):
            with self.subTest(expected=expected):
                def transport(url):
                    if "/pulls/566" in url:
                        return self._corex_pr()
                    if "/commits/" in url and "/check-runs" in url:
                        return {"check_runs": [
                            {"name": "test-iluvatar / benchmark / Benchmark",
                             "status": "completed", "conclusion": "skipped",
                             "details_url": "https://example.test/skipped"},
                            {"name": "test-cuda / unit / Unit tests (cuda)",
                             "status": "completed", "conclusion": "failure",
                             "details_url": "https://example.test/unrelated"},
                            {"name": "test-iluvatar / functional / Functional",
                             "status": "completed", "conclusion": "success",
                             "details_url": "https://example.test/success"},
                            {"name": "test-iluvatar / e2e / E2E",
                             "status": "queued", "conclusion": None,
                             "details_url": "https://example.test/queued"},
                            {"name": "test-iluvatar / unit / Unit tests (iluvatar)",
                             "status": job_status, "conclusion": conclusion,
                             "details_url": "https://example.test/relevant"}]}
                    return self._transport(url)

                corex = self._backend(BranchMonitor(config, transport=transport).status(), "corex")
                self.assertEqual(corex["ci"]["status"], expected)
                self.assertEqual(corex["ci"]["url"], "https://example.test/relevant")

    def _adaptation_ci_transport(self, pr, checks, *, branch_jobs=None):
        calls = []

        def transport(url):
            calls.append(url)
            if url.endswith(f"/pulls/{pr['number']}"):
                return pr
            if "/commits/" in url and "/check-runs" in url:
                self.assertIn(f"/commits/{pr['head']['sha']}/check-runs", url)
                return {"check_runs": checks}
            if "/actions/runs/1/jobs" in url and branch_jobs is not None:
                return {"jobs": branch_jobs}
            return self._transport(url)

        return transport, calls

    def test_backend_ci_retains_pr_result_after_merge_and_later_branch_changes(self) -> None:
        tested_head = self.head
        pr = self._adaptation_pr("cuda", 484, draft=False,
                                 head={"ref": "codex/backend-cuda-demo", "sha": tested_head})
        checks = [
            {"name": "test-cuda / unit / Unit tests (cuda)", "status": "completed",
             "conclusion": "success", "details_url": "https://example.test/cuda-unit"},
            {"name": "test-cuda / e2e / E2E (cuda)", "status": "completed",
             "conclusion": "success", "details_url": "https://example.test/cuda-e2e"},
            {"name": "test-ascend / unit / Unit tests (ascend)", "status": "completed",
             "conclusion": "failure"},
            {"name": "test-cuda / Setup (cuda)", "status": "completed", "conclusion": "failure"},
            {"name": "test-cuda / notify / Notify (cuda)", "status": "completed", "conclusion": "failure"},
        ]
        branch_jobs = [{"name": "test-cuda / e2e / E2E (cuda)", "status": "completed",
                        "conclusion": "failure", "html_url": "https://example.test/later-cuda-e2e"}]
        transport, calls = self._adaptation_ci_transport(pr, checks, branch_jobs=branch_jobs)
        monitor = BranchMonitor(dict(self.config, merged_commits={}), transport=transport)
        candidate = self._backend(monitor.status(force=True))
        self.assertEqual(candidate["integration"]["status"], "open")
        self.assertEqual(candidate["ci"]["status"], "passed")
        self.assertEqual(candidate["ci"]["scope"], "pr")
        self.assertEqual(candidate["ci"]["commit"], tested_head)

        pr.update(state="closed", merged=True, merge_commit_sha=tested_head)
        merged = self._backend(monitor.status(force=True))
        self.assertEqual(merged["integration"]["status"], "merged")
        self.assertEqual(merged["ci"]["status"], "passed")
        self.assertEqual(merged["ci"]["scope"], "merged-pr")
        self.assertEqual(merged["ci"]["commit"], tested_head)
        self.assertEqual(merged["current_ci"]["status"], "failed")
        self.assertEqual(merged["current_ci"]["scope"], "branch")

        self._write("later-change.txt", "another backend was updated\n")
        git(self.repo, "add", "later-change.txt")
        git(self.repo, "commit", "-qm", "later unrelated adaptation")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/upstream/0.4.0-dev", self.head)
        status = monitor.status(force=True)
        backend = self._backend(status)
        self.assertEqual(status["branch_ci"]["status"], "failed")
        self.assertEqual(backend["ci"]["status"], "passed")
        self.assertEqual(backend["ci"]["scope"], "merged-pr")
        self.assertEqual(backend["ci"]["commit"], tested_head)
        self.assertEqual(backend["ci"]["url"], "https://example.test/cuda-unit")
        self.assertEqual(backend["current_ci"]["status"], "failed")
        self.assertEqual(backend["current_ci"]["commit"], self.head)
        self.assertEqual(backend["current_ci"]["url"], "https://example.test/later-cuda-e2e")
        self.assertNotEqual(backend["ci"]["commit"], backend["current_ci"]["commit"])
        self.assertEqual(backend["gate"]["status"], "missing")
        self.assertTrue(any(f"/commits/{tested_head}/check-runs" in url for url in calls))

    def test_merged_pr_ci_uses_iluvatar_alias_and_excludes_other_platform_failures(self) -> None:
        pr = self._corex_pr(state="closed", merged=True, merge_commit_sha=self.head)
        checks = [
            {"name": "test-cuda / e2e / E2E (cuda)", "status": "completed", "conclusion": "failure"},
            {"name": "test-iluvatar / unit / Unit tests (iluvatar)", "status": "completed",
             "conclusion": "success", "details_url": "https://example.test/merged-iluvatar-unit"},
            {"name": "test-iluvatar / e2e / E2E (iluvatar)", "status": "completed", "conclusion": "success"},
            {"name": "test-iluvatar / Setup (iluvatar)", "status": "completed", "conclusion": "failure"},
            {"name": "test-iluvatar / notify / Notify (iluvatar)", "status": "completed", "conclusion": "failure"},
        ]
        branch_jobs = [{"name": "test-iluvatar / unit / Unit tests (iluvatar)",
                        "status": "completed", "conclusion": "failure"}]
        transport, _ = self._adaptation_ci_transport(pr, checks, branch_jobs=branch_jobs)
        config = dict(self.config, backend_prs={"corex": 566}, merged_commits={})
        backend = self._backend(BranchMonitor(config, transport=transport).status(), "corex")
        self.assertEqual(backend["ci"]["status"], "passed")
        self.assertEqual(backend["ci"]["scope"], "merged-pr")
        self.assertEqual(backend["ci"]["url"], "https://example.test/merged-iluvatar-unit")
        self.assertEqual(backend["current_ci"]["status"], "failed")

    def test_merged_pr_ci_requires_own_backend_checks_to_pass(self) -> None:
        pr = self._adaptation_pr("cuda", 484, state="closed", merged=True,
                                 merge_commit_sha=self.head)
        branch_jobs = [{"name": "test-cuda / unit / Unit tests (cuda)", "status": "completed",
                        "conclusion": "success", "html_url": "https://example.test/current-cuda-pass"}]
        for conclusion, expected in (("failure", "failed"), ("timed_out", "failed"),
                                     ("skipped", "partial"), ("cancelled", "cancelled")):
            with self.subTest(conclusion=conclusion):
                checks = [
                    {"name": "test-cuda / unit / Unit tests (cuda)", "status": "completed", "conclusion": "success"},
                    {"name": "test-cuda / e2e / E2E (cuda)", "status": "completed", "conclusion": conclusion,
                     "details_url": "https://example.test/relevant-merged-check"},
                ]
                transport, _ = self._adaptation_ci_transport(pr, checks, branch_jobs=branch_jobs)
                backend = self._backend(BranchMonitor(self.config, transport=transport).status())
                self.assertEqual(backend["ci"]["status"], expected)
                self.assertEqual(backend["ci"]["scope"], "merged-pr")
                self.assertEqual(backend["ci"]["url"], "https://example.test/relevant-merged-check")
                self.assertEqual(backend["current_ci"]["status"], "passed")

    def test_merged_pr_without_matching_backend_checks_does_not_fall_back_to_pass(self) -> None:
        pr = self._adaptation_pr("cuda", 484, state="closed", merged=True,
                                 merge_commit_sha=self.head)
        branch_jobs = [{"name": "test-cuda / unit / Unit tests (cuda)", "status": "completed", "conclusion": "success"}]
        for checks, expected in (([], "unknown"),
                                 ([{"name": "test-ascend / unit / Unit tests (ascend)",
                                    "status": "completed", "conclusion": "success"}], "unknown"),
                                 ([{"name": "test-cuda / e2e / E2E (cuda)",
                                    "status": "completed", "conclusion": "skipped"}], "skipped")):
            with self.subTest(checks=checks):
                transport, _ = self._adaptation_ci_transport(pr, checks, branch_jobs=branch_jobs)
                backend = self._backend(BranchMonitor(self.config, transport=transport).status())
                self.assertEqual(backend["ci"]["status"], expected)
                self.assertEqual(backend["ci"]["scope"], "merged-pr")
                self.assertEqual(backend["current_ci"]["status"], "passed")

    def test_merged_pr_missing_or_invalid_head_keeps_explicit_branch_fallback(self) -> None:
        branch_jobs = [{"name": "test-cuda / unit / Unit tests (cuda)", "status": "completed",
                        "conclusion": "success", "html_url": "https://example.test/current-cuda-unit"}]
        for invalid_head in (None, "", "not-a-sha", "b" * 39):
            with self.subTest(head=invalid_head):
                pr = self._adaptation_pr("cuda", 484, state="closed", merged=True,
                                         merge_commit_sha=self.head, head={"sha": invalid_head})
                if invalid_head is None:
                    pr.pop("head")
                transport, calls = self._adaptation_ci_transport(pr, [], branch_jobs=branch_jobs)
                backend = self._backend(BranchMonitor(self.config, transport=transport).status())
                self.assertEqual(backend["ci"]["status"], "passed")
                self.assertEqual(backend["ci"]["scope"], "branch")
                self.assertEqual(backend["ci"]["commit"], self.head)
                self.assertEqual(backend["ci"]["url"], "https://example.test/current-cuda-unit")
                self.assertFalse(any("/check-runs" in url for url in calls))

    def test_unverified_merged_pr_cannot_supply_adaptation_ci(self) -> None:
        branch_jobs = [{"name": "test-cuda / unit / Unit tests (cuda)", "status": "completed", "conclusion": "failure"}]
        branch_ci = {"status": "failed", "commit": self.head, "url": "https://example.test/current-run"}
        for changes, integration_commit in (({"base": {"ref": "other"}}, self.head),
                                             ({"merge_commit_sha": "f" * 40}, "f" * 40),
                                             ({}, "f" * 40)):
            with self.subTest(changes=changes, integration_commit=integration_commit):
                pr = self._adaptation_pr("cuda", 484, **{
                    "state": "closed", "merged": True, "merge_commit_sha": self.head, **changes})
                def no_network(url):
                    self.fail(f"Unverified merged PR must not read historical checks: {url}")
                monitor = BranchMonitor(self.config, transport=no_network)
                result = monitor._backend_ci("cuda", {"status": "merged", "commit": integration_commit},
                                             branch_jobs, branch_ci, pr, False, [], branch="0.4.0-dev")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["scope"], "branch")
                self.assertEqual(result["commit"], self.head)

    def test_merged_pr_ci_verifies_the_selected_branch_instead_of_default_branch(self) -> None:
        git(self.repo, "update-ref", "refs/remotes/upstream/main", self.head)
        pr = self._adaptation_pr("cuda", 484, state="closed", merged=True,
                                 base={"ref": "main"}, merge_commit_sha=self.head)
        checks = [{"name": "test-cuda / unit / Unit tests (cuda)", "status": "completed", "conclusion": "success"}]
        transport, _ = self._adaptation_ci_transport(pr, checks)
        backend = self._backend(BranchMonitor(dict(self.config, merged_commits={}),
                                             transport=transport).status(branch="main"))
        self.assertEqual(backend["integration"]["status"], "merged")
        self.assertEqual(backend["ci"]["status"], "passed")
        self.assertEqual(backend["ci"]["scope"], "merged-pr")
        self.assertEqual(backend["ci"]["commit"], pr["head"]["sha"])

    def test_ci_check_url_accepts_job_links_and_prefers_relevant_backend_result(self) -> None:
        checks = [
            {"status": "completed", "conclusion": "success", "html_url": "https://example.test/success-job"},
            {"status": "completed", "conclusion": "failure", "html_url": "https://example.test/failed-job"},
        ]
        self.assertEqual(BranchMonitor._ci_check_url(checks, "failed"), "https://example.test/failed-job")
        self.assertEqual(BranchMonitor._ci_check_url(checks, "passed"), "https://example.test/success-job")
        checks[1]["details_url"] = "https://example.test/failed-check"
        self.assertEqual(BranchMonitor._ci_check_url(checks, "failed"), "https://example.test/failed-check")

    def test_custom_ci_platform_applies_to_accepted_run(self) -> None:
        config = dict(self.config, backend_labels={"corex": "CoreX"},
                      backend_ci_platforms={"corex": "custom_vendor"},
                      accepted_ci_runs={"corex": {"branch": "0.4.0-dev", "commit": self.head,
                                                   "run_id": 2, "attempt": 1}})

        def transport(url):
            if "/actions/runs/2/attempts/1/jobs" in url:
                return {"jobs": [
                    {"name": "test-custom_vendor / unit / Unit tests (custom_vendor)",
                     "status": "completed", "conclusion": "success"},
                    {"name": "test-cuda / unit / Unit tests (cuda)",
                     "status": "completed", "conclusion": "failure"}]}
            if "/actions/runs/2/attempts/1" in url:
                return {"head_sha": self.head, "run_attempt": 1, "html_url": "https://example.test/run/2"}
            return self._transport(url)

        corex = self._backend(BranchMonitor(config, transport=transport).status(), "corex")
        self.assertEqual(corex["ci"]["status"], "passed")
        self.assertEqual(corex["ci"]["scope"], "accepted-tree")
        self.assertEqual(corex["current_ci"]["status"], "unknown")
        self.assertEqual(corex["gate"]["status"], "missing")

    def test_ci_platform_mapping_rejects_ambiguous_or_invalid_names(self) -> None:
        for mapping in (["iluvatar"], {"corex": ""}, {"corex": "cuda / unit"},
                        {"corex": "iluvatar", "other": "iluvatar"}):
            with self.subTest(mapping=mapping), self.assertRaises(MonitorError):
                BranchMonitor(dict(self.config, backend_ci_platforms=mapping), transport=self._transport)

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

    def test_sglang_version_uses_engine_pin_in_optional_dependencies(self) -> None:
        self._write("pyproject.toml",
                    '[project]\nname = "plugin"\ndependencies = ["vllm==9.9.9"]\n'
                    '[project.optional-dependencies]\ncuda = ["sglang[all]==0.5.18"]\n')
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "sglang version fixture")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/upstream/0.4.0-dev", self.head)
        status = BranchMonitor(dict(self.config, engine="sglang"), transport=self._transport).status()
        self.assertEqual(status["engine"], "sglang")
        self.assertEqual(status["upstream_version"], "0.5.18")
        self.assertIsNone(status["vllm_version"])
        self.assertEqual(len(status["cases"]), 0)

    def test_sglang_without_pin_does_not_report_vllm_version(self) -> None:
        status = BranchMonitor(dict(self.config, engine="sglang"), transport=self._transport).status()
        self.assertIsNone(status["upstream_version"])
        self.assertIsNone(status["vllm_version"])

    def test_configurable_case_directory_keeps_exact_case_evidence_checks(self) -> None:
        self._write("acceptance/cases/test_engine.py",
                    'def test_engine():\n    run_case("text_single")\n')
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "custom case directory")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/upstream/0.4.0-dev", self.head)
        for model in MODELS:
            for mode in MODES:
                self._write_run(model, mode)
        monitor = BranchMonitor(dict(self.config, case_dir="acceptance/cases"),
                                transport=self._transport)
        status = monitor.status()
        self.assertEqual(status["case_dir"], "acceptance/cases")
        self.assertEqual([case["scenario"] for case in status["cases"]], ["text_single"])
        gate = self._backend(status)["gate"]
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["total"], 4)
        self.assertEqual(gate["requests_passed"], 4)
        evidence = monitor.read_evidence("0.4.0-dev", "cuda", MODELS[0], "eager", "text_single")
        self.assertEqual(evidence["manifest"]["commit"], self.head)
        self.assertTrue(evidence["result"]["summary"]["passed"])
        result_path = self.evidence / "cuda" / MODELS[0] / "eager" / "run-1" / "results" / "text_single.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["output"][0]["checks"]["quality"] = False
        result_path.write_text(json.dumps(result), encoding="utf-8")
        self.assertEqual(self._backend(monitor.status())["gate"]["status"], "failed")

    def test_unparseable_case_directory_never_reports_pass_from_existing_evidence(self) -> None:
        self._write("tests/e2e_tests/test_smoke.py", 'def test_smoke():\n    assert True\n')
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "ordinary smoke suite")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/upstream/0.4.0-dev", self.head)
        for model in MODELS:
            for mode in MODES:
                self._write_run(model, mode)
        self._write_audit()
        monitor = BranchMonitor(dict(self.config, engine="sglang", case_dir="tests/e2e_tests"),
                                transport=self._transport)
        status = monitor.status()
        self.assertEqual(status["cases"], [])
        gate = self._backend(status)["gate"]
        self.assertEqual(gate["status"], "unknown")
        self.assertEqual(gate["passed"], 0)
        self.assertEqual(gate["total"], 0)
        self.assertIn("tests/e2e_tests", gate["detail"])
        with self.assertRaisesRegex(MonitorError, "Unknown gate scenario"):
            monitor.read_evidence("0.4.0-dev", "cuda", MODELS[0], "eager", "text_single")

    def test_engine_and_case_directory_validate_explicit_config(self) -> None:
        for engine in (None, "other", "SGLang", {}, []):
            with self.subTest(engine=engine), self.assertRaisesRegex(MonitorError, "engine must"):
                BranchMonitor(dict(self.config, engine=engine))
        for case_dir in (None, "", "/tmp/cases", "../cases", "cases/../tests", "cases//tests",
                         "cases/./tests", "C:/cases", "cases\\tests", "cases\n/tests"):
            with self.subTest(case_dir=case_dir), self.assertRaisesRegex(MonitorError, "case_dir must"):
                BranchMonitor(dict(self.config, case_dir=case_dir))


    def _sglang_fixture(self, *, include_pytest=True, sources=None):
        text_prompts = ["State count?", "Capital?"]
        text_expected = {"State count?": ["50"], "Capital?": ["paris"]}
        vl_cases = [{"image": name, "question": "Identify " + name, "expected": [answer]}
                    for name, answer in (("red.jpg", "red"), ("cat.jpg", "cat"), ("sign.png", "red"), ("seven.png", "7"))]
        stems = ["qwen3_6_27b", "qwen3_6_35b_a3b"]
        for model, stem in zip(MODELS, stems):
            common = ('import os\nMODEL_PATH = os.environ.get("MODEL_PATH", ' + repr('/models/' + model) + ')\n'
                      'TEXT_PROMPTS = ' + repr(text_prompts) + '\nVL_CASES = ' + repr(vl_cases) + '\n'
                      'def _make_engine():\n    return Engine(disable_cuda_graph=True)\n')
            offline = common + 'TEXT_EXPECTED = ' + repr({"State count?": "50", "Capital?": "Paris"}) + '\n'
            offline += ('def run_engine():\n    return ["50", "Paris"], ["red", "cat", "red", "7"]\n'
                        'def validate(text_outputs, vl_outputs):\n    assert text_outputs == ["50", "Paris"]\n'
                        '    assert vl_outputs == ["red", "cat", "red", "7"]\n')
            self._write('examples/' + stem + '_offline_inference.py', offline)
            concurrent = (common + 'TEXT_EXPECTED = ' + repr(text_expected) + '\n'
                          'CONCURRENT_N = int(os.environ.get("CONCURRENT_N", "4"))\nALL_MODES = ["text", "vl", "mixed"]\n')
            self._write('examples/' + stem + '_concurrent.py', concurrent)
        if include_pytest:
            defaults = [{'text': prompt, 'expected': text_expected[prompt]} for prompt in text_prompts]
            self._write('tests/e2e_tests/concurrent/test_concurrent_smoke.py',
                        '_DEFAULT_TEXT_CASES = ' + repr(defaults) + '\n_DEFAULT_VL_CASES = ' + repr(vl_cases) + '\n')
            self._write('tests/e2e_tests/plugin_utils.py', 'def assert_expected(text, expected, label):\n    assert text\n')
            for model, case in zip(MODELS, ['27b_tp4_nograph', '35b_a3b_tp4_nograph']):
                self._write('tests/models/qwen3_6/' + case + '.yaml',
                            'llm:\n  model: "/models/' + model + '"\n  disable_cuda_graph: true\n'
                            'concurrent:\n  modes: [text, vl, mixed]\n  concurrent_n: 4\n')
        self._write('pyproject.toml', '[project]\nname = "sglang_fl"\n'
                    '[project.optional-dependencies]\ncuda = ["sglang==0.5.18"]\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'sglang acceptance source fixture')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'update-ref', 'refs/remotes/upstream/0.4.0-dev', self.head)
        config = dict(self.config, engine='sglang', modes=['eager'], merged_commits={'cuda': self.head})
        if sources is not None:
            config['case_sources'] = sources
        return BranchMonitor(config, transport=self._transport)

    def _write_sglang_run(self, monitor, model, *, run_id='run-1', only=None, commit=None):
        from sglang_evidence import request_specs, _input
        run = self.evidence / 'cuda' / model / 'eager' / run_id
        (run / 'results').mkdir(parents=True, exist_ok=True)
        manifest = {'schema_version': 1, 'engine': 'sglang', 'run_id': run_id, 'backend': 'cuda',
                    'branch': '0.4.0-dev', 'commit': commit or self.head, 'model': model, 'mode': 'eager', 'results_dir': 'results'}
        (run / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        for case in monitor._cases(self.head):
            if model not in case['models'] or (only is not None and case['scenario'] not in only):
                continue
            rows = [{'input': _input(spec), 'output': {'text': spec['expected'][0] if spec['expected'] else 'answer', 'error': None},
                     'checks': {'nonempty': True, 'expected': True}, 'status': 'passed'} for spec in request_specs(case)]
            value = {'schema_version': 1, 'evidence_type': 'sglang_raw_requests',
                     'case': {'scenario': case['scenario'], 'model_path': '/models/' + model, 'source': case['source'],
                              'source_blob': case['source_blob'], 'commit': manifest['commit'], 'backend': 'cuda', 'mode': 'eager',
                              'request_count': case['requests'], 'definition_files': case['definition_files']},
                     'execution': {'completed': True, 'exit_code': 0, 'plugin_loaded': True, 'plugin_active': True},
                     'requests': rows, 'summary': {'request_count': case['requests'], 'passed_count': case['requests'],
                                'failed_count': 0, 'skipped_count': 0, 'passed': True}}
            (run / 'results' / (case['scenario'] + '.json')).write_text(json.dumps(value), encoding='utf-8')
        return run / 'results'

    def test_sglang_examples_and_pytest_discover_actual_models_requests_and_sources(self):
        monitor = self._sglang_fixture()
        status = monitor.status()
        self.assertEqual(status['engine'], 'sglang')
        self.assertEqual(status['upstream_version'], '0.5.18')
        self.assertEqual(status['case_sources'], ['examples', 'tests/e2e_tests/concurrent'])
        self.assertEqual(status['case_discovery']['warnings'], [])
        self.assertEqual(len(status['cases']), 16)
        self.assertEqual(sum(case['requests'] for case in status['cases']), 60)
        self.assertEqual(self._backend(status)['gate']['status'], 'missing')
        self.assertEqual(self._backend(status)['gate']['total'], 16)
        self.assertEqual(self._backend(status)['gate']['requests_total'], 60)
        self.assertEqual(len(self._backend(status)['results']), 16)
        self.assertTrue(all('请求' in case['label'] for case in status['cases']))
        self.assertTrue(all(case['source_blob'] == git(self.repo, 'rev-parse', self.head + ':' + case['source']) for case in status['cases']))
        mixed = next(case for case in status['cases'] if case['scenario'] == 'qwen3_6_27b_concurrent_mixed')
        from sglang_evidence import request_specs
        self.assertEqual([spec['kind'] for spec in request_specs(mixed)], ['text', 'text', 'vl', 'vl'])
        pytest_case = next(case for case in status['cases'] if case['family'] == 'pytest_concurrent')
        self.assertEqual(pytest_case['environment']['FL_CONCURRENT_N'], '4')
        self.assertEqual(len(pytest_case['definition_files']), 2)

    def test_sglang_complete_raw_matrix_passes_and_exposes_evidence(self):
        monitor = self._sglang_fixture()
        for model in MODELS:
            self._write_sglang_run(monitor, model)
        status = monitor.status()
        gate = self._backend(status)['gate']
        self.assertEqual(gate['status'], 'passed')
        self.assertEqual(gate['passed'], 16)
        self.assertEqual(gate['requests_passed'], 60)
        self.assertEqual(gate['requests_total'], 60)
        case = next(case for case in status['cases'] if case['model'] == MODELS[0])
        evidence = monitor.read_evidence('0.4.0-dev', 'cuda', MODELS[0], 'eager', case['scenario'])
        self.assertEqual(evidence['result']['evidence_type'], 'sglang_raw_requests')
        self.assertNotIn('port', evidence['manifest'])
        with self.assertRaisesRegex(MonitorError, 'does not apply'):
            monitor.read_evidence('0.4.0-dev', 'cuda', MODELS[1], 'eager', case['scenario'])

    def test_sglang_raw_output_rechecked_even_when_summary_checks_and_exit_claim_success(self):
        monitor = self._sglang_fixture()
        results = self._write_sglang_run(monitor, MODELS[0])
        path = results / 'qwen3_6_27b_offline_inference_text.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        for text in ('wrong answer', '', '50'):
            value = json.loads(json.dumps(original))
            index = 1 if text == '50' else 0
            value['requests'][index]['output']['text'] = text
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.subTest(text=text):
                self.assertEqual(self._backend(monitor.status())['gate']['status'], 'failed')
        value = json.loads(json.dumps(original))
        value['requests'][0]['input']['prompt'] = 'different source input'
        path.write_text(json.dumps(value), encoding='utf-8')
        self.assertEqual(self._backend(monitor.status())['gate']['status'], 'failed')

    def test_sglang_source_pin_identity_activation_skip_and_summary_are_required(self):
        monitor = self._sglang_fixture()
        results = self._write_sglang_run(monitor, MODELS[0])
        path = results / 'qwen3_6_27b_concurrent_text.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        mutations = [('source blob', lambda value: value['case'].update(source_blob='a' * 40)),
                     ('candidate commit', lambda value: value['case'].update(commit='a' * 40)),
                     ('backend', lambda value: value['case'].update(backend='musa')),
                     ('model', lambda value: value['case'].update(model_path='/models/' + MODELS[1])),
                     ('plugin', lambda value: value['execution'].update(plugin_active=False)),
                     ('exit code', lambda value: value['execution'].update(exit_code=1)),
                     ('skip', lambda value: value['requests'][0].update(status='skipped')),
                     ('summary', lambda value: value['summary'].update(skipped_count=1)),
                     ('missing request', lambda value: value['requests'].pop())]
        for name, change in mutations:
            value = json.loads(json.dumps(original))
            change(value)
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.subTest(name=name):
                self.assertEqual(self._backend(monitor.status())['gate']['status'], 'failed')

    def test_sglang_partial_run_does_not_reuse_older_complete_manifest(self):
        monitor = self._sglang_fixture()
        for model in MODELS:
            self._write_sglang_run(monitor, model)
        self.assertEqual(self._backend(monitor.status())['gate']['status'], 'passed')
        selected = 'qwen3_6_27b_concurrent_text'
        results = self._write_sglang_run(monitor, MODELS[0], run_id='run-new', only=[selected])
        manifest_path = results.parent / 'manifest.json'
        value = json.loads(manifest_path.read_text(encoding='utf-8'))
        value['created_at'] = '2099-01-01T00:00:00Z'
        manifest_path.write_text(json.dumps(value), encoding='utf-8')
        gate = self._backend(monitor.status())['gate']
        self.assertEqual(gate['status'], 'partial')
        self.assertEqual(gate['passed'], 9)
        self.assertEqual(gate['missing'], 7)

    def test_sglang_manifest_commit_stale_and_pytest_yaml_pin_change_rejects_evidence(self):
        monitor = self._sglang_fixture()
        self._write_sglang_run(monitor, MODELS[0], commit='a' * 40)
        self.assertEqual(self._backend(monitor.status())['gate']['status'], 'stale')
        results = self._write_sglang_run(monitor, MODELS[0], run_id='run-current')
        path = results / 'pytest_qwen3_6_27b_tp4_nograph_text.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['case']['definition_files'][0]['blob'] = 'a' * 40
        path.write_text(json.dumps(value), encoding='utf-8')
        self.assertEqual(self._backend(monitor.status())['gate']['status'], 'failed')

    def test_sglang_missing_source_stays_explicit_and_blocks_full_acceptance(self):
        monitor = self._sglang_fixture(include_pytest=False)
        self.assertEqual(len(monitor._cases(self.head)), 10)
        for model in MODELS:
            self._write_sglang_run(monitor, model)
        status = monitor.status()
        self.assertEqual(status['case_discovery']['warnings'][0]['source'], 'tests/e2e_tests/concurrent')
        self.assertEqual(self._backend(status)['gate']['status'], 'partial')
        self.assertIn('不能判定完整', self._backend(status)['gate']['detail'])

    def test_sglang_sources_validate_relative_paths_and_vllm_json_not_accepted(self):
        for sources in ([], 'examples', ['../examples'], ['/examples'], ['examples//x'], [None]):
            with self.subTest(sources=sources), self.assertRaisesRegex(MonitorError, 'case_sources'):
                BranchMonitor(dict(self.config, engine='sglang', case_sources=sources))
        monitor = self._sglang_fixture()
        results = self._write_sglang_run(monitor, MODELS[0])
        path = results / 'qwen3_6_27b_concurrent_text.json'
        path.write_text(json.dumps({'schema_version': 1, 'case': {}, 'output': [], 'summary': {'passed': True}}), encoding='utf-8')
        self.assertEqual(self._backend(monitor.status())['gate']['status'], 'failed')

    def test_sglang_collector_calls_original_offline_source_and_archives_outputs_without_gpu(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from sglang_evidence import collect
        monitor = self._sglang_fixture(sources=['examples'])
        selected = [case for case in monitor._cases(self.head) if case['model'] == MODELS[0] and case['family'] == 'offline']
        cache = {}
        # Synthetic source fixture only: these functions return literal fixture
        # outputs. This tests archival; it is not actual GPU acceptance.
        with patch.dict('sys.modules', {'sglang_fl': SimpleNamespace(is_plugin_loaded=lambda: True, is_plugin_active=lambda: True)}), patch.dict('os.environ'):
            for case in selected:
                result = collect(self.repo, self.head, '0.4.0-dev', 'cuda', case, '/models/' + MODELS[0],
                                 self.evidence / 'cuda' / MODELS[0] / 'eager', 'collected-offline', cache)
                self.assertEqual(result['status'], 'passed')
            with self.assertRaisesRegex(ValueError, 'already contains'):
                collect(self.repo, self.head, '0.4.0-dev', 'cuda', selected[0], '/models/' + MODELS[0],
                        self.evidence / 'cuda' / MODELS[0] / 'eager', 'collected-offline', cache)
        run = self.evidence / 'cuda' / MODELS[0] / 'eager' / 'collected-offline'
        self.assertEqual(len(list((run / 'results').glob('*.json'))), 2)
        gate = self._backend(monitor.status())['gate']
        self.assertEqual(gate['status'], 'partial')
        self.assertEqual(gate['passed'], 2)


    def test_sglang_missing_configured_model_prevents_full_pass(self):
        fixture = self._sglang_fixture(sources=['examples'])
        monitor = BranchMonitor(dict(fixture.config, models=[MODELS[0], 'Missing-Model']), transport=self._transport)
        self._write_sglang_run(monitor, MODELS[0])
        status = monitor.status()
        self.assertTrue(any(row.get('model') == 'Missing-Model' for row in status['case_discovery']['warnings']))
        self.assertEqual(self._backend(status)['gate']['status'], 'partial')

    def test_sglang_collector_refuses_existing_manifest_identity_change(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from sglang_evidence import collect
        monitor = self._sglang_fixture(sources=['examples'])
        selected = [case for case in monitor._cases(self.head) if case['model'] == MODELS[0] and case['family'] == 'offline']
        output_dir = self.evidence / 'cuda' / MODELS[0] / 'eager'
        with patch.dict('sys.modules', {'sglang_fl': SimpleNamespace(is_plugin_loaded=lambda: True, is_plugin_active=lambda: True)}), patch.dict('os.environ'):
            collect(self.repo, self.head, '0.4.0-dev', 'cuda', selected[0], '/models/' + MODELS[0], output_dir, 'same-run')
            manifest_path = output_dir / 'same-run' / 'manifest.json'
            original = manifest_path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'manifest identity differs'):
                collect(self.repo, self.head, '0.4.0-dev', 'musa', selected[1], '/models/' + MODELS[0], output_dir, 'same-run')
            self.assertEqual(manifest_path.read_bytes(), original)

    def test_sglang_collector_records_actual_pytest_model_and_rejects_mismatch(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from sglang_evidence import collect
        monitor = self._sglang_fixture()
        case = next(case for case in monitor._cases(self.head) if case['model'] == MODELS[0] and case['family'] == 'pytest_concurrent')
        cfg = SimpleNamespace(model='/actual/different-model', engine_kwargs=lambda: {'model_path': '/actual/different-model'})
        module = SimpleNamespace(_CFG=cfg)
        loader_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _: None))
        with patch('sglang_evidence.importlib.util.spec_from_file_location', return_value=loader_spec), patch('sglang_evidence.importlib.util.module_from_spec', return_value=module), patch.dict('os.environ'):
            result = collect(self.repo, self.head, '0.4.0-dev', 'cuda', case, '/models/' + MODELS[0], self.evidence, 'actual-model')
        self.assertEqual(result['status'], 'failed')
        value = json.loads((self.evidence / 'actual-model' / 'results' / (case['scenario'] + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(value['case']['model_path'], '/actual/different-model')
        self.assertFalse(value['execution']['completed'])
        self.assertIn('Actual pytest model config differs', value['execution']['error'])

    def test_sglang_collector_records_actual_returned_request_inputs(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from sglang_evidence import collect
        monitor = self._sglang_fixture()
        case = next(case for case in monitor._cases(self.head) if case['model'] == MODELS[0] and case['family'] == 'pytest_concurrent' and case['request_kind'] == 'text')
        model_path = '/models/' + MODELS[0]
        cfg = SimpleNamespace(model=model_path, engine_kwargs=lambda: {'model_path': model_path})
        module = SimpleNamespace(_CFG=cfg, _engine_kwargs=lambda: {'disable_cuda_graph': True},
                                 _run_text_concurrent=lambda _: [('different actual prompt', '50')] * case['requests'])
        module.test_concurrent = lambda: module._run_text_concurrent(None)
        loader_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _: None))
        with patch('sglang_evidence.importlib.util.spec_from_file_location', return_value=loader_spec), patch('sglang_evidence.importlib.util.module_from_spec', return_value=module), patch.dict('sys.modules', {'sglang_fl': SimpleNamespace(is_plugin_loaded=lambda: True, is_plugin_active=lambda: True)}), patch.dict('os.environ'):
            result = collect(self.repo, self.head, '0.4.0-dev', 'cuda', case, model_path, self.evidence, 'actual-inputs')
        self.assertEqual(result['status'], 'failed')
        value = json.loads((self.evidence / 'actual-inputs' / 'results' / (case['scenario'] + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(value['requests'][0]['input']['prompt'], 'different actual prompt')
        self.assertTrue(value['execution']['completed'])

    def test_branch_without_gate_cases_is_monitorable(self) -> None:
        for path in (self.repo / "tools/adaptation-gate-cases").glob("test_*.py"):
            path.unlink()
        git(self.repo, "add", "-u")
        git(self.repo, "commit", "-qm", "branch without gate cases")
        other_head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/upstream/main", other_head)
        status = self._monitor().status(branch="main")
        self.assertEqual(status["cases"], [])
        self.assertEqual(self._backend(status)["gate"]["status"], "unknown")
        self.assertEqual(self._backend(status)["gate"]["total"], 0)
        with self.assertRaises(MonitorError):
            self._monitor().read_evidence("main", "cuda", MODELS[0], "eager", "text_single")


if __name__ == "__main__":
    unittest.main()
