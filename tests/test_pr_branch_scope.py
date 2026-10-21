"""Configured and discovered PRs must belong to the selected target branch."""
from __future__ import annotations

import time
import unittest
from urllib.parse import parse_qs, urlsplit

import test_branch_monitor as fixtures
from fl_dashboard.monitoring.branch_monitor import BranchMonitor


class PRBranchScopeTests(unittest.TestCase):
    setUp = fixtures.BranchMonitorTests.setUp
    _write = fixtures.BranchMonitorTests._write
    _transport = fixtures.BranchMonitorTests._transport
    _backend = fixtures.BranchMonitorTests._backend
    _adaptation_pr = fixtures.BranchMonitorTests._adaptation_pr
    _discovery_transport = fixtures.BranchMonitorTests._discovery_transport

    def _config(self, engine="vllm"):
        return dict(self.config, engine=engine, github_repo=f"example/{engine}-plugin",
                    backend_prs={"thead": 901}, merged_commits={}, backend_labels={"thead": "T-Head"})

    def _selected_branch(self, name="next-release"):
        # Identical ancestry must not make a PR for another release eligible.
        fixtures.git(self.repo, "update-ref", f"refs/remotes/upstream/{name}", self.head)
        return name

    def test_configured_pr_for_another_branch_cannot_supply_links_or_results(self):
        selected = self._selected_branch()
        for engine in ("vllm", "sglang"):
            for state in ("open", "closed"):
                with self.subTest(engine=engine, state=state):
                    old = self._adaptation_pr("thead", state=state, merged=state == "closed",
                        merge_commit_sha=self.head, body="## Tests\n- Runtime: PASS")
                    transport, calls = self._discovery_transport([], {901: old})
                    row = self._backend(BranchMonitor(self._config(engine), transport=transport)
                                        .status(branch=selected, force=True), "thead")
                    self.assertIsNone(row["pr"])
                    self.assertEqual(row["integration"]["status"], "unknown")
                    self.assertEqual(row["ci"]["status"], "unknown")
                    self.assertEqual(row["acceptance_records"], [])
                    self.assertIsNone(row["acceptance"]["pr_number"])
                    self.assertEqual(row["acceptance"]["rows"], [])
                    self.assertFalse(any("/check-runs" in url for url in calls))

    def test_switching_branches_keeps_cached_pr_ci_and_acceptance_separate(self):
        selected = self._selected_branch()
        for engine in ("vllm", "sglang"):
            with self.subTest(engine=engine):
                old = self._adaptation_pr("thead", body="## Tests\n- Runtime: FAIL")
                new = self._adaptation_pr("thead", 902, base={"ref": selected},
                    head={"ref": "codex/backend-thead-next", "sha": "c" * 40},
                    body="## Tests\n- Runtime: PASS")
                calls = []
                def transport(url):
                    calls.append(url)
                    if "/pulls?" in url:
                        return [old, new]  # Defensively filter even an unscoped API response.
                    if "/check-runs" in url:
                        conclusion = "success" if new["head"]["sha"] in url else "failure"
                        return {"check_runs": [{"name": "test-thead / unit / Unit tests (thead)",
                            "status": "completed", "conclusion": conclusion}]}
                    for pr in (old, new):
                        if url.endswith(f"/pulls/{pr['number']}"):
                            return pr
                    return self._transport(url)
                monitor = BranchMonitor(self._config(engine), transport=transport)
                monitor._auto_refresh_times[selected] = time.monotonic()
                for branch, expected, status in ((monitor.branch, 901, "failed"),
                                                  (selected, 902, "passed"),
                                                  (monitor.branch, 901, "failed")):
                    row = self._backend(monitor.status(branch=branch), "thead")
                    self.assertEqual(row["pr"]["number"], expected)
                    self.assertEqual(row["ci"]["status"], status)
                    self.assertEqual(row["acceptance"]["pr_number"], expected)
                    self.assertEqual(row["acceptance"]["status"], status)
                bases = [parse_qs(urlsplit(url).query)["base"][0] for url in calls if "/pulls?" in url]
                self.assertEqual(bases, [monitor.branch, selected])
                self.assertEqual(monitor.backend_prs, {"thead": 901})

    def test_discovery_outage_cannot_restore_configured_pr_from_another_branch(self):
        selected = self._selected_branch()
        def transport(url):
            if "/pulls?" in url:
                raise OSError("offline")
            if url.endswith("/pulls/901"):
                return self._adaptation_pr("thead")
            return self._transport(url)
        status = BranchMonitor(self._config(), transport=transport).status(branch=selected, force=True)
        self.assertIsNone(self._backend(status, "thead")["pr"])
        self.assertTrue(any("discovery unavailable" in error for error in status["errors"]))

    def test_unverified_configured_pr_details_do_not_create_a_reference(self):
        for details in ({"number": 901},
                        self._adaptation_pr("thead", 999),
                        OSError("offline")):
            with self.subTest(details=type(details).__name__):
                def transport(url):
                    if url.endswith("/pulls/901"):
                        if isinstance(details, Exception):
                            raise details
                        return details
                    return self._transport(url)
                row = self._backend(BranchMonitor(self._config(), transport=transport).status(), "thead")
                self.assertIsNone(row["pr"])
                self.assertEqual(row["acceptance_records"], [])

    def test_failed_or_retargeted_replacement_cannot_fall_back_to_wrong_branch(self):
        selected = self._selected_branch()
        candidate = self._adaptation_pr("thead", 902, base={"ref": selected})
        for replacement in (OSError("offline"), self._adaptation_pr("thead", 902)):
            with self.subTest(replacement=type(replacement).__name__):
                def transport(url):
                    if "/pulls?" in url:
                        return [candidate]
                    if url.endswith("/pulls/901"):
                        return self._adaptation_pr("thead")
                    if url.endswith("/pulls/902"):
                        if isinstance(replacement, Exception):
                            raise replacement
                        return replacement
                    return self._transport(url)
                status = BranchMonitor(self._config(), transport=transport).status(branch=selected, force=True)
                self.assertIsNone(self._backend(status, "thead")["pr"])
                self.assertTrue(status["errors"])

    def test_ambiguous_current_candidates_cannot_show_other_branch_configured_pr(self):
        selected = self._selected_branch()
        candidates = [self._adaptation_pr("thead", number, base={"ref": selected}) for number in (902, 903)]
        transport, _ = self._discovery_transport(candidates, {901: self._adaptation_pr("thead")})
        status = BranchMonitor(self._config(), transport=transport).status(branch=selected, force=True)
        self.assertIsNone(self._backend(status, "thead")["pr"])
        self.assertTrue(any("Multiple open thead" in error for error in status["errors"]))

    def test_refresh_removes_a_pr_retargeted_to_another_release(self):
        pr = self._adaptation_pr("thead")
        transport, _ = self._discovery_transport([pr])
        monitor = BranchMonitor(self._config(), transport=transport)
        self.assertEqual(self._backend(monitor.status(), "thead")["pr"]["number"], 901)
        pr["base"] = {"ref": "another-release"}
        self.assertIsNone(self._backend(monitor.status(force=True), "thead")["pr"])


if __name__ == "__main__":
    unittest.main()
