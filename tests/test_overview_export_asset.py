"""The export asset is fixed, inert, and available at existing access levels."""
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
import ssl
import tempfile
import threading
import unittest

from fl_dashboard.paths import ASSETS
from fl_dashboard.services.access_shares import AccessShareStore
from fl_dashboard.services.flflow import make_server as local_server
from fl_dashboard.services.share_gateway import COOKIE_NAME
from fl_dashboard.services.share_server import SnapshotCache, load_tls_context, make_server as public_server
from test_share_server import EXPLICIT_DUMMY_TLS_CERT, EXPLICIT_DUMMY_TLS_KEY, FakeMonitor


ASSET_PATH = "/assets/overview-export.js"


class OverviewExportAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.monitor = FakeMonitor()
        self.cache = SnapshotCache(self.monitor, refresh_interval=0)
        self.addCleanup(self.cache.close)
        self.asset = (ASSETS / "overview-export.js").read_bytes()

    def serve(self, server):
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close():
            server.shutdown()
            server.server_close()
            worker.join(3)
        self.addCleanup(close)
        return server

    def request(self, server, path=ASSET_PATH, *, headers=None, tls=None, method="GET"):
        connection = (HTTPSConnection("127.0.0.1", server.server_port, context=tls, timeout=5)
                      if tls else HTTPConnection("127.0.0.1", server.server_port, timeout=5))
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def assert_asset(self, response):
        status, headers, body = response
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/javascript; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(body, self.asset)
        self.assertEqual(int(headers["Content-Length"]), len(self.asset))
        self.assertEqual(self.monitor.calls, [])

    def test_local_and_public_assets_do_not_collect_monitor_state(self):
        local = self.serve(local_server(None, "127.0.0.1", 0, monitor=self.monitor))
        public = self.serve(public_server(self.cache, "127.0.0.1", 0))
        self.assert_asset(self.request(local))
        self.assert_asset(self.request(public))
        status, headers, body = self.request(public, method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["Content-Length"]), len(self.asset))
        self.assertEqual(self.monitor.calls, [])

    def test_prefixed_public_asset_uses_same_fixed_file(self):
        public = self.serve(public_server(self.cache, "127.0.0.1", 0, base_path="/team/dashboard"))
        self.assert_asset(self.request(public, "/team/dashboard" + ASSET_PATH))

    def test_unknown_and_traversal_assets_are_not_file_routes(self):
        servers = [self.serve(local_server(None, "127.0.0.1", 0, monitor=self.monitor)),
                   self.serve(public_server(self.cache, "127.0.0.1", 0))]
        for server in servers:
            for path in ("/assets/unknown.js", "/assets/../templates/overview.html",
                         "/assets/%2e%2e/templates/overview.html", "/assets/overview-export.js/extra"):
                with self.subTest(path=path, port=server.server_port):
                    self.assertEqual(self.request(server, path)[0], 404)
            self.assertEqual(self.request(server, ASSET_PATH + "?project=missing")[0], 400)
        self.assertEqual(self.monitor.calls, [])

    def test_asset_obeys_existing_host_and_password_boundary(self):
        local = self.serve(local_server(None, "127.0.0.1", 0, monitor=self.monitor))
        public = self.serve(public_server(self.cache, "127.0.0.1", 0, password="explicit_dummy_password"))
        self.assertEqual(self.request(local, headers={"Host": "untrusted.invalid"})[0], 403)
        self.assertEqual(self.request(public)[0], 401)
        self.assertEqual(self.monitor.calls, [])

    def test_valid_and_revoked_management_shares_can_load_only_the_public_asset(self):
        local = self.serve(local_server(None, "127.0.0.1", 0, monitor=self.monitor))
        store = AccessShareStore(self.root / "shares.json", "https://localhost")
        grant = store.create(label="test collaborator", expires_in_hours=24)
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        cert.write_text(EXPLICIT_DUMMY_TLS_CERT, encoding="ascii")
        key.write_text(EXPLICIT_DUMMY_TLS_KEY, encoding="ascii")
        tls = load_tls_context(cert, key)
        client_tls = ssl.create_default_context(cafile=str(cert))
        public = self.serve(public_server(self.cache, "127.0.0.1", 0, tls_context=tls,
                            access_shares=store, control_url=f"http://127.0.0.1:{local.server_port}"))
        headers = {"Host": "localhost", "Cookie": f"{COOKIE_NAME}={grant['token']}"}
        self.assert_asset(self.request(public, headers=headers, tls=client_tls))
        self.assertEqual(self.request(public, "/assets/private.json", headers=headers, tls=client_tls)[0], 403)
        store.revoke(grant["id"])
        response = self.request(public, headers=headers, tls=client_tls)
        self.assert_asset(response)
        self.assertIn("Max-Age=0", response[1]["Set-Cookie"])
        self.assertEqual(self.request(public, "/api/codex-sessions", headers=headers, tls=client_tls)[0], 401)
        self.assertEqual(self.monitor.calls, [])


if __name__ == "__main__":
    unittest.main()
