import unittest
from fl_dashboard.monitoring.pr_acceptance import public_acceptance_tables, parse_pr_description

class PublicAcceptanceTests(unittest.TestCase):
    def test_only_validation_tables_are_exported_and_secrets_redacted(self):
        blocks = [
            {"type":"heading","text":"Environment"},
            {"type":"table","rows":[["private environment"]]},
            {"type":"heading","text":"Test Results"},
            {"type":"paragraph","text":"internal prose"},
            {"type":"table","rows":[["Test","Result"],["serving","PASS password=abc123 192.168.1.2 https://internal.example ssh://private C:/private/file sk-abcdefghijklmnop"]]},
            {"type":"heading","text":"Changes"},
            {"type":"table","rows":[["private implementation"]]}]
        output = public_acceptance_tables(blocks)
        self.assertEqual(len(output),1)
        text = str(output)
        for secret in ("abc123","192.168.1.2","internal.example","ssh://private","C:/private/file","sk-abcdefghijklmnop","internal prose","private environment","private implementation"):
            self.assertNotIn(secret,text)
        self.assertIn("PASS",text)

    def test_bullet_item_has_specific_label(self):
        value = parse_pr_description("## Tests\n- GPU inference: 12 passed")
        self.assertEqual(value["rows"][0]["label"], "GPU inference")
