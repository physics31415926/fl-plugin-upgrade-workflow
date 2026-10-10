import json
import unittest

from pr_acceptance import detail_text, parse_pr_description
from share_server import sanitize_snapshot


class AcceptanceDetailTests(unittest.TestCase):
    def test_pr_table_retains_case_configuration_result_and_per_row_status(self):
        body = "## Examples validation\n| Script | Configuration | Result |\n|---|---|---|\n|example.py|TP=4 · eager|PASS; 16/16 requests|\n|Concurrency|TP=8 not tested|PASS; 64/64 requests|"
        result = parse_pr_description(body)
        self.assertEqual("partial", result["status"])
        self.assertEqual([{"label": "example.py", "configuration": "TP=4 · eager", "result": "PASS; 16/16 requests", "status": "passed"},
                          {"label": "Concurrency", "configuration": "TP=8 not tested", "result": "PASS; 64/64 requests", "status": "partial"}], result["rows"])
        self.assertEqual(2, result["rows_total"])
        self.assertFalse(result["rows_truncated"])

    def test_prose_results_are_visible_rows_without_html_or_executable_markdown(self):
        body = "## Tests\n- [x] Validation <b>passed</b>; test output reviewed"
        result = parse_pr_description(body)
        self.assertEqual("passed", result["status"])
        self.assertEqual("测试说明", result["rows"][0]["label"])
        self.assertNotIn("<", result["rows"][0]["result"])

    def test_cells_redact_private_paths_addresses_credentials_and_shell_commands(self):
        values = [r"PASS; C:\Users\private\secret.log", "PASS; /private/machine/output", "PASS; ~/identity.pem",
                  "PASS; host 192.168.33.22 fd00:abcd::1", "PASS; api_key=EXPLICIT_DUMMY_SECRET",
                  "PASS; [log](https://user:EXPLICIT_DUMMY_PASSWORD@example.test/private)",
                  "PASS; `ssh private_alias cat /private/key`", "PASS; sk-proj-EXPLICIT_DUMMY_TOKEN_123456789"]
        for value in values:
            with self.subTest(value=value):
                clean = detail_text(value)
                self.assertIn("PASS", clean)
                for private in (r"C:\Users", "/private/", "~/identity", "192.168.33.22", "fd00:abcd::1", "EXPLICIT_DUMMY_SECRET", "EXPLICIT_DUMMY_PASSWORD", "private_alias", "EXPLICIT_DUMMY_TOKEN"):
                    self.assertNotIn(private, clean)

    def test_rows_are_bounded_without_losing_aggregate_counts(self):
        body = "## Tests\n| Test | Result |\n|---|---|\n" + "\n".join(f"|case {n}|PASS|" for n in range(140))
        result = parse_pr_description(body)
        self.assertEqual(140, result["counts"]["passed"])
        self.assertEqual(140, result["rows_total"])
        self.assertEqual(100, len(result["rows"]))
        self.assertTrue(result["rows_truncated"])
        self.assertEqual(160, len(detail_text("x" * 1000, 160)))

    def test_lan_only_projects_safe_row_fields_and_never_exports_nested_provenance(self):
        row = {"label": "Example", "configuration": "TP=4", "result": "PASS; log /private/machine/output",
               "status": "passed", "raw_body": "PRIVATE_BODY", "command": "PRIVATE_COMMAND"}
        acceptance = {"status": "passed", "source": "pr_description", "rows": [row], "rows_total": 1,
                      "rows_truncated": False, "archive_url": "/private/archive", "body": "PRIVATE_PR_BODY"}
        snapshot = {"backends": [{"acceptance": acceptance}]}
        public = sanitize_snapshot(snapshot)["backends"][0]["acceptance"]
        self.assertEqual({"label", "configuration", "result", "status"}, set(public["rows"][0]))
        self.assertEqual(1, public["rows_total"])
        self.assertIsNone(public["archive_url"])
        self.assertNotIn("/private/", json.dumps(public))
        self.assertNotIn("PRIVATE_BODY", json.dumps(public))
        self.assertEqual("PASS; log /private/machine/output", row["result"], "projection does not mutate owner data")

    def test_lan_rejects_non_list_rows_and_limits_oversized_rows(self):
        for rows in (None, "PRIVATE", {"body": "PRIVATE"}):
            value = sanitize_snapshot({"backends": [{"acceptance": {"rows": rows}}]})
            self.assertEqual([], value["backends"][0]["acceptance"]["rows"])
        row = {"label": "x" * 900, "configuration": "x" * 900, "result": "x" * 900, "status": {"private": "value"}}
        result = sanitize_snapshot({"backends": [{"acceptance": {"rows": [row] * 140, "rows_total": 140}}]})
        rows = result["backends"][0]["acceptance"]["rows"]
        self.assertEqual(100, len(rows))
        self.assertEqual([160, 300, 400], [len(rows[0][field]) for field in ("label", "configuration", "result")])
        self.assertEqual("unknown", rows[0]["status"])


if __name__ == "__main__":
    unittest.main()
