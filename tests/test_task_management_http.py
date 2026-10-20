"""HTTP boundaries for reversible dashboard task management."""
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from fl_dashboard.projects.project_registry import ProjectContext, ProjectRegistry
from fl_dashboard.services.flflow import make_server
from fl_dashboard.services.share_server import ProjectSnapshotCache, make_server as make_share_server


SESSION_ID = "a" * 32


class RecordingManager:
    """A non-executing store so these requests cannot touch real task data."""

    def __init__(self, project):
        self.project = project
        self.calls = []
        self.deleted = False

    def _manage(self, action, session_id):
        self.calls.append((action, session_id))
        self.deleted = action == "delete"
        return {"id": session_id, "project_id": self.project, "provider": "codex",
                "status": "completed", "deleted": self.deleted,
                "deleted_at": "2026-01-01T00:00:00Z" if self.deleted else None}

    def delete(self, session_id):
        return self._manage("delete", session_id)

    def restore(self, session_id):
        return self._manage("restore", session_id)


class TaskManagementHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.managers = {project: RecordingManager(project) for project in ("vllm", "sglang")}
        self.registry = ProjectRegistry([
            ProjectContext(project, project, engine_name=project, sessions=manager)
            for project, manager in self.managers.items()
        ])
        self.server = None
        self.cache = None
        self.worker = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.worker.join(3)
        if self.cache is not None:
            self.cache.close()
        self.temp.cleanup()

    def serve(self, *, shared=False):
        if shared:
            self.cache = ProjectSnapshotCache(self.registry, refresh_interval=0)
            index = Path(self.temp.name) / "index.html"
            index.write_text("<html>Read-only fixture</html>", encoding="utf-8")
            self.server = make_share_server(self.cache, "127.0.0.1", 0,
                                           index_path=index, base_path="/fl")
        else:
            self.server = make_server(None, "127.0.0.1", 0, registry=self.registry)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.worker.start()

    def request(self, path, body, *, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            request_headers = {"Content-Type": "application/json", **(headers or {})}
            connection.request("POST", path, json.dumps(body), request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def assert_no_management_calls(self):
        for manager in self.managers.values():
            self.assertEqual(manager.calls, [])
            self.assertFalse(manager.deleted)

    def test_delete_and_restore_route_only_to_the_selected_project(self):
        self.serve()
        for project in self.managers:
            other = "sglang" if project == "vllm" else "vllm"
            prior_other_calls = list(self.managers[other].calls)
            for action in ("delete", "restore"):
                with self.subTest(project=project, action=action):
                    code, result = self.request(
                        f"/api/codex-sessions/{SESSION_ID}/{action}?project={project}", {})
                    self.assertEqual(code, 200)
                    self.assertEqual(result["id"], SESSION_ID)
                    self.assertEqual(result["project_id"], project)
                    self.assertEqual(result["deleted"], action == "delete")
                    self.assertEqual(self.managers[project].calls[-1], (action, SESSION_ID))
                    self.assertEqual(self.managers[other].calls, prior_other_calls)
            self.assertEqual(self.managers[project].calls,
                             [("delete", SESSION_ID), ("restore", SESSION_ID)])

    def test_management_requires_an_empty_json_object(self):
        self.serve()
        invalid_bodies = [None, [], "", 1, True, {"force": True},
                          {"provider": "claude"}, {"worktree": "/fixture/worktree"},
                          {"project": "vllm"}, {"deleted": False}]
        for action in ("delete", "restore"):
            for body in invalid_bodies:
                with self.subTest(action=action, body=body):
                    code, result = self.request(
                        f"/api/codex-sessions/{SESSION_ID}/{action}?project=sglang", body)
                    self.assertEqual(code, 400)
                    self.assertIn("error", result)
        self.assert_no_management_calls()

    def test_native_and_non_dashboard_ids_cannot_reach_management(self):
        self.serve()
        invalid_ids = ["native-thead", "00000000-1111-2222-3333-444444444444",
                       "A" * 32, "a" * 31, "a" * 33]
        for action in ("delete", "restore"):
            for session_id in invalid_ids:
                with self.subTest(action=action, session_id=session_id):
                    code, result = self.request(
                        f"/api/codex-sessions/{session_id}/{action}?project=sglang", {})
                    self.assertEqual(code, 400)
                    self.assertIn("error", result)
        self.assert_no_management_calls()

    def test_ambiguous_and_unknown_projects_cannot_reach_management(self):
        self.serve()
        for action in ("delete", "restore"):
            for query in ("project=missing", "project=vllm&project=sglang",
                          "project=sglang&project=sglang"):
                with self.subTest(action=action, query=query):
                    code, result = self.request(
                        f"/api/codex-sessions/{SESSION_ID}/{action}?{query}", {})
                    self.assertEqual(code, 400)
                    self.assertIn("error", result)
        self.assert_no_management_calls()

    def test_untrusted_host_origin_and_cross_site_requests_are_rejected(self):
        self.serve()
        port = self.server.server_port
        untrusted = [{"Host": f"untrusted.example:{port}"},
                     {"Host": "127.0.0.1:1"},
                     {"Origin": f"http://untrusted.example:{port}"},
                     {"Origin": "null"},
                     {"Origin": f"http://127.0.0.1:{port + 1}"},
                     {"Sec-Fetch-Site": "cross-site"}]
        for action in ("delete", "restore"):
            for headers in untrusted:
                with self.subTest(action=action, headers=headers):
                    code, result = self.request(
                        f"/api/codex-sessions/{SESSION_ID}/{action}?project=sglang",
                        {}, headers=headers)
                    self.assertEqual(code, 403)
                    self.assertIn("error", result)
        self.assert_no_management_calls()
        origin = {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}
        for action in ("delete", "restore"):
            self.assertEqual(self.request(
                f"/api/codex-sessions/{SESSION_ID}/{action}?project=sglang",
                {}, headers=origin)[0], 200)
        self.assertEqual(self.managers["vllm"].calls, [])
        self.assertEqual(self.managers["sglang"].calls,
                         [("delete", SESSION_ID), ("restore", SESSION_ID)])

    def test_anonymous_shared_read_only_server_rejects_delete_and_restore(self):
        self.serve(shared=True)
        for project in self.managers:
            for action in ("delete", "restore"):
                with self.subTest(project=project, action=action):
                    code, result = self.request(
                        f"/fl/api/codex-sessions/{SESSION_ID}/{action}?project={project}", {})
                    self.assertEqual(code, 405)
                    self.assertIn("read-only", result["error"])
        self.assert_no_management_calls()


if __name__ == "__main__":
    unittest.main()
