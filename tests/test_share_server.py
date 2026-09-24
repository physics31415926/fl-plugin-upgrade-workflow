import base64
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from share_server import SnapshotCache, make_server, sanitize_snapshot


def source_snapshot():
    return {
        "branch": "0.4.0-dev", "branch_ref": "refs/remotes/upstream/0.4.0-dev", "head": "a" * 40,
        "head_url": "https://github.com/flagos-ai/vllm-plugin-FL/commit/" + "a" * 40,
        "observed_at": "2026-09-24T10:00:00Z", "vllm_version": "0.18.0",
        "branches": ["0.4.0-dev", "main"], "models": ["Qwen3.6-27B"], "modes": ["eager"],
        "cases": [{"scenario": "text_single", "requests": 1, "file": "tools/adaptation-gate-cases/test_text.py", "test": "test_text_single"}],
        "branch_ci": {"status": "failed", "scope": "branch", "url": "https://github.com/flagos-ai/vllm-plugin-FL/actions/runs/123", "detail": "local path C:\\Users\\operator\\secrets"},
        "repo": "C:\\Users\\operator\\Documents\\GitHub\\vllm-plugin-FL", "source_thread": "private-thread-1",
        "errors": ["Git failed in /home/secure/plugin at 10.24.1.25"],
        "backends": [{"id": "cuda", "label": "NVIDIA", "ci_enabled": True,
                      "integration": {"status": "merged", "commit": "a" * 40},
                      "ci": {"status": "passed", "scope": "accepted-tree", "attempt": 2, "url": "https://github.com/flagos-ai/vllm-plugin-FL/actions/runs/122"},
                      "pr": {"number": 484, "title": "NVIDIA adaptation", "url": "https://github.com/flagos-ai/vllm-plugin-FL/pull/484"},
                      "gate": {"status": "recorded_pass", "passed": 20, "total": 20, "source_thread": "private-thread-2", "audit_sources": [{"machine": "devmachine", "path": "/secret/results"}], "run_id": "private-machine-at-time", "evidence_type": "audited_record", "raw_results_available": True},
                      "results": [{"model": "Qwen3.6-27B", "mode": "eager", "scenario": "text_single", "status": "recorded_pass", "requests": 1,
                                   "reason": "result from /data/private/results", "evidence_url": "/api/branch-evidence?secret=value", "provenance": {"ssh_alias": "devmachine"}}]}],
    }


class FakeMonitor:
    branch = "0.4.0-dev"

    def __init__(self):
        self.calls = []

    def status(self, branch=None):
        self.calls.append(("status", branch))
        value = source_snapshot()
        value["branch"] = branch or self.branch
        return value

    def refresh_branch(self, branch=None):
        self.calls.append(("refresh", branch))
        return self.status(branch)


class ShareServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.index = Path(self.temp.name) / "index.html"
        self.index.write_text("<html>readonly</html>", encoding="utf-8")
        self.monitor = FakeMonitor()
        self.cache = SnapshotCache(self.monitor, refresh_interval=0)
        self.server = None
        self.worker = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.worker.join(3)
        self.cache.close()
        self.temp.cleanup()

    def serve(self, **kwargs):
        self.server = make_server(self.cache, "127.0.0.1", 0, index_path=self.index, **kwargs)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def request(self, path, method="GET", authorization=None, host=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {}
        if authorization is not None:
            headers["Authorization"] = authorization
        if host:
            headers["Host"] = host
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    @staticmethod
    def auth(password="test-long-password", username="viewer"):
        return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    def test_public_projection_keeps_results_but_removes_private_metadata_recursively(self):
        original = source_snapshot()
        result = sanitize_snapshot(original)
        text = json.dumps(result)
        for private in ("private-thread", "devmachine", "Users", "secure", "10.24.1.25", "private/results", "provenance", "audit_sources", "source_thread", "branch-evidence", "private-machine"):
            self.assertNotIn(private, text)
        backend = result["backends"][0]
        self.assertEqual(backend["gate"]["passed"], 20)
        self.assertEqual(backend["ci"]["status"], "passed")
        self.assertIsNone(backend["results"][0]["evidence_url"])
        self.assertEqual(result["head_url"], original["head_url"])
        self.assertIn("C:\\Users", original["repo"])

    def test_untrusted_external_urls_and_nested_status_objects_are_not_exposed(self):
        source = source_snapshot()
        source["head_url"] = "https://github.com.evil.test/secret"
        source["backends"][0]["pr"]["url"] = "https://viewer:secret@github.com/owner/repo"
        source["backends"][0]["ci"]["status"] = {"source_thread": "private-id"}
        source["cases"][0]["file"] = "/secret/cases/test.py"
        result = sanitize_snapshot(source)
        self.assertIsNone(result["head_url"])
        self.assertIsNone(result["backends"][0]["pr"]["url"])
        self.assertIsNone(result["backends"][0]["ci"]["status"])
        self.assertIsNone(result["cases"][0]["file"])

    def test_auth_protects_all_reads_and_accepts_proxy_host(self):
        self.serve(password="test-long-password")
        for auth in (None, "Basic invalid", self.auth("wrong"), "Basic " + "x" * 5000):
            status, headers, _ = self.request("/healthz", authorization=auth)
            self.assertEqual(status, 401)
            self.assertIn("Basic", headers["WWW-Authenticate"])
        status, _, data = self.request("/api/branch-status", authorization=self.auth(), host="dashboard.example.internal")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["backends"][0]["gate"]["passed"], 20)

    def test_control_routes_and_raw_artifacts_are_denied(self):
        self.serve(password="test-long-password")
        for path in ("/sessions", "/workflow", "/api/codex-sessions", "/api/codex-machines", "/api/branch-evidence?branch=0.4.0-dev", "/api/logs", "/runs/gate-evidence/cuda/acceptance.json", "/../branch-monitor.json"):
            self.assertEqual(self.request(path, authorization=self.auth())[0], 404, path)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            status, headers, _ = self.request("/api/branch-refresh", method=method, authorization=self.auth())
            self.assertEqual(status, 405)
            self.assertEqual(headers["Allow"], "GET, HEAD")
        self.assertEqual(self.monitor.calls, [("status", None)])

    def test_prefixed_and_proxy_stripped_gets_return_capabilities(self):
        self.serve(base_path="/fl/dashboard")
        self.assertEqual(self.request("/fl/dashboard")[0], 308)
        for prefix in ("", "/fl/dashboard"):
            self.assertEqual(self.request(prefix + "/")[0], 200)
            status, _, data = self.request(prefix + "/api/capabilities")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data), {"read_only": True, "sessions": False, "refresh": False, "evidence": False, "default_branch": "0.4.0-dev"})
            self.assertEqual(self.request(prefix + "/api/branch-status?branch=0.4.0-dev")[0], 200)
        self.assertEqual(self.request("/other/api/branch-status")[0], 404)

    def test_cached_status_rejects_arbitrary_refs_without_git_fetch(self):
        self.serve()
        self.assertEqual(self.request("/api/branch-status?branch=../../private")[0], 400)
        self.assertEqual(self.request("/api/branch-status?branch=main")[0], 200)
        self.assertEqual(self.request("/api/branch-status?branch=main")[0], 200)
        self.assertEqual(self.monitor.calls, [("status", None), ("status", "main")])

    def test_non_loopback_requires_password_or_explicit_trusted_ingress(self):
        with self.assertRaises(ValueError):
            make_server(self.cache, "0.0.0.0", 0, index_path=self.index)
        server = make_server(self.cache, "0.0.0.0", 0, trusted_ingress=True, index_path=self.index)
        server.server_close()
        with self.assertRaises(ValueError):
            make_server(self.cache, "127.0.0.1", 0, password="", index_path=self.index)

    def test_bad_runtime_errors_do_not_return_local_paths(self):
        self.serve()
        self.cache.allowed_branches.add("bad")
        def failing_status(branch=None):
            raise RuntimeError("secret /home/private C:\\Users\\operator 192.168.1.20")
        self.monitor.status = failing_status
        status, _, data = self.request("/api/branch-status?branch=bad")
        self.assertEqual(status, 503)
        self.assertNotIn(b"private", data)
        self.assertNotIn(b"Users", data)

    def test_background_refresh_replaces_cache_with_sanitized_data(self):
        class OneCycle:
            def __init__(self):
                self.cycles = 0

            def wait(self, _interval):
                self.cycles += 1
                return self.cycles > 1

            def is_set(self):
                return False

            def set(self):
                pass

        self.cache._stop = OneCycle()
        self.cache._refresh()
        self.assertIn(("refresh", "0.4.0-dev"), self.monitor.calls)
        self.assertNotIn("source_thread", json.dumps(self.cache.get()))


if __name__ == "__main__":
    unittest.main()
