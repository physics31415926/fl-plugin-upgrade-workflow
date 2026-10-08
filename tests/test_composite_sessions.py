import copy
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from codex_sessions import SessionError, require_free_machine
from composite_sessions import CompositeSessionManager
import flflow
from project_registry import ProjectContext, ProjectRegistry, RemoteSessionManager


class FakeSettings:
    def __init__(self, directory=None):
        self.directory = directory
        self.calls = []
        self.value = {"provider": "claude", "base_url": "https://api.example.test", "model": "sonnet",
                      "permission_mode": "acceptEdits", "allowed_tools": ["Read"], "max_turns": 20,
                      "configured": False, "api_protocol": "anthropic", "api_key": "explicit_dummy_key"}

    def snapshot(self):
        return dict(self.value)

    def update(self, payload):
        self.calls.append(copy.deepcopy(payload))
        self.value.update({key: value for key, value in payload.items() if key != "api_key"})
        self.value["configured"] = bool(payload.get("api_key"))
        return dict(self.value)


class FakeOwner:
    def __init__(self, provider, root):
        self.provider = provider
        self.config = {"branch": "upgrade", "engine": "vllm"}
        self.root = root / provider
        self.evidence_dir = root / "evidence"
        self.base_dir = root
        self.default_branch = "upgrade"
        self.rows, self.profiles, self.calls = [], [], []
        self.machine_lock = threading.RLock()
        self.peer_managers = [self]
        self.closed = False

    def snapshot(self, compact=False):
        return {"available": True, "default_branch": "upgrade", "engine": "vllm", "project_label": "Test plugin",
                "sessions": copy.deepcopy(self.rows), "machine_profiles": copy.deepcopy(self.profiles), "health": None}

    def allocation_sessions(self):
        return copy.deepcopy(self.rows)

    def create(self, payload):
        self.calls.append(("create", copy.deepcopy(payload)))
        require_free_machine(payload, self.peer_managers)
        row = {"id": ("a" if self.provider == "codex" else "b") * 32, "provider": self.provider,
               "status": "prepared", "backend": payload["backend"], "branch": payload.get("branch", "upgrade"),
               "ssh_alias": payload["ssh_alias"], "ssh_connection": None, "created_at": "2026-01-02T00:00:00Z"}
        self.rows.append(row)
        return row

    def save_profile(self, payload):
        self.calls.append(("save_profile", copy.deepcopy(payload)))
        self.profiles = [row for row in self.profiles if row["id"] != payload["id"]] + [copy.deepcopy(payload)]
        return copy.deepcopy(payload)

    def check_machine(self, payload):
        self.calls.append(("check_machine", copy.deepcopy(payload)))
        return {"ok": True}

    def action(self, action, session_id, *args, **kwargs):
        self.calls.append((action, session_id, args, kwargs))
        return {"id": session_id}

    def start(self, session_id): return self.action("start", session_id)
    def message(self, session_id, text): return self.action("message", session_id, text)
    def interrupt(self, session_id): return self.action("interrupt", session_id)
    def respond(self, session_id, request_id, response): return self.action("respond", session_id, request_id, response)
    def event_history(self, session_id, **kwargs): return self.action("event_history", session_id, **kwargs)

    def health(self):
        return {"available": True, "authenticated": self.provider == "codex", "version": "1.0", "error": None,
                **({"configured": False, "provider": "claude"} if self.provider == "claude" else {})}

    def close(self): self.closed = True


class CompositeSessionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex, self.claude = FakeOwner("codex", self.root), FakeOwner("claude", self.root)
        self.settings = FakeSettings()
        self.manager = CompositeSessionManager(self.codex, self.claude, self.settings)
        self.payload = {"backend": "cuda", "branch": "upgrade", "ssh_alias": "first-gpu"}
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.worker.join(3)
        self.temp.cleanup()

    def serve(self):
        project = ProjectContext("vllm", "vLLM", sessions=self.manager, agent_settings=self.settings)
        self.server = flflow.make_server(None, "127.0.0.1", 0, registry=ProjectRegistry([project]))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def request(self, path, body=None, *, origin=None, host=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            if origin: headers["Origin"] = origin
            if host: headers["Host"] = host
            connection.request("POST" if body is not None else "GET", path,
                               body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_default_codex_and_explicit_claude_create_use_distinct_owners(self):
        self.assertEqual(self.manager.create(self.payload)["provider"], "codex")
        self.assertNotIn("provider", self.codex.calls[-1][1])
        row = self.manager.create({**self.payload, "provider": "claude", "ssh_alias": "second-gpu"})
        self.assertEqual(row["provider"], "claude")
        self.assertEqual(self.claude.calls[-1][1]["provider"], "claude")
        self.assertEqual(len(self.codex.rows), 1)

    def test_unknown_provider_fails_before_either_owner_is_called(self):
        with self.assertRaisesRegex(SessionError, "provider"):
            self.manager.create({**self.payload, "provider": "other"})
        self.assertEqual(self.codex.calls + self.claude.calls, [])

    def test_controls_and_events_follow_session_owner_including_legacy_missing_provider(self):
        self.codex.rows = [{"id": "a" * 32, "status": "completed"}]
        self.claude.rows = [{"id": "b" * 32, "status": "completed"}]
        self.manager.start("a" * 32)
        self.manager.start("b" * 32)
        self.manager.message("b" * 32, "continue")
        self.manager.interrupt("b" * 32)
        self.manager.respond("b" * 32, 4, {"decision": "decline"})
        self.manager.event_history("b" * 32, cursor="cursor", limit=2)
        self.assertEqual([call[0] for call in self.codex.calls], ["start"])
        self.assertEqual([call[0] for call in self.claude.calls], ["start", "message", "interrupt", "respond", "event_history"])
        for tid in ("native-thead", "11111111-1111-1111-1111-111111111111", "c" * 32):
            with self.assertRaises(SessionError): self.manager.start(tid)

    def test_ambiguous_owner_is_rejected_instead_of_guessing(self):
        self.codex.rows = self.claude.rows = [{"id": "a" * 32, "status": "completed"}]
        with self.assertRaisesRegex(SessionError, "ambiguous"):
            self.manager.interrupt("a" * 32)
        self.assertFalse(self.codex.calls or self.claude.calls)

    def test_selected_old_profile_is_copied_as_configuration_not_credentials_or_check_results(self):
        original = {"id": "machine", "label": "GPU", "backend": "cuda", "ssh_alias": "first-gpu",
                    "remote_workspace": "/task", "model_paths": {}, "role": "compute",
                    "ssh_connection": {"host": "gpu.example.test", "user": "compute", "port": 22,
                                       "identity_file": "/path/to/key", "jump_alias": "jump"},
                    "ssh_config_path": "/old/store/ssh.conf", "last_check": {"ok": True}, "private_key": "never copy"}
        self.codex.profiles = [copy.deepcopy(original)]
        self.manager.create({**self.payload, "profile_id": "machine", "provider": "claude"})
        copied = self.claude.calls[0][1]
        self.assertEqual(copied["ssh_connection"]["identity_file"], "/path/to/key")
        for field in ("private_key", "ssh_config_path", "last_check"):
            self.assertNotIn(field, copied)
        self.assertEqual(self.codex.profiles[0], original)
        self.assertEqual([call[0] for call in self.codex.calls], [])

    def test_prepared_machine_lease_is_shared_across_providers(self):
        self.manager.create(self.payload)
        with self.assertRaisesRegex(SessionError, "machine"):
            self.manager.create({**self.payload, "provider": "claude"})
        self.assertFalse(self.claude.rows)
        self.assertIs(self.codex.machine_lock, self.claude.machine_lock)
        self.assertEqual({row["provider"] for row in self.manager.allocation_sessions()}, {"codex"})

    def test_unavailable_live_owner_blocks_machine_allocations(self):
        self.codex.allocation_sessions = Mock(side_effect=SessionError("offline"))
        with self.assertRaisesRegex(SessionError, "offline"):
            self.manager.allocation_sessions()
        snapshot = self.manager.snapshot()
        self.assertIn("providers", snapshot)

    def test_local_claude_can_still_be_interrupted_when_codex_owner_is_offline(self):
        self.codex.allocation_sessions = Mock(side_effect=SessionError("offline"))
        self.claude.rows = [{"id": "b" * 32, "status": "running"}]
        result = self.manager.interrupt("b" * 32)
        self.assertEqual(result["provider"], "claude")
        self.assertEqual(self.claude.calls[0][0], "interrupt")
        self.assertFalse(self.codex.calls)

    def test_snapshot_merges_profiles_and_safe_settings_without_leaking_key(self):
        self.codex.profiles = [{"id": "same", "label": "current"}]
        self.claude.profiles = [{"id": "same", "label": "older"}, {"id": "claude-only", "label": "extra"}]
        snapshot = self.manager.snapshot(compact=True)
        self.assertEqual([profile["label"] for profile in snapshot["machine_profiles"]], ["current", "extra"])
        self.assertEqual(set(snapshot["providers"]), {"codex", "claude"})
        self.assertFalse(snapshot["providers"]["claude"]["configured"])
        self.assertNotIn("explicit_dummy_key", json.dumps(snapshot))
        self.assertNotIn("api_key", snapshot["agent_settings"])

    def test_health_preserves_codex_fields_and_adds_both_provider_results(self):
        value = self.manager.health()
        self.assertTrue(value["authenticated"])
        self.assertEqual(value["version"], "1.0")
        self.assertFalse(value["providers"]["claude"]["configured"])

    def test_new_claude_task_is_not_hidden_by_old_codex_native_replacement(self):
        native = Mock(snapshot=Mock(return_value=[{"id": "native-thead", "backend": "thead", "branch": "upgrade"}]))
        project = ProjectContext("vllm", "vLLM", native_tasks=native)
        rows = project.adaptation_tasks(session_rows=[
            {"id": "a" * 32, "backend": "thead", "branch": "upgrade", "provider": "codex"},
            {"id": "b" * 32, "backend": "thead", "branch": "upgrade", "provider": "claude"}])
        self.assertEqual(rows[1]["provider"], "claude")
        self.assertFalse(rows[1]["historical"])
        self.assertTrue(rows[2]["historical"])

    def test_profile_edits_stay_in_original_codex_store(self):
        self.manager.save_profile({"id": "machine", "provider": "claude", "label": "edited"})
        self.assertEqual(self.codex.profiles[0]["label"], "edited")
        self.assertFalse(self.claude.calls)
        self.assertNotIn("provider", self.codex.calls[0][1])

    def test_settings_http_returns_only_safe_fields_and_requires_local_origin(self):
        self.serve()
        self.assertEqual(self.request("/api/agent-settings")[0], 200)
        code, value = self.request("/api/agent-settings", {"provider": "claude", "api_key": "explicit_dummy_request_key", "model": "opus"})
        self.assertEqual(code, 200)
        self.assertTrue(value["configured"])
        self.assertNotIn("api_key", value)
        self.assertNotIn("explicit_dummy", json.dumps(value))
        before = len(self.settings.calls)
        self.assertEqual(self.request("/api/agent-settings", {}, origin="http://outside.example")[0], 403)
        self.assertEqual(self.request("/api/agent-settings", host="outside.example")[0], 403)
        self.assertEqual(len(self.settings.calls), before)
        self.assertEqual(set(self.request("/api/agent-providers")[1]["providers"]), {"codex", "claude"})

    def test_http_claude_creation_and_control_never_call_codex_executor(self):
        self.serve()
        code, row = self.request("/api/codex-sessions", {**self.payload, "provider": "claude", "start": True})
        self.assertEqual(code, 200)
        self.assertEqual(row["provider"], "claude")
        self.assertEqual([call[0] for call in self.claude.calls], ["create", "start"])
        self.assertFalse(self.codex.calls)
        self.assertEqual(self.request("/api/codex-sessions/native-thead/start", {})[0], 400)

    def test_registry_readonly_never_constructs_secret_store_or_any_provider(self):
        config = self.root / "projects.json"
        config.write_text(json.dumps({"projects": [{"id": "vllm", "label": "vLLM", "monitor_config": "monitor.json"}]}), encoding="utf8")
        (self.root / "monitor.json").write_text(json.dumps({"repo": ".", "branch": "upgrade"}), encoding="utf8")
        settings, codex, claude = Mock(), Mock(), Mock()
        registry = ProjectRegistry.from_config(config, read_only=True, monitor_factory=lambda cfg: Mock(config=cfg),
                    sessions_factory=codex, claude_sessions_factory=claude, settings_factory=settings)
        settings.assert_not_called()
        codex.assert_not_called()
        claude.assert_not_called()
        self.assertIsNone(registry.projects[0].agent_settings)
        self.assertIsNone(registry.projects[0].sessions)

    def test_registry_remote_codex_and_local_claude_keep_separate_stores_and_shared_leaf_locks(self):
        config = self.root / "projects.json"
        config.write_text(json.dumps({"projects": [{"id": "vllm", "label": "vLLM", "monitor_config": "monitor.json"}]}), encoding="utf8")
        (self.root / "monitor.json").write_text(json.dumps({"repo": ".", "branch": "upgrade", "sessions_api_url": "http://127.0.0.1:8765"}), encoding="utf8")
        factory = Mock(return_value=self.claude)
        registry = ProjectRegistry.from_config(config, monitor_factory=lambda cfg: Mock(config=cfg),
                    claude_sessions_factory=factory, settings_factory=lambda directory: self.settings)
        composite = registry.projects[0].sessions
        self.assertIsInstance(composite.codex, RemoteSessionManager)
        clause_config = factory.call_args.args[0]
        self.assertNotIn("sessions_api_url", clause_config)
        self.assertEqual(Path(clause_config["sessions_dir"]), self.root / "runs/vllm/claude-sessions")
        self.assertIs(composite.codex.machine_lock, self.claude.machine_lock)
        self.assertEqual(composite.codex.peer_managers, [composite.codex, self.claude])
        self.assertIs(registry.projects[0].agent_settings, self.settings)

    def test_remote_codex_directly_rejects_other_provider_before_http(self):
        owner = RemoteSessionManager({"sessions_api_url": "http://127.0.0.1:8765", "sessions_dir": str(self.root / "old"),
                                      "evidence_dir": str(self.root / "evidence")}, self.root)
        owner._request = Mock()
        with self.assertRaisesRegex(SessionError, "another"):
            owner.create({**self.payload, "provider": "claude"})
        owner._request.assert_not_called()


if __name__ == "__main__": unittest.main()
