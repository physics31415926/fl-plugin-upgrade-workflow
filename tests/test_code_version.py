"""The dashboard reports when it runs code older than the checkout, and restarts itself."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

from fl_dashboard.services import code_version, flflow
from fl_dashboard.services.code_version import CodeVersion


class CodeVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "fl_dashboard").mkdir()
        self.module = self.root / "fl_dashboard" / "a.py"
        self.module.write_text("x = 1\n")
        old = time.time() - 100
        os.utime(self.module, (old, old))
        self.heads = ["a" * 40]

    def tearDown(self):
        self.tmp.cleanup()

    def version(self):
        return CodeVersion(self.root, head=lambda _root: self.heads[-1], ttl=0)

    def test_fresh_process_is_not_stale(self):
        snap = self.version().snapshot()
        self.assertFalse(snap["stale"])
        self.assertEqual(snap["started_head"], "a" * 40)
        self.assertEqual(snap["changed_count"], 0)
        self.assertTrue(snap["can_restart"])

    def test_moved_head_is_stale(self):
        version = self.version()
        self.heads.append("b" * 40)
        snap = version.snapshot()
        self.assertTrue(snap["head_moved"])
        self.assertTrue(snap["stale"])

    def test_edited_source_is_stale_and_listed(self):
        version = self.version()
        future = time.time() + 5
        os.utime(self.module, (future, future))
        snap = version.snapshot()
        self.assertFalse(snap["head_moved"])
        self.assertTrue(snap["stale"])
        self.assertEqual(snap["changed_files"], ["fl_dashboard/a.py"])

    def test_bytecode_cache_and_non_python_files_are_ignored(self):
        version = self.version()
        future = time.time() + 5
        for name in ("__pycache__/a.cpython-313.pyc", "notes.md"):
            path = self.root / "fl_dashboard" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("")
            os.utime(path, (future, future))
        self.assertFalse(version.snapshot()["stale"])

    def test_unknown_head_alone_is_not_stale(self):
        self.heads[-1] = None
        version = self.version()
        self.heads.append("b" * 40)
        self.assertFalse(version.snapshot()["head_moved"])

    def test_git_head_rejects_non_hash_output(self):
        result = mock.Mock(returncode=0, stdout="not a hash\n")
        with mock.patch.object(code_version.subprocess, "run", return_value=result):
            self.assertIsNone(code_version.git_head(self.root))

    def test_respawn_reuses_original_arguments(self):
        with mock.patch.object(code_version.subprocess, "Popen") as popen:
            code_version.respawn(["-u", "scripts/flflow.py", "serve", "--port", "8766"])
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:], ["-u", "scripts/flflow.py", "serve", "--port", "8766"])


class RestartRouteTests(unittest.TestCase):
    def setUp(self):
        self.server = flflow.make_server(None, "127.0.0.1", 0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = None if body is None else json.dumps(body)
        base = {"Host": f"127.0.0.1:{self.port}"}
        if method == "POST":
            base["Content-Type"] = "application/json"
        conn.request(method, path, payload, {**base, **(headers or {})})
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def test_code_version_route_reports_state(self):
        status, body = self.request("GET", "/api/code-version")
        self.assertEqual(status, 200)
        self.assertIn("stale", body)
        self.assertEqual(body["pid"], os.getpid())

    def test_restart_rejects_cross_site_and_fields(self):
        status, _ = self.request("POST", "/api/restart", {}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/restart", {"argv": ["x"]})
        self.assertEqual(status, 400)
        self.assertFalse(self.server.restart_requested)

    def test_restart_stops_serving_and_flags_respawn(self):
        status, body = self.request("POST", "/api/restart", {})
        self.assertEqual((status, body["accepted"]), (202, True))
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.server.restart_requested)

    def test_share_gateway_never_forwards_restart(self):
        from fl_dashboard.services.share_gateway import GET_ROUTES, POST_ROUTES, PUBLIC_DEVICE_GET_ROUTES
        self.assertNotIn("/api/restart", POST_ROUTES)
        self.assertNotIn("/api/code-version", GET_ROUTES | PUBLIC_DEVICE_GET_ROUTES)


class ServeRespawnTests(unittest.TestCase):
    def test_serve_respawns_only_when_restart_was_requested(self):
        for requested in (False, True):
            server = mock.Mock(server_port=1, restart_requested=requested)
            with mock.patch.object(flflow, "make_server", return_value=server), \
                    mock.patch.object(code_version, "respawn") as respawn:
                flflow.serve(None, "127.0.0.1", 0)
            self.assertEqual(respawn.called, requested)


if __name__ == "__main__":
    unittest.main()
