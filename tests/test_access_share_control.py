import contextlib
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fl_dashboard.services.access_shares import AccessShareStore
from fl_dashboard.services import flflow


class AccessShareControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccessShareStore(Path(self.temp.name) / "grants.json", "https://dashboard.example.test:8767")
        self.server = flflow.make_server(None, "127.0.0.1", 0, access_shares=self.store)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(5)

    def request(self, path, body=None, headers=None):
        client = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json"} if body is not None else {}
        request_headers.update(headers or {})
        client.request("POST" if body is not None else "GET", path,
                       json.dumps(body) if body is not None else None, request_headers)
        response = client.getresponse()
        data = response.read()
        content = json.loads(data) if "application/json" in response.getheader("Content-Type", "") else data.decode()
        result = response.status, content
        client.close()
        return result

    def test_create_list_revoke_preserves_secrecy_and_cross_process_revocation(self):
        self.assertTrue(self.request("/api/capabilities")[1]["access_shares"])
        self.assertEqual(200, self.request("/access-shares")[0])
        code, created = self.request("/api/access-shares", {"label": "Collaborator", "expires_in_hours": 24})
        self.assertEqual(201, code)
        self.assertNotIn("token", created["share"])
        token = created["url"].split("#token=", 1)[1]
        reader = AccessShareStore(self.store.path, self.store.public_url)
        self.assertIsNotNone(reader.validate(token))
        code, listed = self.request("/api/access-shares")
        self.assertEqual(200, code)
        self.assertTrue(listed["available"])
        self.assertNotIn(token, json.dumps(listed))
        self.assertNotIn("token_hash", json.dumps(listed))
        self.assertEqual(1, len(listed["shares"]))
        code, revoked = self.request("/api/access-shares/" + created["share"]["id"] + "/revoke", {})
        self.assertEqual(200, code)
        self.assertFalse(revoked["share"]["active"])
        self.assertIsNone(reader.validate(token))

    def test_untrusted_host_origin_and_cross_site_cannot_mint_links(self):
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(403, self.request("/api/access-shares", {}, headers)[0])
        self.assertEqual([], self.store.list())

    def test_client_cannot_choose_destination_or_store_and_invalid_lifetime_is_rejected(self):
        for body in ({"public_url": "https://attacker.example"}, {"path": "/tmp/other"},
                     {"expires_in_hours": 0}, {"expires_in_hours": 169}, {"expires_in_hours": True}):
            with self.subTest(body=body):
                self.assertEqual(400, self.request("/api/access-shares", body)[0])
        self.assertEqual(400, self.request("/api/access-shares?project=vllm", {})[0])
        self.assertEqual([], self.store.list())

    def test_storage_errors_do_not_expose_private_diagnostics(self):
        with patch.object(self.store, "create", side_effect=OSError("PRIVATE_PATH_OR_KEY")):
            code, result = self.request("/api/access-shares", {})
        self.assertEqual(503, code)
        self.assertNotIn("PRIVATE_PATH_OR_KEY", json.dumps(result))

    def test_disabled_by_default_and_startup_requires_paired_settings(self):
        plain = flflow.make_server(None, "127.0.0.1", 0)
        worker = threading.Thread(target=plain.serve_forever, daemon=True)
        worker.start()
        try:
            client = HTTPConnection("127.0.0.1", plain.server_port, timeout=5)
            client.request("GET", "/api/capabilities")
            self.assertFalse(json.loads(client.getresponse().read())["access_shares"])
            client.request("POST", "/api/access-shares", "{}", {"Content-Type": "application/json"})
            response = client.getresponse()
            self.assertEqual(404, response.status)
            response.read()
            client.close()
        finally:
            plain.shutdown()
            plain.server_close()
            worker.join(5)
        for extra in (["--access-shares-file", "grants.json"], ["--share-public-url", "https://dashboard.example"]):
            with contextlib.redirect_stderr(io.StringIO()), patch.object(flflow.ProjectRegistry, "from_config") as registry:
                with self.assertRaises(SystemExit):
                    flflow.main(["serve", "--projects-config", "unused.json", *extra])
                registry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
