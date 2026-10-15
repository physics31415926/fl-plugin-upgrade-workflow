import unittest
from fl_dashboard.monitoring.pr_acceptance import description_blocks, acceptance_summary
from fl_dashboard.services.share_server import sanitize_snapshot

class DescriptionDetailTests(unittest.TestCase):
    def test_preserves_unclassified_tables_and_limitations(self):
        body = "## Environment\n| Device | Version |\n| --- | --- |\n| GPU | vendor runtime |\n\n## Limitations\nThis diagnostic does not establish model correctness.\n\n## Testing\n| Scenario | Observed |\n| --- | --- |\n| Concurrency | 16 responses |"
        blocks = description_blocks(body)
        self.assertEqual(len([b for b in blocks if b["type"] == "table"]), 2)
        self.assertTrue(any("does not establish" in b.get("text","") for b in blocks))
        self.assertEqual(blocks[-1]["rows"][-1], ["Concurrency","16 responses"])

    def test_local_details_excluded_from_anonymous_projection(self):
        result = acceptance_summary({"number":1}, {"number":1,"base":{"ref":"main"},"body":"## Environment\nInternal context"}, [], {}, branch="main", head="a"*40)
        self.assertTrue(result["description_blocks"])
        public = sanitize_snapshot({"backends":[{"id":"cuda","acceptance":result}]})
        self.assertNotIn("description_blocks", public["backends"][0]["acceptance"])
        self.assertNotIn("Internal context", str(public))

    def test_redaction_and_no_html_execution(self):
        blocks = description_blocks("## Test\npassword=example-secret\n<script>alert(1)</script>\n"+chr(96)*3+"sh\nssh private-host\n"+chr(96)*3)
        text = str(blocks)
        self.assertNotIn("example-secret",text)
        self.assertNotIn("<script>",text)
        self.assertNotIn("private-host",text)
