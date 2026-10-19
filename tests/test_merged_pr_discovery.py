"""Discovered adaptation PRs remain visible after their merge and service restart."""

from __future__ import annotations

import unittest

import test_branch_monitor as fixtures
from fl_dashboard.monitoring.branch_monitor import BranchMonitor


class MergedPRDiscoveryTests(unittest.TestCase):
    # Reuse repository fixtures without inheriting and rerunning their test suite.
    setUp = fixtures.BranchMonitorTests.setUp
    _write = fixtures.BranchMonitorTests._write
    _transport = fixtures.BranchMonitorTests._transport
    _backend = fixtures.BranchMonitorTests._backend
    _adaptation_pr = fixtures.BranchMonitorTests._adaptation_pr
    _discovery_transport = fixtures.BranchMonitorTests._discovery_transport

    def _merged(self, number=901, **changes):
        return self._adaptation_pr(
            "thead", number, state="closed", merged=True, draft=False,
            merged_at="2026-10-19T06:37:53Z", merge_commit_sha=self.head,
            **changes,
        )

    def _config(self, engine="vllm"):
        return dict(self.config, engine=engine, backend_labels={"thead": "THead"})

    def test_open_to_merged_discovery_survives_restart_for_both_engines(self):
        for engine in ("vllm", "sglang"):
            with self.subTest(engine=engine):
                candidate = self._adaptation_pr("thead", 901)
                transport, calls = self._discovery_transport([candidate])
                monitor = BranchMonitor(self._config(engine), transport=transport)
                self.assertEqual(self._backend(monitor.status(), "thead")["integration"]["status"], "draft")
                candidate.update(self._merged())
                for instance in (monitor, BranchMonitor(self._config(engine), transport=transport)):
                    row = self._backend(instance.status(force=True), "thead")
                    self.assertEqual(row["pr"]["number"], 901)
                    self.assertEqual(row["pr"]["selected_by"], "discovered")
                    self.assertEqual(row["integration"]["status"], "merged")
                    self.assertEqual(row["integration"]["commit"], self.head)
                    self.assertEqual(row["ci"]["scope"], "merged-pr")
                self.assertTrue(all("state=all" in url for url in calls if "/pulls?" in url))

    def test_closed_unmerged_wrong_branch_and_unreachable_merge_are_ignored(self):
        candidates = [
            self._adaptation_pr("thead", 901, state="closed", merged=False),
            self._merged(902, base={"ref": "another-release"}),
            {**self._merged(903), "merge_commit_sha": "f" * 40},
        ]
        transport, calls = self._discovery_transport(candidates)
        row = self._backend(BranchMonitor(self._config(), transport=transport).status(), "thead")
        self.assertIsNone(row["pr"])
        self.assertEqual(row["integration"]["status"], "unknown")
        self.assertFalse(any(url.endswith(f"/pulls/{number}") for url in calls for number in (901, 902, 903)))

    def test_merged_candidate_details_must_still_prove_the_same_adaptation(self):
        listed = self._merged()
        for changes in (
            {"base": {"ref": "another-release"}},
            {"head": {"ref": "codex/backend-musa-demo", "sha": "b" * 40}},
            {"merged": False},
            {"merge_commit_sha": "f" * 40},
        ):
            with self.subTest(changes=changes):
                transport, _ = self._discovery_transport([listed], {901: {**listed, **changes}})
                status = BranchMonitor(self._config(), transport=transport).status()
                self.assertIsNone(self._backend(status, "thead")["pr"])
                self.assertTrue(any("no longer matches" in error for error in status["errors"]))

    def test_latest_merge_wins_over_later_comments_and_larger_pr_number(self):
        older = {**self._merged(902), "merged_at": "2026-10-18T08:00:00Z", "updated_at": "2026-10-19T10:00:00Z"}
        newer = {**self._merged(901), "updated_at": "2026-10-19T06:37:53Z"}
        for candidates in ([older, newer], [newer, older]):
            with self.subTest(order=[pr["number"] for pr in candidates]):
                transport, _ = self._discovery_transport(candidates)
                row = self._backend(BranchMonitor(self._config(), transport=transport).status(), "thead")
                self.assertEqual(row["pr"]["number"], 901)
                self.assertEqual(row["integration"]["status"], "merged")

    def test_open_adaptation_has_priority_over_prior_merged_history(self):
        candidates = [self._merged(901), self._adaptation_pr("thead", 902)]
        transport, _ = self._discovery_transport(candidates)
        row = self._backend(BranchMonitor(self._config(), transport=transport).status(), "thead")
        self.assertEqual(row["pr"]["number"], 902)
        self.assertEqual(row["integration"]["status"], "draft")

    def test_multiple_open_candidates_remain_ambiguous_despite_merged_history(self):
        candidates = [self._merged(901), self._adaptation_pr("thead", 902), self._adaptation_pr("thead", 903)]
        transport, _ = self._discovery_transport(candidates)
        status = BranchMonitor(self._config(), transport=transport).status()
        self.assertIsNone(self._backend(status, "thead")["pr"])
        self.assertTrue(any("Multiple open thead" in error for error in status["errors"]))


    def test_truncated_history_preserves_complete_open_discovery_with_warning(self):
        active = self._adaptation_pr("thead", 999)
        calls = []

        def transport(url):
            if "/pulls?" in url:
                calls.append(url)
                if "state=all" in url:
                    return [self._merged(number) for number in range(100, 200)]
                self.assertIn("state=open", url)
                return [active]
            return self._transport(url)

        errors = []
        monitor = BranchMonitor(self._config(), transport=transport)
        candidates = monitor._candidate_prs("0.4.0-dev", False, errors)
        self.assertEqual(candidates, [active])
        self.assertTrue(any("history" in message.lower() for message in errors))
        self.assertEqual(sum("state=all" in url for url in calls), 3)
        self.assertEqual(sum("state=open" in url for url in calls), 1)


if __name__ == "__main__":
    unittest.main()