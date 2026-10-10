import copy
import importlib.util
import json
from pathlib import Path
import shutil
import unittest

from pr_acceptance import acceptance_summary, parse_pr_description


def table(*rows):
    return "## Tests\n\n| Test | Result |\n| --- | --- |\n" + "\n".join(f"| {name} | {result} |" for name, result in rows)


class PRDescriptionTests(unittest.TestCase):
    def test_result_table_reports_pass_without_treating_chunk_or_issue_ids_as_fraction(self):
        body = table(("Unit suite", "122/122 tests passed"),
                     ("Streaming", "Text and image streaming passed; 206/467 chunks returned"),
                     ("Deep model", "Passed; prior 32-token failure case completed"),
                     ("Security", "Checks passed; alerts 819/820 fixed"),
                     ("Shared CI", "failed before model load"))
        result = parse_pr_description(body)
        self.assertEqual("passed", result["status"])
        self.assertEqual(4, result["counts"]["passed"])
        self.assertEqual(0, result["counts"]["failed"])

    def test_explicit_failure_counts_and_incomplete_test_counts_stay_visible(self):
        for result in ("3 passed, 1 failed", "PASS; 6/8 requests", "4/7", "部分通过，未执行全部测试"):
            with self.subTest(result=result):
                self.assertEqual("partial", parse_pr_description(table(("Concurrency", result)))["status"])
        self.assertEqual("failed", parse_pr_description(table(("Runtime", "FAIL; server startup failed")))["status"])

    def test_passed_scope_with_not_rerun_or_not_covered_is_partial(self):
        body = table(("Benchmark", "24/24 runs passed; the full matrix was not rerun on the published image"),
                     ("Not covered", "Two-node validation"),
                     ("CI", "passed; a separate CI job failed"))
        result = parse_pr_description(body)
        self.assertEqual("partial", result["status"])
        self.assertEqual({"passed": 0, "partial": 1, "failed": 0, "pending": 1}, result["counts"])

    def test_nonblocking_diagnostic_and_byte_match_do_not_override_required_results(self):
        body = table(("Examples", "PASS; all semantic validations"), ("MTP", "PASS; 12/12 content checks"))
        body += "\n\nThe byte-for-byte match was 8/12; a control also matched 8/12 while both runs passed all 12 semantic checks.\n"
        body += "For both formal runs, the master completed all checks. This is not an inference or communication failure after successful inference.\n"
        body += "## Known non-blocking follow-ups\nAdditional diagnostic passed only 6/8 requests and is not counted as acceptance.\n"
        self.assertEqual("passed", parse_pr_description(body)["status"])

    def test_ci_badges_tables_commands_and_hidden_template_are_not_experimental_results(self):
        bodies = ["## CI\n| Test | Result |\n|---|---|\n| CUDA | PASS |",
                  "CI passed; all checks are green", "## Changes\n| Area | Description |\n|Validation|Keep failed logs|",
                  "## Tests\n```sh\necho 'tests passed'\n```\n",
                  "<!-- ## Tests\n- validation passed -->", "## Summary\n| Component | Status |\n|---|---|\n| Feature | passed |"]
        for body in bodies:
            with self.subTest(body=body):
                self.assertIsNone(parse_pr_description(body))

    def test_nonleading_ci_only_prose_is_not_experimental_acceptance(self):
        body = "## Validation\nAll GitHub Actions tests passed."
        self.assertIsNone(parse_pr_description(body))

    def test_ci_only_result_column_is_not_experimental_acceptance(self):
        self.assertIsNone(parse_pr_description(table(("Runtime", "CI passed; all checks are green"))))

    def test_primary_pass_does_not_hide_a_current_model_failure(self):
        result = parse_pr_description(table(("Runtime", "PASS; the second model failed")))
        self.assertEqual("partial", result["status"])

    def test_previous_success_does_not_hide_a_contrasting_current_failure(self):
        result = parse_pr_description(table(("Runtime", "PASS; previous build succeeded but current runtime failed")))
        self.assertEqual("partial", result["status"])

    def test_not_tested_configuration_scope_is_partial(self):
        body = ("## Tests\n| Test | Configuration | Result |\n|---|---|---|\n"
                "| Models | TP2 executed; TP4 not tested | PASS |")
        self.assertEqual("partial", parse_pr_description(body)["status"])

    def test_four_backtick_fence_keeps_nested_three_backticks_inside_code(self):
        body = "## Tests\n````sh\n```\ntests passed\n```\n````\n"
        self.assertIsNone(parse_pr_description(body))

    def test_planned_unchecked_or_negated_tests_are_pending(self):
        for body in ("## Test plan\n- [ ] Run inference tests", "Tests not tested", "Tests should pass",
                     "## Tests\n- Expected result: PASS", "Validation has not passed", table(("Test", "not run"))):
            with self.subTest(body=body):
                result = parse_pr_description(body)
                self.assertIsNotNone(result)
                self.assertEqual("pending", result["status"])

    def test_summary_never_copies_body_commands_paths_tokens_or_model_details(self):
        body = table(("PRIVATE_HOST_MODEL", "passed; result stored at /private/machine/path with DUMMY_PRIVATE_TOKEN"))
        result = json.dumps(parse_pr_description(body))
        for private in ("PRIVATE_HOST_MODEL", "/private/machine/path", "DUMMY_PRIVATE_TOKEN", "body"):
            self.assertNotIn(private, result)
        self.assertIn("pr_description", result)

    def test_result_prose_completed_full_and_zero_failures_are_passed(self):
        self.assertEqual("passed", parse_pr_description("## Validation\n- Focused tests: 15 passed, 0 failed, 0 warnings")['status'])
        self.assertEqual("passed", parse_pr_description(table(("Examples", "all scripts completed in full")))['status'])

    def test_result_column_does_not_read_failures_from_command_or_test_name(self):
        body = "## Tests\n| Script | Configuration | Result |\n|---|---|---|\n|test_failed_recovery.py|run --fail-fast|PASS; EXIT:0|"
        self.assertEqual("passed", parse_pr_description(body)["status"])

    def test_empty_unknown_or_unbounded_body_is_safe(self):
        for body in (None, 12, {}, "", "No experiment result", "- [x] Added tests"):
            self.assertIsNone(parse_pr_description(body))
        self.assertIsNone(parse_pr_description("x" * 300000 + table(("Tests", "passed"))))


class AcceptanceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.pr = {"number": 17, "url": "https://github.com/example/plugin/pull/17"}
        self.data = {"number": 17, "base": {"ref": "target"}, "body": table(("Examples", "PASS"))}
        self.gate = {"status": "missing", "passed": 0, "total": 20}

    def summary(self, records=(), **changes):
        arguments = dict(pr=self.pr, pr_data=self.data, records=list(records), gate=self.gate, branch="target", head="a" * 40)
        arguments.update(changes)
        return acceptance_summary(**arguments)

    def test_pr_reports_have_priority_without_mutating_raw_gate_or_records(self):
        records = [{"pr_number": 17, "status": "failed"}]
        original = copy.deepcopy((self.pr, self.data, records, self.gate))
        result = self.summary(records)
        self.assertEqual("pr_description", result["source"])
        self.assertEqual("passed", result["status"])
        self.assertEqual((self.pr, self.data, records, self.gate), original)
        self.assertNotIn("body", result)

    def test_pending_or_failed_pr_is_not_replaced_with_old_pass(self):
        for status in ("not run", "failed"):
            data = {**self.data, "body": table(("Runtime", status))}
            result = self.summary([{"pr_number": 17, "status": "passed"}], pr_data=data)
            self.assertEqual("pr_description", result["source"])
            self.assertEqual("pending" if status == "not run" else "failed", result["status"])

    def test_wrong_branch_or_pr_number_cannot_supply_pr_results(self):
        for data in ({**self.data, "base": {"ref": "other"}}, {**self.data, "number": 18}):
            self.assertEqual("reference", self.summary(pr_data=data)["status"])

    def test_missing_pr_data_number_cannot_supply_pr_results(self):
        data = {key: value for key, value in self.data.items() if key != "number"}
        result = self.summary(pr_data=data)
        self.assertEqual("reference", result["status"])
        self.assertEqual("pr_reference", result["source"])

    def test_matching_local_archive_fallback_has_safe_identity_and_link(self):
        record = {"pr_number": 17, "status": "passed", "archive_available": True,
                  "archive_url": "/api/acceptance-record?record=fixture", "recorded_at": "2026-01-01"}
        result = self.summary([record], pr_data=None)
        self.assertEqual("local_record", result["source"])
        self.assertTrue(result["archive_available"])
        self.assertEqual(record["archive_url"], result["archive_url"])
        self.assertEqual(17, result["pr_number"])
        self.assertEqual("reference", self.summary([{**record, "pr_number": 16}], pr_data=None)["status"])

    def test_current_gate_fallback_stays_version_bound_and_no_pr_is_not_passed(self):
        gate = {"status": "passed", "passed": 20, "total": 20, "latest_commit": "a" * 40}
        self.assertEqual("current_gate", self.summary(pr=None, pr_data=None, gate=gate)["source"])
        self.assertEqual("passed", self.summary(pr=None, pr_data=None, gate=gate)["status"])
        self.assertEqual("missing", self.summary(pr=None, pr_data=None, gate={**gate, "latest_commit": "b" * 40})["status"])


@unittest.skipUnless(shutil.which("git"), "Git is required for monitor fixtures")
class AcceptanceIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Load only the neighboring fixture; do not depend on an unrelated
        # installed package named tests or on test discovery import ordering.
        specification = importlib.util.spec_from_file_location("acceptance_fixture", Path(__file__).with_name("test_acceptance_records.py"))
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        self.fixture = module.AcceptanceMonitorIntegrationTests("test_source_tree_difference_remains_visible_on_historical_record")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def with_body(self, body):
        original = self.fixture.transport
        def transport(url):
            data = original(url)
            if url.endswith("/pulls/487"):
                return {**data, "body": body}
            return data
        self.fixture.monitor.transport = transport

    def test_monitor_uses_existing_pr_body_and_preserves_raw_missing_results(self):
        self.with_body(table(("Runtime", "PASS; 20/20 tests")))
        backend = self.fixture.status()
        self.assertEqual("passed", backend["acceptance"]["status"])
        self.assertEqual("pr_description", backend["acceptance"]["source"])
        self.assertEqual("missing", backend["gate"]["status"])
        self.assertEqual(0, backend["gate"]["passed"])
        self.assertEqual(1, len([url for url in self.fixture.requests if url.endswith("/pulls/487")]))
        self.assertNotIn("body", backend["pr"])

    def test_force_refresh_updates_description_without_rerunning_experiments(self):
        self.with_body(table(("Runtime", "not run")))
        self.assertEqual("pending", self.fixture.status()["acceptance"]["status"])
        self.with_body(table(("Runtime", "PASS")))
        backend = next(item for item in self.fixture.monitor.status(force=True)["backends"] if item["id"] == "ascend")
        self.assertEqual("passed", backend["acceptance"]["status"])
        self.assertEqual("missing", backend["gate"]["status"])


if __name__ == "__main__":
    unittest.main()
