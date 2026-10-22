from http.client import HTTPConnection
import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from fl_dashboard.services import flflow


class DeviceRouteTests(unittest.TestCase):
    def setUp(self):
        self.fleet = Mock()
        self.fleet.snapshot.return_value = {"project_id": "vllm", "hosts": [], "refreshing": False}
        self.fleet.refresh.return_value = {"project_id": "vllm", "hosts": [], "refreshing": True}
        self.sessions = SimpleNamespace(snapshot=lambda **kw: {"sessions": [], "machine_profiles": []})
        inventory = Mock()
        inventory.snapshot.return_value = {"enabled": False}
        with patch("fl_dashboard.resources.fleet.DeviceFleet", return_value=self.fleet), \
                patch("fl_dashboard.resources.jumpserver.JumpServerInventory", return_value=inventory), \
                patch("fl_dashboard.resources.jumpserver_profiles.JumpServerProfiles"), \
                patch("fl_dashboard.tasks.ssh_profile_sync.SSHProfileSync"):
            self.server = flflow.make_server(None, "127.0.0.1", 0, sessions=self.sessions)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, body=None, headers=None):
        client = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        values = {"Content-Type": "application/json"} if body is not None else {}
        values.update(headers or {})
        client.request("POST" if body is not None else "GET", path,
                       None if body is None else json.dumps(body), values)
        response = client.getresponse()
        raw = response.read()
        data = json.loads(raw) if "application/json" in response.getheader("Content-Type", "") else raw.decode()
        client.close()
        return response.status, data

    def test_status_is_cached_and_page_is_available_without_starting_collection(self):
        self.assertTrue(self.request("/api/capabilities")[1]["devices"])
        self.assertEqual(200, self.request("/devices")[0])
        self.assertEqual(200, self.request("/api/devices?project=vllm")[0])
        self.fleet.snapshot.assert_called_once_with("vllm")
        self.fleet.refresh.assert_not_called()

    def test_refresh_only_accepts_saved_profile_and_preserves_project_scope(self):
        code, data = self.request("/api/devices/refresh?project=vllm", {"profile_id": "ssh-demo"})
        self.assertEqual(202, code)
        self.assertTrue(data["refreshing"])
        self.fleet.refresh.assert_called_once_with("vllm", profile_id="ssh-demo")
        self.fleet.refresh.reset_mock()
        for path, body in (("/api/devices/refresh", {"command": "untrusted"}),
                           ("/api/devices/refresh", {"profile_id": None}),
                           ("/api/devices/refresh", {"profile_id": []}),
                           ("/api/devices/refresh?host=other", {}),
                           ("/api/devices/refresh?project=missing", {})):
            with self.subTest(path=path, body=body):
                self.assertEqual(400, self.request(path, body)[0])
        self.fleet.refresh.assert_not_called()

    def test_untrusted_refresh_and_private_error_details_are_blocked(self):
        self.assertEqual(403, self.request("/api/devices/refresh", {}, {"Origin": "https://elsewhere.invalid"})[0])
        self.fleet.refresh.assert_not_called()
        self.fleet.snapshot.side_effect = RuntimeError("PRIVATE_SSH_DIAGNOSTIC")
        status, body = self.request("/api/devices")
        self.assertEqual(503, status)
        self.assertNotIn("PRIVATE_SSH_DIAGNOSTIC", json.dumps(body))

    def test_server_closes_collector(self):
        self.server.server_close()
        self.fleet.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
