"""Sharing is an authenticated gateway; ordinary LAN visitors stay read-only."""
from datetime import datetime, timedelta, timezone
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from fl_dashboard.services.access_shares import AccessShareStore
from fl_dashboard.services.share_gateway import COOKIE_NAME, ShareGateway, validate_control_url
from fl_dashboard.services.share_server import SnapshotCache, load_tls_context, make_server
from test_share_server import EXPLICIT_DUMMY_TLS_CERT, EXPLICIT_DUMMY_TLS_KEY, FakeMonitor


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = AccessShareStore(self.root / "shares.json", "https://localhost")
        self.created = self.store.create(label="test collaborator", expires_in_hours=24)
        self.calls = []
        self.upstream_status = 200
        outer = self

        class Control(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                outer.calls.append((self.command, self.path, dict(self.headers), None))
                body = {"read_only": False, "sessions": True, "access_shares": True, "private": "task details"}
                self.send_response(outer.upstream_status)
                self.send_header("Content-Type", "application/json")
                if 300 <= outer.upstream_status < 400:
                    self.send_header("Location", "http://untrusted.invalid/")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append((self.command, self.path, dict(self.headers), body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"accepted":true}')

        self.control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
        self.control.daemon_threads = True
        self.control_worker = threading.Thread(target=self.control.serve_forever, daemon=True)
        self.control_worker.start()
        self.control_url = f"http://127.0.0.1:{self.control.server_port}"
        self.cache = SnapshotCache(FakeMonitor(), refresh_interval=0)
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        cert.write_text(EXPLICIT_DUMMY_TLS_CERT, encoding="ascii")
        key.write_text(EXPLICIT_DUMMY_TLS_KEY, encoding="ascii")
        self.tls = load_tls_context(cert, key)
        self.client_tls = ssl.create_default_context(cafile=str(cert))
        # A second store models the independent LAN process seeing local writes.
        reader = AccessShareStore(self.store.path, self.store.public_url)
        self.server = make_server(self.cache, "127.0.0.1", 0, tls_context=self.tls,
                                  access_shares=reader, control_url=self.control_url)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        for server, worker in ((self.server, self.worker), (self.control, self.control_worker)):
            server.shutdown()
            server.server_close()
            worker.join(3)
        self.cache.close()
        self.temp.cleanup()

    def request(self, path, *, method="GET", body=None, token=None, headers=None):
        request_headers = {"Host": "localhost"}
        if token is not None:
            request_headers["Cookie"] = f"{COOKIE_NAME}={token}"
        if body is not None:
            body = json.dumps(body).encode()
            request_headers.update({"Content-Type": "application/json", "Origin": self.store.public_url})
        request_headers.update(headers or {})
        connection = HTTPSConnection("127.0.0.1", self.server.server_port, context=self.client_tls, timeout=4)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def login(self, token=None, **kwargs):
        return self.request("/api/share-login", method="POST", body={"token": token or self.created["token"]}, **kwargs)

    def test_ordinary_visitors_are_still_read_only(self):
        status, _, data = self.request("/api/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["read_only"])
        self.assertEqual(self.request("/api/codex-sessions")[0], 404)
        self.assertEqual(self.request("/api/codex-sessions", method="POST", body={})[0], 405)
        self.assertEqual(self.calls, [])

    def test_device_monitoring_requires_authorized_share(self):
        for path in ("/devices", "/devices.html", "/api/devices?project=vllm"):
            self.assertEqual(404, self.request(path)[0])
        self.assertEqual(405, self.request("/api/devices/refresh", method="POST", body={})[0])
        self.assertEqual([], self.calls)
        for path in ("/devices", "/api/devices?project=vllm"):
            self.assertEqual(200, self.request(path, token=self.created["token"])[0])
        self.assertEqual(200, self.request("/api/devices/refresh?project=vllm", method="POST", body={"profile_id": "ssh-test"}, token=self.created["token"])[0])
        self.assertEqual("/api/devices/refresh?project=vllm", self.calls[-1][1])

    def test_login_cookie_is_secure_and_token_is_never_in_response_body(self):
        status, headers, data = self.login()
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        for expected in (COOKIE_NAME + "=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/", "Max-Age="):
            self.assertIn(expected, cookie)
        self.assertNotIn(self.created["token"], data.decode())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.calls, [])

    def test_authenticated_capabilities_and_mutation_use_only_control_origin(self):
        token = self.created["token"]
        status, _, data = self.request("/api/capabilities?project=sglang", token=token)
        self.assertEqual(status, 200)
        capabilities = json.loads(data)
        self.assertFalse(capabilities["read_only"])
        self.assertTrue(capabilities["shared_access"])
        self.assertFalse(capabilities["access_shares"])
        status, _, _ = self.request("/api/codex-sessions?project=sglang", method="POST", body={"label": "fixture"}, token=token,
            headers={"Authorization": "Bearer explicit_dummy", "X-Forwarded-For": "untrusted"})
        self.assertEqual(status, 200)
        method, path, headers, body = self.calls[-1]
        self.assertEqual((method, path, body), ("POST", "/api/codex-sessions?project=sglang", {"label": "fixture"}))
        self.assertEqual(headers["Host"], self.control_url.removeprefix("http://"))
        self.assertEqual(headers["Origin"], self.control_url)
        for forbidden in ("Cookie", "Authorization", "X-Forwarded-For"):
            self.assertNotIn(forbidden, headers)

    def test_revocation_and_expiration_are_checked_on_every_request(self):
        token = self.created["token"]
        self.assertEqual(self.request("/api/codex-sessions", token=token)[0], 200)
        self.store.revoke(self.created["id"])
        status, headers, _ = self.request("/api/codex-sessions", token=token)
        self.assertEqual(status, 401)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.login()[0], 401)
        another = self.store.create(expires_in_hours=1)
        with patch("fl_dashboard.services.access_shares._utc_now", return_value=datetime.now(timezone.utc) + timedelta(hours=2)):
            self.assertEqual(self.request("/api/codex-sessions", token=another["token"])[0], 401)
        self.assertEqual(len(self.calls), 1)

    def test_host_origin_and_cross_site_rejected_before_forwarding(self):
        token = self.created["token"]
        self.assertEqual(self.login(headers={"Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(self.login(headers={"Host": "evil.invalid"})[0], 403)
        self.assertEqual(self.login(headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/api/codex-sessions", token=token, headers={"Host": "evil.invalid"})[0], 403)
        self.assertEqual(self.request("/api/codex-sessions", method="POST", body={}, token=token,
                                      headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.calls, [])

    def test_link_management_and_unknown_routes_are_not_forwarded(self):
        token = self.created["token"]
        for path in ("/access-shares", "/access-shares.html", "/api/access-shares", "/api/access-shares/abc/revoke",
                     "/api/not-a-route", "/api/%63apabilities"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, token=token)[0], 403)
                self.assertEqual(self.request(path, method="POST", body={}, token=token)[0], 403)
        self.assertEqual(self.calls, [])

    def test_invalid_cookie_and_store_fail_closed(self):
        self.assertEqual(self.request("/api/codex-sessions", token="x" * 43)[0], 401)
        self.assertEqual(self.request("/api/codex-sessions", token="bad")[0], 401)
        self.assertEqual(self.request("/api/codex-sessions", headers={"Cookie": f"{COOKIE_NAME}=aaa; {COOKIE_NAME}=bbb"})[0], 401)
        self.store.path.write_text("not json", encoding="utf-8")
        self.assertEqual(self.request("/api/codex-sessions", token=self.created["token"])[0], 401)
        self.assertEqual(self.calls, [])

    def test_logout_clears_cookie_even_after_revocation(self):
        self.store.revoke(self.created["id"])
        status, headers, data = self.request("/api/share-logout", method="POST", body={}, token=self.created["token"])
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["read_only"])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.calls, [])

    def test_invalid_login_input_and_body_framing_are_rejected(self):
        for body in ({}, {"token": 5}, {"token": self.created["token"], "extra": True}):
            self.assertEqual(self.request("/api/share-login", method="POST", body=body)[0], 400)
        self.assertEqual(self.request("/api/share-login", method="POST", body={"token": self.created["token"]},
                                     headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.request("/api/codex-sessions", method="POST", body={}, token=self.created["token"],
                                     headers={"Content-Length": "1000001"})[0], 400)
        self.assertEqual(self.calls, [])

    def test_upstream_redirect_and_oversized_responses_fail_closed(self):
        self.upstream_status = 302
        status, headers, _ = self.request("/api/status", token=self.created["token"])
        self.assertEqual(status, 502)
        self.assertNotIn("Location", headers)
        self.upstream_status = 200
        with patch("fl_dashboard.services.share_gateway.MAX_RESPONSE", 10):
            self.assertEqual(self.request("/api/status", token=self.created["token"])[0], 502)

    def test_sharing_link_can_be_opened_from_chat_or_email(self):
        status, _, _ = self.request("/access", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
        self.assertEqual(status, 200)
        self.assertEqual(self.login(headers={"Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(self.calls, [])

    def test_revocation_during_body_read_prevents_mutation(self):
        original_body = ShareGateway._body
        def revoke_then_read(handler, limit=1_000_000):
            result = original_body(handler, limit)
            self.store.revoke(self.created["id"])
            return result
        with patch.object(ShareGateway, "_body", staticmethod(revoke_then_read)):
            self.assertEqual(self.request("/api/codex-sessions", method="POST", body={}, token=self.created["token"])[0], 401)
        self.assertEqual(self.calls, [])

    def test_access_page_discards_query_token_and_has_no_referrer(self):
        status, headers, data = self.request("/access")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn(b"replaceState", data)
        self.assertEqual(self.request("/access?token=explicit_dummy")[0], 400)


class GatewayConfigurationTests(unittest.TestCase):
    def test_control_target_must_be_literal_loopback_origin(self):
        for target in ("https://127.0.0.1:8766", "http://localhost:8766", "http://example.invalid:80",
                       "http://127.0.0.1:8766/path", "http://user@127.0.0.1:8766", "http://127.0.0.1:8766?x=1",
                       "http://@127.0.0.1:8766", "http://127.0.0.1:8766?", "http://127.0.0.1:8766#",
                       "http://127.0.0.1:8766/\\", "http://127.0.0.1:8766/\x7f"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_control_url(target)
        self.assertEqual(validate_control_url("http://127.0.0.1:8766/"), "http://127.0.0.1:8766")
        self.assertEqual(validate_control_url("http://[::1]:8766"), "http://[::1]:8766")

    def test_full_access_requires_direct_tls_root_url(self):
        store = AccessShareStore("unused.json", "https://localhost")
        with self.assertRaises(ValueError):
            ShareGateway(store, "http://127.0.0.1:8766", tls_context=None)
        with self.assertRaises(ValueError):
            ShareGateway(store, "http://127.0.0.1:8766", tls_context=object(), base_path="/monitor")
        with self.assertRaises(ValueError):
            ShareGateway(AccessShareStore("unused.json", "https://localhost/monitor"),
                         "http://127.0.0.1:8766", tls_context=object())


if __name__ == "__main__":
    unittest.main()
