"""JumpServer control routes use cached state and enforce the current asset list."""
from contextlib import ExitStack
from copy import deepcopy
from http.client import HTTPConnection
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from fl_dashboard.projects.project_registry import ProjectContext, ProjectRegistry
from fl_dashboard.resources.jumpserver import JumpServerError
from fl_dashboard.services import flflow


class JumpServerRouteTests(unittest.TestCase):
    def setUp(self):
        self.owner_secret = "explicit-dummy-owner-" + "a" * 48
        self.state = {"enabled": True, "authenticated": True, "refreshing": False,
                      "updated_at": None, "error": None, "assets": []}
        self.inventory = Mock()
        self.inventory.snapshot.side_effect = lambda: deepcopy(self.state)
        for operation in ("configure", "login", "refresh", "logout"):
            getattr(self.inventory, operation).return_value = {"operation": operation}
        self.profiles = Mock()
        self.profiles.profiles.return_value = [{"id": "jms-authorized", "source": "jumpserver",
                                                 "label": "Authorized test device"}]
        self.fleet = Mock()
        self.sync = Mock()
        self.sessions = SimpleNamespace(
            snapshot=Mock(side_effect=lambda **kw: {"sessions": [], "machine_profiles": [
                {"id": "ssh-local", "source": "ssh_config"},
                {"id": "jms-revoked", "source": "jumpserver"}]}),
            check_machine=Mock(return_value={"ok": True}),
            create=Mock(return_value={"id": "a" * 32}),
            start=Mock(return_value={"status": "running"}),
            message=Mock(return_value={"accepted": True}),
            interrupt=Mock(return_value={"accepted": True}),
        )
        self.sync.synchronize.side_effect = lambda manager, snapshot: snapshot
        self.server = self.make_server(sessions=self.sessions)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)

    def make_server(self, *, sessions=None, registry=None, monitor=None):
        with ExitStack() as patches:
            self.owner_token = patches.enter_context(patch(
                "fl_dashboard.services.owner_rpc.owner_rpc_token", return_value=self.owner_secret))
            self.inventory_class = patches.enter_context(patch(
                "fl_dashboard.resources.jumpserver.JumpServerInventory", return_value=self.inventory))
            self.profiles_class = patches.enter_context(patch(
                "fl_dashboard.resources.jumpserver_profiles.JumpServerProfiles", return_value=self.profiles))
            self.fleet_class = patches.enter_context(patch(
                "fl_dashboard.resources.fleet.DeviceFleet", return_value=self.fleet))
            patches.enter_context(patch(
                "fl_dashboard.tasks.ssh_profile_sync.SSHProfileSync", return_value=self.sync))
            return flflow.make_server(None, "127.0.0.1", 0, sessions=sessions,
                                      monitor=monitor, registry=registry)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)

    def request(self, path, *, body=None, method=None, headers=None, raw=None):
        method = method or ("POST" if body is not None or raw is not None else "GET")
        values = {"Content-Type": "application/json"} if method == "POST" else {}
        values.update(headers or {})
        payload = raw if raw is not None else (json.dumps(body) if body is not None else None)
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, payload, values)
        response = connection.getresponse()
        status, data = response.status, json.loads(response.read())
        connection.close()
        return status, data

    def assert_no_mutations(self):
        for operation in ("configure", "login", "refresh", "logout"):
            getattr(self.inventory, operation).assert_not_called()

    def test_get_only_reads_cached_state(self):
        status, body = self.request("/api/jumpserver?project=vllm")
        self.assertEqual(200, status)
        self.assertEqual(self.state, body)
        self.inventory.snapshot.assert_called_once_with()
        self.assert_no_mutations()
        self.profiles.profiles.assert_not_called()
        self.fleet.refresh.assert_not_called()
        self.sync.synchronize.assert_not_called()

    def test_settings_login_refresh_and_logout_dispatch_once(self):
        cases = (("settings", "configure", {"enabled": True, "base_url": "https://example.invalid"}, 200),
                 ("login", "login", {"username": "fixture", "password": "explicit-dummy"}, 200),
                 ("refresh", "refresh", {}, 202), ("logout", "logout", {}, 200))
        for route, operation, payload, expected in cases:
            with self.subTest(route=route):
                status, body = self.request("/api/jumpserver/" + route + "?project=vllm", body=payload)
                self.assertEqual(expected, status)
                self.assertEqual({"operation": operation}, body)
                method = getattr(self.inventory, operation)
                if operation in ("configure", "login"):
                    method.assert_called_once_with(payload)
                else:
                    method.assert_called_once_with()
        self.fleet.refresh.assert_not_called()
        self.sync.synchronize.assert_not_called()

    def test_unexpected_errors_are_redacted_and_public_validation_is_retained(self):
        private = "PRIVATE_REMOTE_ERROR_EXPLICIT_DUMMY"
        self.inventory.snapshot.side_effect = RuntimeError(private)
        status, body = self.request("/api/jumpserver")
        self.assertEqual(503, status)
        self.assertNotIn(private, json.dumps(body))
        for route, operation in (("settings", "configure"), ("login", "login"),
                                 ("refresh", "refresh"), ("logout", "logout")):
            with self.subTest(route=route):
                getattr(self.inventory, operation).side_effect = RuntimeError(private)
                status, body = self.request("/api/jumpserver/" + route, body={})
                self.assertEqual(503, status)
                self.assertNotIn(private, json.dumps(body))
        self.inventory.login.side_effect = JumpServerError("Please check the login policy")
        self.assertEqual((400, {"error": "Please check the login policy"}),
                         self.request("/api/jumpserver/login", body={}))

    def test_origin_host_and_fetch_site_are_checked_before_credentials(self):
        for headers in ({"Origin": "https://elsewhere.invalid"}, {"Origin": "null"},
                        {"Host": "elsewhere.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(403, self.request("/api/jumpserver/login", body={}, headers=headers)[0])
        self.assertEqual(403, self.request("/api/jumpserver", headers={"Host": "elsewhere.invalid"})[0])
        self.assert_no_mutations()
        self.inventory.snapshot.assert_not_called()

    def test_query_parameters_body_shape_and_size_are_rejected(self):
        for query in ("host=other", "project=vllm&project=vllm", "project=missing", "project="):
            with self.subTest(query=query):
                self.assertEqual(400, self.request("/api/jumpserver?" + query)[0])
                self.assertEqual(400, self.request("/api/jumpserver/login?" + query, body={})[0])
        for route in ("refresh", "logout"):
            self.assertEqual(400, self.request("/api/jumpserver/" + route, body={"host": "other"})[0])
        for raw in ("[]", "null", "invalid-json"):
            self.assertEqual(400, self.request("/api/jumpserver/login", raw=raw)[0])
        self.assertEqual(400, self.request("/api/jumpserver/login", body={}, headers={"Content-Type": "text/plain"})[0])
        self.assertEqual(400, self.request("/api/jumpserver/login", body={}, headers={"Content-Length": "1000001"})[0])
        self.assert_no_mutations()

    def test_no_session_service_disables_all_jumpserver_routes(self):
        server = self.make_server()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        previous, self.server = self.server, server
        try:
            self.assertEqual(503, self.request("/api/jumpserver")[0])
            for route in ("settings", "login", "refresh", "logout"):
                self.assertEqual(503, self.request("/api/jumpserver/" + route, body={})[0])
            self.assertFalse(self.request("/api/capabilities")[1]["jumpserver_settings"])
            self.assert_no_mutations()
        finally:
            self.server = previous
            server.shutdown()
            server.server_close()
            worker.join(3)

    def test_active_jumpserver_blocks_legacy_ssh_sync_without_refreshing_assets(self):
        self.assertEqual(400, self.request("/api/codex-machines/sync-ssh", body={})[0])
        self.sync.synchronize.assert_not_called()
        self.inventory.refresh.assert_not_called()
        self.sessions.snapshot.assert_not_called()

    def test_workbench_uses_only_current_bridge_profiles_without_ssh_sync(self):
        for suffix in ("", "?view=cards"):
            with self.subTest(suffix=suffix):
                status, body = self.request("/api/codex-sessions" + suffix)
                self.assertEqual(200, status)
                self.assertEqual(self.profiles.profiles.return_value, body["machine_profiles"])
                self.assertEqual("jumpserver", body["machine_source"])
                self.assertEqual("jumpserver", body["ssh_config_sync"]["source"])
                self.assertEqual(1, body["ssh_config_sync"]["profile_count"])
        self.profiles.profiles.assert_called_with("vllm", self.sessions)
        self.sync.synchronize.assert_not_called()
        self.inventory.refresh.assert_not_called()
        self.sessions.snapshot.assert_any_call(compact=True)

    def test_revoked_and_unready_assets_cannot_be_checked_or_create_tasks(self):
        for current in ([], [{"id": "jms-revoked", "configuration_error": "Authorization unavailable"}]):
            self.profiles.profiles.return_value = current
            for route in ("/api/codex-machines/check", "/api/codex-sessions"):
                with self.subTest(current=current, route=route):
                    self.assertEqual(400, self.request(route, body={"profile_id": "jms-revoked"})[0])
        self.sessions.check_machine.assert_not_called()
        self.sessions.create.assert_not_called()
        self.sessions.start.assert_not_called()

    def test_authorized_asset_can_be_checked_and_create_task(self):
        payload = {"profile_id": "jms-authorized"}
        self.assertEqual(200, self.request("/api/codex-machines/check", body=payload)[0])
        self.sessions.check_machine.assert_called_once_with(payload)
        with patch("fl_dashboard.tasks.automatic_tasks.normalize_automatic_task", return_value=payload):
            self.assertEqual(200, self.request("/api/codex-sessions", body=payload)[0])
        self.sessions.create.assert_called_once_with(payload)

    def test_reauthorization_does_not_block_interacting_with_existing_task(self):
        self.profiles.profiles.return_value = []
        task_id = "a" * 32
        self.assertEqual(200, self.request("/api/codex-sessions/" + task_id + "/message", body={"text": "Continue"})[0])
        self.assertEqual(200, self.request("/api/codex-sessions/" + task_id + "/interrupt", body={})[0])
        self.sessions.message.assert_called_once_with(task_id, "Continue")
        self.sessions.interrupt.assert_called_once_with(task_id)
        self.profiles.profiles.assert_not_called()

    def test_starting_saved_draft_rechecks_current_asset_authorization(self):
        task_id = "a" * 32
        self.sessions.snapshot.side_effect = lambda **kw: {"sessions": [
            {"id": task_id, "profile_id": "jms-revoked", "status": "draft"}]}
        self.profiles.profiles.return_value = []
        self.assertEqual(400, self.request("/api/codex-sessions/" + task_id + "/start", body={})[0])
        self.sessions.snapshot.assert_called_once_with(compact=True)
        self.sessions.start.assert_not_called()

    def test_create_and_start_rechecks_authorization_after_draft_creation(self):
        payload = {"profile_id": "jms-authorized", "start": True}
        self.profiles.profiles.side_effect = [[{"id": "jms-authorized"}], []]
        with patch("fl_dashboard.tasks.automatic_tasks.normalize_automatic_task", return_value=payload):
            self.assertEqual(400, self.request("/api/codex-sessions", body=payload)[0])
        self.sessions.create.assert_called_once_with(payload)
        self.sessions.start.assert_not_called()

    def test_disabling_jumpserver_hides_history_and_rejects_new_actions_on_it(self):
        self.state["enabled"] = False
        status, body = self.request("/api/codex-sessions")
        self.assertEqual(200, status)
        self.assertEqual("ssh_config", body["machine_source"])
        self.assertEqual(["ssh-local"], [row["id"] for row in body["machine_profiles"]])
        self.profiles.profiles.assert_not_called()
        for route in ("/api/codex-machines/check", "/api/codex-sessions"):
            with self.subTest(route=route):
                self.assertEqual(400, self.request(route, body={"profile_id": "jms-revoked"})[0])
        self.sessions.check_machine.assert_not_called()
        self.sessions.create.assert_not_called()
        self.assertEqual(200, self.request("/api/codex-machines/check", body={"profile_id": "ssh-local"})[0])
        self.sessions.check_machine.assert_called_once_with({"profile_id": "ssh-local"})

    def test_forged_owner_header_does_not_bypass_single_owner_authorization(self):
        headers = {"X-FL-Session-Owner": "1"}
        self.profiles.profiles.return_value = []
        status, body = self.request("/api/codex-sessions?view=cards", headers=headers)
        self.assertEqual(200, status)
        self.assertEqual([], body["machine_profiles"])
        payload = {"profile_id": "jms-revoked"}
        self.assertEqual(400, self.request("/api/codex-machines/check", body=payload, headers=headers)[0])
        self.assertEqual(400, self.request("/api/codex-sessions", body=payload, headers=headers)[0])
        self.sessions.check_machine.assert_not_called()
        self.sessions.create.assert_not_called()
        self.sync.synchronize.assert_not_called()

    def test_only_authenticated_single_owner_requests_preserve_raw_profiles(self):
        token_path = "D:/Codex/work/explicit-dummy-owner-token.json"
        monitor = SimpleNamespace(config={"sessions_owner_token_file": token_path})
        server = self.make_server(sessions=self.sessions, monitor=monitor)
        self.owner_token.assert_called_once_with(token_path)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        previous, self.server = self.server, server
        self.profiles.profiles.return_value = []
        try:
            for wrong in ("1", "explicit-dummy-wrong-token"):
                headers = {"X-FL-Session-Owner": wrong}
                self.assertEqual([], self.request("/api/codex-sessions", headers=headers)[1]["machine_profiles"])
                self.assertEqual(400, self.request("/api/codex-machines/check", body={"profile_id": "jms-revoked"}, headers=headers)[0])
            self.profiles.profiles.reset_mock()
            headers = {"X-FL-Session-Owner": self.owner_secret}
            status, body = self.request("/api/codex-sessions?view=cards", headers=headers)
            self.assertEqual(200, status)
            self.assertEqual(["ssh-local", "jms-revoked"], [row["id"] for row in body["machine_profiles"]])
            payload = {"profile_id": "jms-revoked"}
            self.assertEqual(200, self.request("/api/codex-machines/check", body=payload, headers=headers)[0])
            with patch("fl_dashboard.tasks.automatic_tasks.normalize_automatic_task", return_value=payload):
                self.assertEqual(200, self.request("/api/codex-sessions", body=payload, headers=headers)[0])
            self.sessions.check_machine.assert_called_once_with(payload)
            self.sessions.create.assert_called_once_with(payload)
            self.profiles.profiles.assert_not_called()
            self.sync.synchronize.assert_not_called()
            self.assertEqual(403, self.request("/api/codex-machines/check", body=payload,
                                              headers={**headers, "Origin": "https://elsewhere.invalid"})[0])
        finally:
            self.server = previous
            server.shutdown()
            server.server_close()
            worker.join(3)

    def test_owner_header_does_not_bypass_aggregate_controller_authorization(self):
        monitor = SimpleNamespace(config={"sessions_owner_token_file": "D:/Codex/work/explicit-dummy-owner-token.json"})
        registry = ProjectRegistry([ProjectContext("vllm", "vLLM", monitor=monitor, sessions=self.sessions),
                                    ProjectContext("sglang", "SGLang")])
        server = self.make_server(registry=registry)
        self.owner_token.assert_not_called()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        previous, self.server = self.server, server
        self.profiles.profiles.return_value = []
        try:
            for route in ("/api/codex-machines/check", "/api/codex-sessions"):
                self.assertEqual(400, self.request(route, body={"profile_id": "jms-revoked"},
                                                  headers={"X-FL-Session-Owner": self.owner_secret})[0])
            status, body = self.request("/api/codex-sessions", headers={"X-FL-Session-Owner": self.owner_secret})
            self.assertEqual(200, status)
            self.assertEqual([], body["machine_profiles"])
            self.assertEqual("jumpserver", body["machine_source"])
            self.sessions.check_machine.assert_not_called()
            self.sessions.create.assert_not_called()
        finally:
            self.server = previous
            server.shutdown()
            server.server_close()
            worker.join(3)

    def test_resource_state_paths_isolate_single_owner_and_multi_project_controller(self):
        base = Path("D:/Codex/work/explicit-test-resource-state")
        monitor = SimpleNamespace(config={"sessions_dir": str(base / "sessions"),
                                          "resource_state_dir": str(base / "resources")})
        single = self.make_server(sessions=self.sessions, monitor=monitor)
        try:
            expected = base / "resources" / "project-vllm"
            self.inventory_class.assert_called_once_with(expected / "jumpserver")
            self.assertEqual(expected / "device-resource-cache.json", self.fleet_class.call_args.kwargs["cache_path"])
            self.assertIs(self.inventory, self.fleet_class.call_args.kwargs["inventory"])
            self.assertIs(self.profiles, self.fleet_class.call_args.kwargs["profile_source"])
        finally:
            single.server_close()
        registry = ProjectRegistry([ProjectContext("vllm", "vLLM", monitor=monitor, sessions=self.sessions),
                                    ProjectContext("sglang", "SGLang")])
        aggregate = self.make_server(registry=registry)
        try:
            expected = base / "resources" / "multi-project"
            self.inventory_class.assert_called_once_with(expected / "jumpserver")
            self.assertEqual(expected / "device-resource-cache.json", self.fleet_class.call_args.kwargs["cache_path"])
        finally:
            aggregate.server_close()

    def test_relative_resource_state_directory_is_resolved_from_app_root(self):
        monitor = SimpleNamespace(config={"resource_state_dir": "runs/example-resource-state"})
        server = self.make_server(sessions=self.sessions, monitor=monitor)
        try:
            expected = flflow.APP_ROOT / "runs/example-resource-state/project-vllm"
            self.inventory_class.assert_called_once_with(expected / "jumpserver")
            self.assertEqual(expected / "device-resource-cache.json", self.fleet_class.call_args.kwargs["cache_path"])
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
