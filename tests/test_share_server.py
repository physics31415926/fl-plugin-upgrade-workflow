import base64
from contextlib import redirect_stderr
from http.client import HTTPConnection, HTTPSConnection
import io
import json
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from share_server import SnapshotCache, load_tls_context, main, make_server, sanitize_snapshot


# Public explicit_dummy credentials created only for tests; never deploy these.
EXPLICIT_DUMMY_TLS_CERT = """-----BEGIN CERTIFICATE-----
MIIC5jCCAc6gAwIBAgIBATANBgkqhkiG9w0BAQsFADAeMRwwGgYDVQQDDBNleHBs
aWNpdF9kdW1teS50ZXN0MB4XDTIwMDEwMTAwMDAwMFoXDTQwMDEwMTAwMDAwMFow
HjEcMBoGA1UEAwwTZXhwbGljaXRfZHVtbXkudGVzdDCCASIwDQYJKoZIhvcNAQEB
BQADggEPADCCAQoCggEBAJHN6YURRyHl9uQ/NWaTZI2p3X10JmAkIipt6fVpattB
1D8zIeq8wf4UHJia3uDF7mAEuv2eOcFR3TF4TYbsjT1lLW6LCumPzK/wZWwUwiXh
EOvt62GKJMmPwjvCqHjXYmLqq63InTuFnyDBvYVAS4ADBnKfb3oWg1LBQT1ihWeg
Fu2cepVLOVn7o8X/PTID/tXZzEm1F8YnZENI4jgC4LMXIiLwXuBld4MhybHzDkq5
u7889ocuWkpXVlZiUaeNOMqlIIzzC5UYWHF5Zx7dX1EP/nh0vp3UCNa7sOTuZoyb
M2yk13snl6GHi26MMJXrQIgLeEbM0rso+gM/ghUxKjUCAwEAAaMvMC0wGgYDVR0R
BBMwEYIJbG9jYWxob3N0hwR/AAABMA8GA1UdEwEB/wQFMAMBAf8wDQYJKoZIhvcN
AQELBQADggEBAEZIywg82e+wv+H+Y4emQitodSGRKHcexERzT2HQW1NpyyBgHZbW
E3KC59CjH3soFJdnWdf1tl9p8TEVrQljsFmtaFCl+iup9YzLyPeFnI2WnYKCiaoV
aNynxjFDYiMrJLDgr+i064ZLea0t2XqKjka54WTAPtOEyYP+e4NTLYX8TEWgj+8O
DSW5/7329ka0n/XxriV/nOy0AHzV3yQvLWRsFsUh5qCB6z+yKDp1BKlOiElDWHpp
YM6niqDeNDByww0uTtBmbBCgObXZgnc5F5DjN1j99xK8RmcvTMjV2t66TSA7rWd3
/ZAjYAsTwAbxZrqYO4YGYGWGQCtByXSC82Y=
-----END CERTIFICATE-----
"""
EXPLICIT_DUMMY_TLS_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCRzemFEUch5fbk
PzVmk2SNqd19dCZgJCIqben1aWrbQdQ/MyHqvMH+FByYmt7gxe5gBLr9njnBUd0x
eE2G7I09ZS1uiwrpj8yv8GVsFMIl4RDr7ethiiTJj8I7wqh412Ji6qutyJ07hZ8g
wb2FQEuAAwZyn296FoNSwUE9YoVnoBbtnHqVSzlZ+6PF/z0yA/7V2cxJtRfGJ2RD
SOI4AuCzFyIi8F7gZXeDIcmx8w5Kubu/PPaHLlpKV1ZWYlGnjTjKpSCM8wuVGFhx
eWce3V9RD/54dL6d1AjWu7Dk7maMmzNspNd7J5ehh4tujDCV60CIC3hGzNK7KPoD
P4IVMSo1AgMBAAECggEAA4FbY8kseDPpZDI3KYdnHjcZ0wDhGvlwvyuOeZ7bxGLY
LwnJabWOhg974tkBp+PnonQn/l4Ys4gGj5XTFVM9ZQm4QSVYxpCKt6XJpm12/8ai
ARi39TZx7SJGkD29OPnX92/+tHTzf2wWYoj4h7TUloKxcRLaBlR2up1YoRjBLiJ5
mTlRvV0xooiZIUr95ELqTGq02bdoGbnFqx40cLNF1GS8kVHms55Kwl/TcTxq8N+G
ru0UXUdSQJ+C78mVSAC1iRjtc6E6om5tD40odWBSirqLvZ18PQKax66DllzxP7yX
Plwpg3jUh1HzsWt+jjpczJ9jmJJ/jr7zUPgVNfy9EQKBgQDNnQE97k5SDst7sP9E
D7EKm1l7B4y4U6a7vJybk575263vzT+z6xa26DuELQ7csVNqgs4XpBLC9tYHgSVf
yj1kQBlKoHyO1Q2qNFFQVXRo+zmk6XwnGDWGywpxC+KpW3Es4bVU1V31XqaVyv/5
2yTLhVSJCF/OBkzYOx9zqB3xEQKBgQC1iNZwhWtqs+G/Rfswz1QRbzYkiPGGbzS6
2ZXNvuhRUdsFEzNPtZN5WCWukMnlwVkQ6l2E9tiQMx2CPHoe5eJae5qnZ5RTuaCj
Dv2Le2oxsyB8cDZROSTYTYexMn/hUIx6UT5cS0/a+CC3kMA5iW91Wf1Y8WweAATW
JaYINqAm5QKBgGMErlltzyfzWgkaRwQ27lKmV6RJEXqfRld40I6Xu+Kl6ZDnbjsu
BsVU93yC2nprpzlO/wMhj6t2hcIdbqs/0Orbd5KZ0QJUFf2WrmYakTIRckQ58QE+
xmBAnamtSJ24nLs5g0L0cgg9acoPddCUuZaNghVwA/tKy20Co/Xly22RAoGBAJk2
NEYjVNVpnE3ism4vjbRpODj9m1rq4IYSvew4A7seasuNuPC9Pwpa6vK6IKxY0uMD
6ooeDAkoGu+gOzF4s+HKEGzZbsZ7FKiKLlNahfYMSI695hoHMKGeTbev914irW1O
xGMA7RTR9kr3UEU/SWKx4LEAkU+AlJRokgi8kc5BAoGARzp9Z3h/7icvTCqVM1ic
WHkZLRjZHk8vGYx+31MI387B4cqp28yZgak3VPVidCMIO5OeJGq8YyvUUgXV2yrH
CZPA5G/vRxgOrbLmNlt8UX9WjRuCJhyB+DZohzPEb2kWELnq5zds8X/W5/MD+O7j
iEvjTMQ+8/NP3EsIkLeS5bw=
-----END PRIVATE KEY-----
"""

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


class TLSConfigurationTests(unittest.TestCase):
    def test_tls_parameters_must_be_paired_before_monitors_or_workers_start(self):
        self.assertIsNone(load_tls_context())
        for flags in (["--tls-cert", "dummy-cert.pem"], ["--tls-key", "dummy-key.pem"]):
            with self.subTest(flags=flags), patch("share_server.ProjectRegistry.from_config") as registry, \
                    patch("share_server.ProjectSnapshotCache") as cache, redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as raised:
                    main(["--projects-config", "unused.json", *flags])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("--tls-cert and --tls-key must be provided together", errors.getvalue())
                registry.assert_not_called()
                cache.assert_not_called()

    def test_invalid_tls_files_fail_before_monitor_or_workers_start(self):
        with tempfile.TemporaryDirectory() as temp:
            invalid = Path(temp) / "invalid.pem"
            invalid.write_text("explicit_dummy invalid PEM", encoding="ascii")
            for certificate in (invalid, Path(temp) / "missing.pem"):
                with self.subTest(certificate=certificate), patch("share_server.BranchMonitor") as monitor, \
                        patch("share_server.SnapshotCache") as cache, redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as raised:
                        main(["--monitor-config", "unused.json", "--tls-cert", str(certificate), "--tls-key", str(invalid)])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn("Cannot load TLS certificate/key:", errors.getvalue())
                    monitor.assert_not_called()
                    cache.assert_not_called()


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

    def request(self, path, method="GET", authorization=None, host=None, body=None, extra_headers=None, tls_context=None):
        connection = (HTTPSConnection("127.0.0.1", self.server.server_port, timeout=3, context=tls_context)
                      if tls_context else HTTPConnection("127.0.0.1", self.server.server_port, timeout=3))
        headers = {}
        if authorization is not None:
            headers["Authorization"] = authorization
        if host:
            headers["Host"] = host
        if body is not None:
            body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    @staticmethod
    def auth(password="test-long-password", username="viewer"):
        return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    def test_direct_https_handshake_authentication_and_read_only_routes(self):
        cert_path = Path(self.temp.name) / "explicit_dummy-cert.pem"
        key_path = Path(self.temp.name) / "explicit_dummy-key.pem"
        cert_path.write_text(EXPLICIT_DUMMY_TLS_CERT, encoding="ascii")
        key_path.write_text(EXPLICIT_DUMMY_TLS_KEY, encoding="ascii")
        server_context = load_tls_context(cert_path, key_path)
        self.assertEqual(server_context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.serve(password="test-long-password", tls_context=server_context)
        client_context = ssl.create_default_context(cafile=str(cert_path))
        client = HTTPSConnection("127.0.0.1", self.server.server_port, timeout=3, context=client_context)
        try:
            client.connect()
            self.assertIn(client.sock.version(), ("TLSv1.2", "TLSv1.3"))
            client.request("GET", "/healthz", headers={"Authorization": self.auth()})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["read_only"])
        finally:
            client.close()
        for auth in (None, self.auth("wrong")):
            self.assertEqual(self.request("/api/branch-status", authorization=auth, tls_context=client_context)[0], 401)
        status, _, data = self.request("/api/branch-status", authorization=self.auth(), tls_context=client_context)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["backends"][0]["gate"]["passed"], 20)
        self.assertEqual(self.request("/", authorization=self.auth(), tls_context=client_context)[0], 200)
        self.assertEqual(self.request("/api/logs", authorization=self.auth(), tls_context=client_context)[0], 404)
        self.assertEqual(self.request("/api/codex-sessions", method="POST", authorization=self.auth(), tls_context=client_context)[0], 405)
        self.assertEqual(self.monitor.calls, [("status", None)])

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

    def test_public_sglang_cases_keep_model_ownership_without_nested_definitions(self):
        source = source_snapshot()
        source["engine"] = "sglang"
        source["cases"] = [{"scenario": "model_a_text", "requests": 4, "model": "Model-A",
                            "models": ["Model-A", "C:/Users/secret", {"private": "nested"}],
                            "modes": ["eager"], "label": "Concurrent text", "source": "examples/model_a.py",
                            "source_blob": "a" * 40, "file": "examples/model_a.py", "test": "run_text_concurrent",
                            "text_cases": [{"prompt": "private prompt", "expected": ["secret"]}],
                            "definition_files": [{"file": "/secret/config", "blob": "b" * 40}]},
                           {"scenario": "model_b_text", "requests": 4, "model": "Model-B",
                            "models": ["Model-B"], "modes": ["eager"]}]
        source["backends"][0]["results"][0].update(label="Concurrent text", source="examples/model_a.py", source_blob="a" * 40)
        public = sanitize_snapshot(source)
        self.assertEqual(public["cases"][0]["models"], ["Model-A"])
        self.assertEqual(public["cases"][1]["models"], ["Model-B"])
        self.assertEqual(public["cases"][0]["modes"], ["eager"])
        self.assertEqual(public["cases"][0]["label"], "Concurrent text")
        self.assertEqual(public["backends"][0]["results"][0]["source"], "examples/model_a.py")
        self.assertNotIn("secret", json.dumps(public))
        self.assertNotIn("text_cases", json.dumps(public))
        self.assertNotIn("definition_files", json.dumps(public))

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
            status, headers, _ = self.request("/api/codex-sessions", method=method, authorization=self.auth())
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
            capabilities = json.loads(data)
            self.assertEqual({key: capabilities[key] for key in ("read_only", "sessions", "refresh", "evidence", "default_branch")},
                             {"read_only": True, "sessions": False, "refresh": True, "evidence": False, "default_branch": "0.4.0-dev"})
            self.assertFalse(capabilities["workflow"])
            self.assertEqual(self.request(prefix + "/api/branch-status?branch=0.4.0-dev")[0], 200)
        self.assertEqual(self.request("/other/api/branch-status")[0], 404)

    def test_manual_refresh_reads_once_and_returns_new_sanitized_snapshot(self):
        self.serve(base_path="/fl/dashboard", password="test-long-password")
        def changed(branch=None):
            self.monitor.calls.append(("refresh", branch))
            value = source_snapshot()
            value["backends"][0]["pr"].update(number=566, state="open", draft=False)
            return value
        self.monitor.refresh_branch = changed
        status, _, data = self.request("/fl/dashboard/api/branch-refresh", method="POST", authorization=self.auth(),
                                       body={"branch": "0.4.0-dev"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["backends"][0]["pr"]["number"], 566)
        self.assertEqual(json.loads(data)["backends"][0]["pr"]["state"], "open")
        self.assertNotIn(b"source_thread", data)
        self.assertEqual(self.request("/api/branch-refresh", method="POST", authorization=self.auth(), body={"branch":"0.4.0-dev"})[0], 200)
        self.assertEqual(self.monitor.calls.count(("refresh", "0.4.0-dev")), 1)
        self.assertEqual(json.loads(self.request("/api/branch-status", authorization=self.auth())[2])["backends"][0]["pr"]["number"], 566)

    def test_manual_refresh_rejects_cross_site_and_unconfigured_branch(self):
        self.serve()
        self.assertEqual(self.request("/api/branch-refresh", method="POST", body={},
                                     extra_headers={"Origin":"https://other.example"})[0], 403)
        self.assertEqual(self.request("/api/branch-refresh", method="POST", body={"branch":"other"})[0], 400)
        self.assertEqual(self.request("/api/branch-refresh", method="POST")[0], 415)
        self.assertEqual(self.monitor.calls, [("status", None)])

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
