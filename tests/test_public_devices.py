"""Public resource views use cached telemetry, never the task-control surface."""
from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPConnection
import io
import json
import threading
import unittest
from unittest.mock import patch

from fl_dashboard.services.public_devices import public_snapshot
from fl_dashboard.services.share_server import SnapshotCache, make_server, main
from test_share_server import FakeMonitor


class PublicDeviceTests(unittest.TestCase):
    def test_projection_only_preserves_display_metrics_at_every_level(self):
        result = public_snapshot({"project_id": "vllm", "ssh_profiles": "private", "summary": {"free_cards": 1, "config": "private"},
            "hosts": [{"id": "device", "label": "GPU", "ssh_connection": "private", "source_root": "private",
                       "node": {"cpu_pct": 10, "command": "private"},
                       "accel": {"total": 1, "private": "private", "cards": [{"index": 0, "util": 0, "secret": "private",
                           "procs": [{"pid": 123, "name": "python", "user": "operator", "cmdline": "private", "token": "private"}]}]}}]}, "vllm")
        self.assertTrue(result["read_only"])
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(result["hosts"][0]["accel"]["cards"][0]["procs"][0]["pid"], 123)
        for value in ({}, {"project_id": "sglang", "hosts": []}, {"project_id": "vllm", "hosts": {}}):
            with self.assertRaises(ValueError):
                public_snapshot(value, "vllm")

    def test_readonly_controller_source_does_not_require_a_share_store(self):
        cache = SnapshotCache(FakeMonitor(), refresh_interval=0)
        server = make_server(cache, "127.0.0.1", 0, control_url="http://127.0.0.1:8766")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def request(path, method="GET"):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request(method, path, body="{}" if method == "POST" else None)
            response = conn.getresponse()
            result = response.status, json.loads(response.read())
            conn.close()
            return result
        try:
            capabilities = request("/api/capabilities")[1]
            self.assertTrue(capabilities["devices"])
            self.assertTrue(capabilities["read_only"])
            self.assertFalse(capabilities["devices_refresh"])
            self.assertFalse(capabilities["sessions"])
            with patch("fl_dashboard.services.share_server.read_public_devices", return_value={"project_id": "vllm", "hosts": [], "read_only": True}) as read:
                self.assertEqual(200, request("/api/devices?project=vllm")[0])
                read.assert_called_once_with("http://127.0.0.1:8766", "vllm")
                self.assertEqual(405, request("/api/devices/refresh", "POST")[0])
                self.assertEqual(405, request("/api/codex-sessions", "POST")[0])
                self.assertEqual(read.call_count, 1)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)
            cache.close()

    def test_cli_accepts_readonly_control_source_without_share_management(self):
        with patch("fl_dashboard.services.share_server.ProjectRegistry.from_config"), \
                patch("fl_dashboard.services.share_server.ProjectSnapshotCache") as cache, \
                patch("fl_dashboard.services.share_server.make_server") as factory, redirect_stdout(io.StringIO()):
            main(["--projects-config", "unused.json", "--control-url", "http://127.0.0.1:8766"])
            self.assertIsNone(factory.call_args.kwargs["access_shares"])
            self.assertEqual(factory.call_args.kwargs["control_url"], "http://127.0.0.1:8766")
            cache.return_value.close.assert_called_once()
        with patch("fl_dashboard.services.share_server.ProjectRegistry.from_config") as registry, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--projects-config", "unused.json", "--control-url", "http://external.invalid:8766"])
            registry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
