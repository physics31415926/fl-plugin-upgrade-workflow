"""New adaptations are visible before their CI registry entry lands."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from fl_dashboard.monitoring.branch_monitor import BranchMonitor


class DiscoveredBackendRowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prs = [{
            "number": 901, "title": "adapt(amd): ROCm backend on vLLM 0.32",
            "body": "## Tests\n| Test | Result |\n| --- | --- |\n| Gate | FAIL |",
            "state": "open", "draft": True, "merged": False,
            "base": {"ref": "next-release"},
            "head": {"ref": "adapt/amd-runtime", "sha": "c" * 40},
        }]
        self.checks = [{"name": "test-cuda / unit", "status": "completed", "conclusion": "failure"}]
        self.details = None

    def transport(self, url):
        if "/pulls?" in url:
            # Deliberately return other branches too: scope must be enforced.
            return self.prs
        if "/check-runs" in url:
            return {"check_runs": self.checks}
        for pr in self.prs:
            if url.endswith(f"/pulls/{pr['number']}"):
                return self.details or pr
        raise AssertionError(url)

    def monitor(self, engine="vllm"):
        monitor = BranchMonitor({"repo": self.temp.name, "evidence_dir": self.temp.name,
            "branch": "stable", "github_repo": f"example/{engine}-plugin", "engine": engine},
            transport=self.transport)
        boundaries = {
            "_head": lambda branch: ("refs/remotes/origin/" + branch,
                                     ("b" if branch == "next-release" else "a") * 40),
            "_cases": lambda sha: [], "_registry": lambda sha: {"cuda": True, "ascend": False},
            "_branch_ci": lambda branch, sha, force, errors: ({"commit": sha}, []),
            "_version": lambda sha: "0.32.0" if sha == "b" * 40 else "0.28.0",
            "_branches": lambda: ["stable", "next-release"],
            "_ancestor": lambda ancestor, head: ancestor == head,
        }
        for name, value in boundaries.items():
            replacement = patch.object(monitor, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        return monitor

    def status(self, monitor, branch="next-release"):
        return monitor.status(branch=branch, force=True)

    def test_draft_without_registry_is_shown_without_claiming_ci_or_gate_pass(self):
        monitor = self.monitor()
        result = self.status(monitor)
        self.assertEqual([row["id"] for row in result["backends"]], ["cuda", "amd", "ascend"])
        amd = result["backends"][1]
        self.assertEqual(amd["label"], "AMD / 超威半导体")
        self.assertEqual(amd["pr"]["number"], 901)
        self.assertEqual(amd["integration"]["status"], "draft")
        self.assertEqual(amd["ci"]["status"], "unknown")
        self.assertEqual(amd["acceptance"]["status"], "failed")
        self.assertFalse(amd["ci_enabled"])
        self.assertEqual(monitor.backend_labels, {})
        self.assertEqual(monitor.backend_prs, {})

    def test_selected_branch_version_and_backend_rows_stay_isolated(self):
        for engine in ("vllm", "sglang"):
            with self.subTest(engine=engine):
                monitor = self.monitor(engine)
                for branch, version, present in (("stable", "0.28.0", False),
                        ("next-release", "0.32.0", True), ("stable", "0.28.0", False)):
                    result = self.status(monitor, branch)
                    self.assertEqual(result["upstream_version"], version)
                    self.assertEqual(any(row["id"] == "amd" for row in result["backends"]), present)

    def test_only_amd_checks_decide_amd_ci(self):
        self.checks.append({"name": "test-amd / unit", "status": "completed", "conclusion": "success"})
        amd = self.status(self.monitor())["backends"][1]
        self.assertEqual(amd["ci"]["status"], "passed")

    def test_closed_unmerged_and_unrelated_merge_do_not_add_rows(self):
        self.prs[0].update(state="closed", merged=False)
        for merged, merge_sha in ((False, None), (True, "e" * 40), (True, "b" * 40)):
            with self.subTest(merged=merged, merge_sha=merge_sha):
                self.prs[0].update(merged=merged, merged_at="2026-01-01T00:00:00Z" if merged else None,
                                   merge_commit_sha=merge_sha)
                rows = self.status(self.monitor())["backends"]
                self.assertEqual(any(row["id"] == "amd" for row in rows), merge_sha == "b" * 40)

    def test_hardware_mentions_without_adaptation_intent_do_not_add_rows(self):
        self.prs[0].update(title="docs: explain ROCm diagnostics", body="## Environment\nAMD W7900D")
        self.assertFalse(any(row["id"] == "amd" for row in self.status(self.monitor())["backends"]))

    def test_detail_revalidation_removes_a_pr_that_changed_target(self):
        self.details = copy.deepcopy(self.prs[0])
        self.details["base"] = {"ref": "stable"}
        result = self.status(self.monitor())
        self.assertFalse(any(row["id"] == "amd" for row in result["backends"]))
        self.assertTrue(any("no longer matches" in error for error in result["errors"]))

    def test_ambiguous_adaptations_are_reported_without_selecting_one(self):
        self.prs.append(dict(self.prs[0], number=902))
        result = self.status(self.monitor())
        self.assertFalse(any(row["id"] == "amd" for row in result["backends"]))
        self.assertTrue(any("Multiple open amd" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
