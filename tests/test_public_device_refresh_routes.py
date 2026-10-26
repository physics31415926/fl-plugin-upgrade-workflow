"""LAN refresh accepts only a same-origin fixed resource operation."""
from http.client import HTTPConnection
import json
import threading
import unittest
from unittest.mock import Mock, patch

from fl_dashboard.services.share_server import SnapshotCache, make_server
from test_share_server import FakeMonitor


class PublicRefreshRouteTests(unittest.TestCase):
    def setUp(self):
        self.cache = SnapshotCache(FakeMonitor(), refresh_interval=0)
        self.refresher = Mock()
        self.refresher.refresh.return_value = {"project_id": "vllm", "read_only": True,
                                               "device_refresh": True, "hosts": [],
                                               "public_refresh_retry_after": 30}
        self.refresher.retry_after.return_value = 17
        with patch("fl_dashboard.services.share_server.PublicDeviceRefresher", return_value=self.refresher):
            self.server = make_server(self.cache, "127.0.0.1", 0, control_url="http://127.0.0.1:8766")
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.authority = f"127.0.0.1:{self.server.server_port}"
        self.origin = "http://" + self.authority

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)
        self.cache.close()

    def request(self, path="/api/devices/refresh?project=vllm", *, raw=b"{}", headers=None, duplicate=(), method="POST"):
        values = {"Host": self.authority, "Origin": self.origin,
                  "Content-Type": "application/json", "Content-Length": str(len(raw))}
        values.update(headers or {})
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        try:
            connection.putrequest(method, path, skip_host=True)
            for key, value in [*values.items(), *duplicate]:
                if value is not None:
                    connection.putheader(key, value)
            connection.endheaders(raw)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_empty_request_refreshes_only_selected_project_without_changing_permissions(self):
        status, value = self.request()
        self.assertEqual(202, status)
        self.assertTrue(value["read_only"])
        self.assertTrue(value["device_refresh"])
        self.assertEqual(30, value["public_refresh_retry_after"])
        self.refresher.refresh.assert_called_once_with("vllm")

    def test_polling_reports_global_cooldown_without_starting_collection(self):
        with patch("fl_dashboard.services.share_server.read_public_devices", return_value={
            "project_id": "vllm", "read_only": True, "hosts": [], "device_refresh": True}):
            status, value = self.request("/api/devices?project=vllm", raw=b"", method="GET")
        self.assertEqual(200, status)
        self.assertEqual(17, value["public_refresh_retry_after"])
        self.refresher.refresh.assert_not_called()

    def test_missing_cross_origin_and_rebinding_headers_cannot_refresh(self):
        for headers in ({"Origin": None}, {"Origin": "http://external.invalid"},
                        {"Origin": "https://" + self.authority},
                        {"Origin": "http://untrusted.invalid", "Host": "untrusted.invalid"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(403, self.request(headers=headers)[0])
        for name, value in (("Origin", self.origin), ("Host", self.authority),
                            ("Sec-Fetch-Site", "same-origin")):
            with self.subTest(duplicate=name):
                base = {"Sec-Fetch-Site": "same-origin"} if name == "Sec-Fetch-Site" else {}
                self.assertEqual(403, self.request(headers=base, duplicate=[(name, value)])[0])
        self.refresher.refresh.assert_not_called()

    def test_query_and_payload_cannot_select_hosts_commands_or_other_routes(self):
        for query in ("project=missing", "project=", "project=vllm&project=vllm", "host=arbitrary", "project=vllm&path=/api/agent-settings"):
            with self.subTest(query=query):
                self.assertEqual(400, self.request("/api/devices/refresh?" + query)[0])
        for raw in (b'[]', b'null', b'invalid', b'{"profile_id":"device"}', b'{"host":"other"}', b'{"command":"anything"}'):
            with self.subTest(raw=raw):
                self.assertEqual(400, self.request(raw=raw)[0])
        for path in ("/api/codex-sessions", "/api/codex-machines", "/api/jumpserver/login", "/api/jumpserver/settings"):
            with self.subTest(path=path):
                self.assertEqual(405, self.request(path)[0])
        self.refresher.refresh.assert_not_called()

    def test_ambiguous_or_oversized_request_framing_is_rejected(self):
        for headers in ({"Content-Length": None}, {"Content-Length": "4097"},
                        {"Transfer-Encoding": "chunked"}, {"Content-Type": "text/plain"}):
            with self.subTest(headers=headers):
                self.assertEqual(400, self.request(headers=headers)[0])
        self.assertEqual(400, self.request(duplicate=[("Content-Length", "2")])[0])
        self.refresher.refresh.assert_not_called()

    def test_upstream_failures_do_not_expose_private_diagnostics(self):
        self.refresher.refresh.side_effect = RuntimeError("PRIVATE_CONNECTION_DIAGNOSTIC")
        status, value = self.request()
        self.assertEqual(503, status)
        self.assertNotIn("PRIVATE_CONNECTION_DIAGNOSTIC", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
