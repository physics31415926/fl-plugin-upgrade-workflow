"""Automatic create lifecycle tests; all SSH, Git and agent operations are mocked."""
import copy
from http.client import HTTPConnection
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import fl_dashboard.tasks.automatic_tasks as automatic
from fl_dashboard.agents.codex_sessions import CodexSessionManager, SessionError
from fl_dashboard.agents.composite_sessions import CompositeSessionManager
import fl_dashboard.services.flflow as flflow
from fl_dashboard.projects.project_registry import ProjectContext, ProjectRegistry, RemoteSessionManager
from fl_dashboard.services.share_server import make_server as make_shared_server


def output(hardware="", source=False, home="/home/test", tmux=True):
    value = "FLFLOW_AUTO_READY=1\nHOME\t" + home + "\nTMUX\t" + ("1" if tmux else "0") + "\n"
    if source:
        value += "SOURCE\t/home/test/repos/plugin\t" + "a" * 40 + "\n"
    for line in hardware.splitlines():
        value += "HARDWARE\t" + line + "\n"
    return SimpleNamespace(returncode=0, stdout=value.encode("utf-8"), stderr=b"")


class FakeClient:
    def __init__(self, executable, cwd, callback, timeout=20):
        self.callback = callback
        self.process = SimpleNamespace(poll=lambda: None)
        self.info = {}
        self.requests = []

    def request(self, method, params):
        self.requests.append((method, params))
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": "thread-test"}}
        if method == "turn/start":
            self.callback({"method": "turn/started", "params": {"turn": {"id": "turn-test"}}})
            return {"turn": {"id": "turn-test", "status": "inProgress"}}
        return {}

    def close(self):
        self.process = SimpleNamespace(poll=lambda: 0)


class ProviderManager(CodexSessionManager):
    """Exercise the same provider-stripping superclass hook as real adapters."""
    def __init__(self, config, base, provider):
        self.provider = provider
        self.agent_settings = SimpleNamespace(snapshot=lambda: {"configured": True, "allowed_tools": ["Bash", "Read"]})
        super().__init__(config, base, FakeClient)

    def create(self, payload):
        if payload.get("provider") != self.provider:
            raise SessionError("Wrong provider")
        result = super().create({key: value for key, value in payload.items() if key != "provider"})
        return {**result, "provider": self.provider}


class AutomaticTasksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        (self.base / "repo").mkdir()
        self.config = {"repo": str(self.base / "repo"), "branch": "base", "codex_executable": "fake"}
        self.manager = CodexSessionManager(self.config, self.base, FakeClient)
        self.managers = [self.manager]
        self.mock_git(self.manager)
        self.profile = self.manager.save_profile({"id": "gpu", "ssh_alias": "selected-gpu", "backend": "cuda",
                                                 "remote_workspace": "", "model_paths": {}})
        self.project = ProjectContext("vllm", "Plugin", "vllm", "base", sessions=self.manager)
        self.payload = {"automatic": True, "provider": "codex", "profile_id": "gpu"}
        self.runner = Mock(return_value=output())
        self.servers = []

    def mock_git(self, manager):
        def run(*args):
            if args[:2] == ("worktree", "add"):
                Path(args[-2]).mkdir(parents=True, exist_ok=True)
                return ""
            return "a" * 40 if args[0] == "rev-parse" else ""
        manager._git = Mock(side_effect=run)

    def tearDown(self):
        for server, worker in self.servers:
            server.shutdown()
            server.server_close()
            worker.join(3)
        for manager in self.managers:
            manager.close()
        with automatic._PROOF_LOCK:
            automatic._PROOFS.clear()
        self.temp.cleanup()

    def normalize(self, payload=None):
        return automatic.normalize_automatic_task(self.project, payload or self.payload,
                                                  runner=self.runner, ssh_executable="mock-ssh")

    def update_profile(self, **fields):
        self.profile = self.manager.save_profile({**self.profile, **fields})

    def manual_profile(self):
        self.update_profile(ssh_connection={"host": "gpu.example.test", "user": "test", "port": 22,
                                            "identity_file": "", "jump_alias": ""})
        return self.profile["ssh_config_path"]

    def test_manual_payload_passthrough_never_probes(self):
        for body in ({}, {"profile_id": "gpu", "automatic": False}, {"automatic": "true", "extra": 1}):
            self.assertEqual(automatic.normalize_automatic_task(self.project, body, runner=self.runner), body)
        self.runner.assert_not_called()

    def test_server_fields_are_rejected_even_in_manual_requests(self):
        for field in ("_automatic_proof", "automatic_context"):
            for mode in (False, True):
                with self.subTest(field=field, mode=mode), self.assertRaisesRegex(SessionError, "server-owned"):
                    automatic.normalize_automatic_task(self.project, {"automatic": mode, field: "forged"}, runner=self.runner)
        self.runner.assert_not_called()

    def test_selected_probe_is_bounded_readonly_and_no_eager_start(self):
        value = self.normalize()
        self.assertEqual(self.runner.call_count, 1)
        argv = self.runner.call_args.args[0]
        options = self.runner.call_args.kwargs
        self.assertEqual(argv[-2], "selected-gpu")
        self.assertEqual(options["timeout"], 20)
        self.assertIsInstance(options["input"], bytes)
        self.assertNotIn(b"\r", options["input"])
        self.assertIn(b'count" -lt 100', options["input"])
        self.assertIn(b"Processing accelerators", options["input"])
        for forbidden in (b"mkdir", b"git clone", b"git checkout", b"git reset"):
            self.assertNotIn(forbidden, options["input"])
        self.assertFalse(value["automatic_context"]["workspace_exists"])
        self.assertIsNone(value["automatic_context"]["probe"]["source_checkout"])
        self.assertRegex(value["remote_workspace"], r"^/home/test/\.flflow/tasks/vllm/[0-9a-f]{16}/candidate$")
        self.assertNotIn("start", value)
        self.assertEqual(value["branch"], "base")

    def test_configured_backend_wins_without_vendor_guessing(self):
        self.update_profile(ssh_alias="metax-c550", hardware="Ascend 910B")
        value = self.normalize()
        self.assertEqual(value["backend"], "cuda")
        self.assertTrue(self.runner.call_args.args[0][-1].endswith(" 0"))

    def test_collected_hardware_overrides_old_profile_and_still_checks_task_connection(self):
        self.update_profile(backend="metax", hardware="MetaX C550")
        lookup = Mock(return_value="cuda")
        value = automatic.normalize_automatic_task(
            self.project, self.payload, runner=self.runner, ssh_executable="mock-ssh", backend_lookup=lookup)
        self.assertEqual(lookup.call_args.args[0]["id"], "gpu")
        self.assertEqual(value["backend"], "cuda")
        self.assertEqual(value["automatic_context"]["backend_source"], "device_collection")
        self.assertTrue(self.runner.call_args.args[0][-1].endswith(" 0"))
        self.assertEqual(self.runner.call_args.args[0][-2], "selected-gpu")
        self.assertTrue(value["remote_workspace"].startswith("/home/test/.flflow/tasks/"))
        self.assertEqual(self.manager.snapshot()["machine_profiles"][0]["backend"], "metax")

    def test_missing_trusted_observation_reprobes_jumpserver_name_hint(self):
        self.update_profile(backend="metax", backend_source=automatic.JUMPSERVER_BACKEND_SOURCE)
        self.runner.return_value = output(hardware="NVIDIA GPU: H100")
        value = automatic.normalize_automatic_task(
            self.project, self.payload, runner=self.runner, ssh_executable="mock-ssh",
            backend_lookup=Mock(return_value=None))
        self.assertEqual(value["backend"], "cuda")
        self.assertEqual(value["automatic_context"]["backend_source"], "ssh_hardware")
        self.assertTrue(self.runner.call_args.args[0][-1].endswith(" 1"))

    def test_unsupported_collected_hardware_cannot_create_or_probe(self):
        for backend in ("rocm", "unrecognized", {"backend": "cuda"}):
            with self.subTest(backend=backend), self.assertRaisesRegex(SessionError, "collected device backend"):
                automatic.normalize_automatic_task(
                    self.project, self.payload, runner=self.runner,
                    backend_lookup=Mock(return_value=backend))
        self.runner.assert_not_called()
        self.assertFalse(self.manager._sessions)

    def test_browser_cannot_supply_hardware_observation(self):
        lookup = Mock(return_value="cuda")
        for field, value in (("backend", "cuda"), ("device_observation", {"verified": True}),
                             ("backend_lookup", "cuda")):
            with self.subTest(field=field), self.assertRaisesRegex(SessionError, "accept only"):
                automatic.normalize_automatic_task(
                    self.project, {**self.payload, field: value}, runner=self.runner, backend_lookup=lookup)
        lookup.assert_not_called()
        self.runner.assert_not_called()

    def test_manual_mode_does_not_consult_collected_hardware(self):
        lookup = Mock(return_value="cuda")
        body = {"profile_id": "gpu", "backend": "metax"}
        self.assertEqual(automatic.normalize_automatic_task(
            self.project, body, runner=self.runner, backend_lookup=lookup), body)
        lookup.assert_not_called()
        self.runner.assert_not_called()

    def test_alias_classification_is_only_a_hint_and_actual_hardware_wins(self):
        from fl_dashboard.tasks.device_recognition import ALIAS_BACKEND_SOURCE
        self.update_profile(backend="metax", backend_source=ALIAS_BACKEND_SOURCE, ssh_alias="metax_c550")
        self.runner.return_value = output(hardware="NVIDIA GPU: H20")
        value = self.normalize()
        self.assertEqual(value["backend"], "cuda")
        self.assertEqual(value["automatic_context"]["backend_source"], "ssh_hardware")
        self.assertTrue(self.runner.call_args.args[0][-1].endswith(" 1"))
        self.assertEqual(self.manager.snapshot()["machine_profiles"][0]["backend"], "metax")

    def test_imported_classification_survives_owner_storage_and_task_resolution(self):
        from fl_dashboard.tasks.ssh_profile_sync import SSHProfileSync
        from fl_dashboard.tasks.device_recognition import ALIAS_BACKEND_SOURCE
        config = self.base / "ssh-config"
        config.write_text("Host h20_lab\n HostName example.test\n", encoding="utf-8")
        snapshot = SSHProfileSync(config).synchronize(self.manager, self.manager.snapshot())
        imported = next(profile for profile in snapshot["machine_profiles"] if profile["ssh_alias"] == "h20_lab")
        persisted = next(profile for profile in self.manager.snapshot()["machine_profiles"] if profile["id"] == imported["id"])
        self.assertEqual(persisted["backend"], "cuda")
        self.assertEqual(persisted["backend_source"], ALIAS_BACKEND_SOURCE)
        self.runner.return_value = output(hardware="MetaX C550")
        value = self.normalize({**self.payload, "profile_id": imported["id"]})
        self.assertEqual(value["backend"], "metax")
        self.assertEqual(value["ssh_alias"], "h20_lab")
        self.assertEqual(value["automatic_context"]["backend_source"], "ssh_hardware")
        self.assertFalse(self.manager._sessions)

    def test_alias_classification_cannot_skip_unknown_actual_hardware(self):
        from fl_dashboard.tasks.device_recognition import ALIAS_BACKEND_SOURCE
        self.update_profile(backend="cuda", backend_source=ALIAS_BACKEND_SOURCE, ssh_alias="h20_lab")
        self.runner.return_value = output(hardware="Unknown accelerator")
        with self.assertRaisesRegex(SessionError, "实际硬件"):
            self.normalize()
        self.assertFalse(self.manager._sessions)

    def test_unknown_backend_uses_actual_accelerator_not_alias(self):
        self.update_profile(backend="", ssh_alias="metax-c550", label="NVIDIA A100")
        self.runner.return_value = output(hardware="NVIDIA GPU: H100")
        value = self.normalize()
        self.assertEqual(value["backend"], "cuda")
        self.assertEqual(value["automatic_context"]["backend_source"], "ssh_hardware")
        self.assertTrue(self.runner.call_args.args[0][-1].endswith(" 1"))

    def test_unknown_actual_vendor_rejects_misleading_alias_and_hardware_hint(self):
        self.update_profile(backend="", ssh_alias="a100", hardware="NVIDIA A100")
        self.runner.return_value = output(hardware="Unknown accelerator device 1234")
        with self.assertRaisesRegex(SessionError, "实际硬件"):
            self.normalize()
        self.assertFalse(self.manager._sessions)

    def test_conflicting_saved_hardware_is_explicit_error(self):
        self.update_profile(backend="", hardware="MetaX C550")
        self.runner.return_value = output(hardware="NVIDIA GPU: H100")
        with self.assertRaisesRegex(SessionError, "冲突"):
            self.normalize()

    def test_ambiguous_actual_hardware_and_cpu_only_information_fail(self):
        self.update_profile(backend="")
        for hardware in ("NVIDIA GPU: H100\nHuawei Processing accelerators Ascend", ""):
            self.runner.return_value = output(hardware=hardware)
            with self.assertRaisesRegex(SessionError, "实际硬件"):
                self.normalize()
        # CPU/NIC vendor lines are excluded before hardware parsing.
        self.assertIn(b"grep -Ei 'VGA|3D controller|Display controller|Processing accelerators|Co-processor'",
                      self.runner.call_args.kwargs["input"])

    def test_missing_source_and_existing_source_both_prepare_future_candidate(self):
        self.runner.return_value = output(source=True)
        value = self.normalize()
        self.assertEqual(value["automatic_context"]["probe"]["source_checkout"]["commit"], "a" * 40)
        self.assertNotEqual(value["remote_workspace"], "/home/test/repos/plugin")
        self.assertIn("只读基线", value["goal"])
        self.assertIn("tmux", value["goal"])

    def test_probe_error_never_exposes_raw_ssh_output(self):
        for result in (SimpleNamespace(returncode=1, stdout=b"private", stderr=b"ProxyCommand token=private_secret"),
                       SimpleNamespace(returncode=1, stdout=b"", stderr=b"Permission denied private_secret"),
                       output(home="relative"),
                       SimpleNamespace(returncode=0, stdout=b"x" * 16385, stderr=b"")):
            self.runner.return_value = result
            with self.assertRaises(SessionError) as raised:
                self.normalize()
            self.assertNotIn("private", str(raised.exception))
            self.assertLess(len(str(raised.exception)), 300)
        self.runner.side_effect = subprocess.TimeoutExpired("secret-command", 20)
        with self.assertRaisesRegex(SessionError, "20 秒"):
            self.normalize()

    def test_noncompute_unknown_profile_and_extra_fields_fail_without_ssh(self):
        for body in ({**self.payload, "profile_id": "absent"}, {**self.payload, "remote_workspace": "/escape"},
                     {**self.payload, "start": "yes"}):
            with self.assertRaises(SessionError):
                self.normalize(body)
        self.update_profile(role="gateway")
        with self.assertRaisesRegex(SessionError, "compute"):
            self.normalize()
        self.runner.assert_not_called()

    def test_proof_is_single_use_owner_bound_and_payload_bound(self):
        for mutate in (lambda value: value.update(branch="other"),
                       lambda value: value.update(remote_workspace="/different"),
                       lambda value: value["automatic_context"].update(workspace_exists=True)):
            value = self.normalize()
            mutate(value)
            with self.assertRaisesRegex(SessionError, "proof"):
                automatic.consume_automatic_task(value, self.manager)
        value = self.normalize()
        with self.assertRaisesRegex(SessionError, "proof"):
            automatic.consume_automatic_task(value, object())
        value = self.normalize()
        clean, context = automatic.consume_automatic_task(value, self.manager)
        self.assertNotIn("_automatic_proof", clean)
        self.assertNotIn("automatic_context", clean)
        self.assertFalse(context["workspace_exists"])
        with self.assertRaisesRegex(SessionError, "proof"):
            automatic.consume_automatic_task(value, self.manager)
        value = self.normalize()
        with patch("fl_dashboard.tasks.automatic_tasks.time.monotonic", return_value=10**12), self.assertRaisesRegex(SessionError, "expired"):
            automatic.consume_automatic_task(value, self.manager)

    def test_manual_strict_preflight_remains_required(self):
        self.manual_profile()
        self.manager._check_connection = Mock(return_value={"ok": False, "error": "directory missing"})
        with self.assertRaisesRegex(SessionError, "directory missing"):
            self.manager.create({"profile_id": "gpu", "remote_workspace": "/missing"})
        self.manager._check_connection.assert_called_once()
        self.manager._git.assert_not_called()

    def test_future_local_workspace_creates_and_starts_without_strict_probe_or_proof_leak(self):
        source_config = self.manual_profile()
        self.manager._check_connection = Mock(side_effect=AssertionError("future directory must not be prechecked"))
        value = self.normalize()
        token = value["_automatic_proof"]
        session = self.manager.create(value)
        self.assertFalse(session["preflight_required"])
        self.assertFalse(session["preflight"]["workspace_exists"])
        self.assertEqual(session["preflight"]["scope"], "home_and_optional_source_only")
        self.assertNotEqual(session["ssh_config_path"], source_config)
        self.assertIn("task-local session SSH argv", session["goal"])
        self.assertIn(json.dumps(session["ssh_config_path"])[1:-1], session["prompt"])
        frozen = Path(session["ssh_config_path"]).read_text(encoding="utf-8")
        self.update_profile(ssh_connection={"host": "changed.example.test", "user": "test"})
        self.assertEqual(Path(session["ssh_config_path"]).read_text(encoding="utf-8"), frozen)
        self.manager.start(session["id"])
        self.manager._workers[session["id"]].join(3)
        self.assertFalse(self.manager._workers[session["id"]].is_alive())
        self.manager._check_connection.assert_not_called()
        for path in self.manager.root.rglob("*.json"):
            self.assertNotIn(token, path.read_text(encoding="utf-8"))
        self.assertNotIn("_automatic_proof", json.dumps(self.manager.snapshot()))

    def test_forged_local_context_fails_before_git_or_ssh(self):
        self.manager._check_connection = Mock()
        for body in ({"automatic_context": {"probe": {"ok": True}}},
                     {"_automatic_proof": "forged", "automatic_context": {"probe": {"ok": True}}}):
            with self.assertRaisesRegex(SessionError, "proof"):
                self.manager.create({"backend": "cuda", "ssh_alias": "gpu", "remote_workspace": "/future", **body})
        self.manager._check_connection.assert_not_called()
        self.manager._git.assert_not_called()

    def test_claude_permissions_are_explicit_and_never_changed(self):
        claude = ProviderManager({**self.config, "sessions_dir": "runs/claude"}, self.base, "claude")
        self.managers.append(claude)
        self.mock_git(claude)
        store = Mock()
        store.snapshot.return_value = {"configured": True, "allowed_tools": ["Read"]}
        claude.agent_settings = store
        self.project.sessions = CompositeSessionManager(self.manager, claude, store)
        with self.assertRaisesRegex(SessionError, "Bash"):
            self.normalize({**self.payload, "provider": "claude"})
        self.runner.assert_not_called()
        store.update.assert_not_called()

    def test_all_three_provider_dispatch_consumes_proof_on_real_target_and_shares_lease(self):
        claude = ProviderManager({**self.config, "sessions_dir": "runs/claude"}, self.base, "claude")
        flagscale = ProviderManager({**self.config, "sessions_dir": "runs/flagscale"}, self.base, "flagscale")
        self.managers += [claude, flagscale]
        for manager in (claude, flagscale):
            self.mock_git(manager)
        self.project.sessions = CompositeSessionManager(self.manager, claude, claude.agent_settings,
                                                       flagscale=flagscale, flagscale_settings=flagscale.agent_settings)
        value = self.normalize({**self.payload, "provider": "flagscale"})
        session = self.project.sessions.create(value)
        self.assertEqual(session["provider"], "flagscale")
        self.assertFalse(self.manager._sessions)
        self.assertFalse(claude._sessions)
        with self.assertRaisesRegex(SessionError, "active or prepared"):
            self.project.sessions.create(self.normalize({**self.payload, "provider": "claude"}))

    def test_legacy_remote_owner_receives_plain_payload_and_real_manual_config_argv(self):
        config_path = self.manual_profile()
        remote = RemoteSessionManager({"sessions_api_url": "http://127.0.0.1:1",
                                       "sessions_dir": str(self.manager.root),
                                       "evidence_dir": str(self.base / "evidence"), "branch": "base"}, self.base)
        remote.snapshot = Mock(return_value={"sessions": [], "machine_profiles": [copy.deepcopy(self.profile)]})
        remote._request = Mock(return_value={"id": "b" * 32, "status": "prepared"})
        self.project.sessions = remote
        value = self.normalize()
        self.assertIsNone(value["ssh_connection"])
        self.assertFalse(value["require_preflight"])
        self.assertNotIn("_automatic_proof", value)
        self.assertNotIn("automatic_context", value)
        self.assertIn(config_path.replace("\\", "\\\\"), value["goal"])
        self.assertIn("旧 owner", value["goal"])
        self.assertEqual(self.runner.call_args.args[0][1:3], ["-F", str(Path(config_path).resolve())])
        remote.create(value)
        body = remote._request.call_args.args[1]
        self.assertIsNone(body["ssh_connection"])
        self.assertIs(body["start"], False)
        self.assertEqual(self.profile["ssh_connection"]["host"], "gpu.example.test")

    def serve(self, *, shared=False):
        if shared:
            cache = SimpleNamespace(monitor=SimpleNamespace(config={}, branch="base"))
            server = make_shared_server(cache, "127.0.0.1", 0)
        else:
            server = flflow.make_server(None, "127.0.0.1", 0, registry=ProjectRegistry([self.project]))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.servers.append((server, worker))
        return server

    def request(self, server, body):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("POST", "/api/codex-sessions", json.dumps(body), {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_automatic_create_consumes_proof_and_explicit_start(self):
        server = self.serve()
        with patch("fl_dashboard.tasks.automatic_tasks.subprocess.run", self.runner), patch("fl_dashboard.tasks.automatic_tasks.shutil.which", return_value="mock-ssh"):
            code, result = self.request(server, {**self.payload, "start": True})
        self.assertEqual(code, 200, result)
        self.manager._workers[result["id"]].join(3)
        self.assertFalse(result["preflight_required"])
        self.assertNotIn("_automatic_proof", json.dumps(result))
        self.assertEqual(self.runner.call_count, 1)

    def test_http_manual_passthrough_and_forged_proof_no_ssh(self):
        server = self.serve()
        with patch("fl_dashboard.tasks.automatic_tasks.subprocess.run", self.runner):
            code, result = self.request(server, {"backend": "cuda", "ssh_alias": "gpu", "remote_workspace": "/work"})
            self.assertEqual(code, 200, result)
            for body in ({"_automatic_proof": "forged"}, {"automatic_context": {"probe": {"ok": True}}}):
                self.assertEqual(self.request(server, body)[0], 400)
        self.runner.assert_not_called()

    def test_http_automatic_is_unavailable_on_shared_dashboard(self):
        server = self.serve(shared=True)
        with patch("fl_dashboard.tasks.automatic_tasks.subprocess.run", self.runner):
            code, result = self.request(server, self.payload)
        self.assertEqual(code, 405, result)
        self.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
