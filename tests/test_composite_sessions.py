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


class FakeFlagScaleSettings(FakeSettings):
    def __init__(self, directory=None):
        super().__init__(directory)
        self.value = {"provider": "flagscale", "llm_provider": "openai",
                      "base_url": "https://api.example.test", "model": "test-model",
                      "max_iterations": 100, "max_continuations": 10, "max_output_tokens": 8192,
                      "thinking_budget": 0, "time_budget_sec": 60.0, "max_context_tokens": 128000,
                      "configured": True, "key_storage": "protected", "api_protocol": "openai",
                      "api_key": "explicit_dummy_flagscale_key", "private_config": "never expose"}


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
        row = {"id": {"codex": "a", "claude": "b", "flagscale": "c"}[self.provider] * 32, "provider": self.provider,
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

    def test_desktop_continuation_routes_original_codex_and_rejects_other_provider(self):
        self.codex.rows = [{"id": "a" * 32, "status": "completed"}]
        self.claude.rows = [{"id": "b" * 32, "status": "completed"}]
        self.codex.desktop = Mock(return_value={"session": {"id": "a" * 32}, "desktop_url": "codex://threads/11111111-1111-1111-1111-111111111111"})
        self.claude.desktop = Mock()
        result = self.manager.desktop("a" * 32)
        self.assertEqual(result["session"]["id"], "a" * 32)
        self.codex.desktop.assert_called_once_with("a" * 32)
        with self.assertRaisesRegex(SessionError, "Only Codex"):
            self.manager.desktop("b" * 32)
        self.claude.desktop.assert_not_called()
        with self.assertRaisesRegex(SessionError, "observation-only"):
            self.manager.desktop("native-musa")

    def test_desktop_route_rejects_client_urls_and_cross_origin(self):
        self.codex.rows = [{"id": "a" * 32, "status": "completed"}]
        self.codex.desktop = Mock(return_value={"session": {"id": "a" * 32}})
        self.serve()
        path = "/api/codex-sessions/" + "a" * 32 + "/desktop?project=vllm"
        self.assertEqual(self.request(path, {"url": "codex://threads/arbitrary"})[0], 400)
        self.assertEqual(self.request(path, {}, origin="https://untrusted.example.test")[0], 403)
        self.codex.desktop.assert_not_called()
        status, result = self.request(path, {})
        self.assertEqual(status, 200)
        self.assertEqual(result["session"]["id"], "a" * 32)
        self.codex.desktop.assert_called_once_with("a" * 32)

    def test_older_owner_reports_update_required_without_creating_task(self):
        self.codex.rows = [{"id": "a" * 32, "status": "completed"}]
        with self.assertRaisesRegex(SessionError, "Update the original"):
            self.manager.desktop("a" * 32)
        self.assertEqual(self.codex.calls, [])

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
        settings, codex, claude, flagscale, flagscale_settings = Mock(), Mock(), Mock(), Mock(), Mock()
        registry = ProjectRegistry.from_config(config, read_only=True, monitor_factory=lambda cfg: Mock(config=cfg),
                    sessions_factory=codex, claude_sessions_factory=claude, settings_factory=settings,
                    flagscale_sessions_factory=flagscale, flagscale_settings_factory=flagscale_settings)
        settings.assert_not_called()
        codex.assert_not_called()
        claude.assert_not_called()
        flagscale.assert_not_called()
        flagscale_settings.assert_not_called()
        self.assertIsNone(registry.projects[0].flagscale_settings)
        self.assertFalse((self.root / "runs").exists())
        self.assertIsNone(registry.projects[0].agent_settings)
        self.assertIsNone(registry.projects[0].sessions)

    def test_registry_remote_codex_and_local_claude_keep_separate_stores_and_shared_leaf_locks(self):
        config = self.root / "projects.json"
        config.write_text(json.dumps({"projects": [{"id": "vllm", "label": "vLLM", "monitor_config": "monitor.json"}]}), encoding="utf8")
        (self.root / "monitor.json").write_text(json.dumps({"repo": ".", "branch": "upgrade", "sessions_api_url": "http://127.0.0.1:8765"}), encoding="utf8")
        factory = Mock(return_value=self.claude)
        flagscale = FakeOwner("flagscale", self.root)
        flagscale_settings = FakeFlagScaleSettings()
        flagscale_factory = Mock(return_value=flagscale)
        registry = ProjectRegistry.from_config(config, monitor_factory=lambda cfg: Mock(config=cfg),
                    claude_sessions_factory=factory, settings_factory=lambda directory: self.settings,
                    flagscale_sessions_factory=flagscale_factory,
                    flagscale_settings_factory=lambda directory: flagscale_settings)
        composite = registry.projects[0].sessions
        self.assertIsInstance(composite.codex, RemoteSessionManager)
        clause_config = factory.call_args.args[0]
        self.assertNotIn("sessions_api_url", clause_config)
        self.assertEqual(Path(clause_config["sessions_dir"]), self.root / "runs/vllm/claude-sessions")
        self.assertIs(composite.codex.machine_lock, self.claude.machine_lock)
        self.assertEqual(composite.codex.peer_managers, [composite.codex, self.claude, flagscale])
        self.assertIs(registry.projects[0].flagscale_settings, flagscale_settings)
        flagscale_config = flagscale_factory.call_args.args[0]
        self.assertNotIn("sessions_api_url", flagscale_config)
        self.assertEqual(Path(flagscale_config["sessions_dir"]), self.root / "runs/vllm/flagscale-sessions")
        self.assertIs(flagscale_factory.call_args.kwargs["settings_store"], flagscale_settings)
        self.assertIs(flagscale.machine_lock, composite.codex.machine_lock)
        self.assertIs(registry.projects[0].agent_settings, self.settings)

    def test_remote_codex_directly_rejects_other_provider_before_http(self):
        owner = RemoteSessionManager({"sessions_api_url": "http://127.0.0.1:8765", "sessions_dir": str(self.root / "old"),
                                      "evidence_dir": str(self.root / "evidence")}, self.root)
        owner._request = Mock()
        with self.assertRaisesRegex(SessionError, "another"):
            owner.create({**self.payload, "provider": "claude"})
        owner._request.assert_not_called()



    def enable_flagscale(self):
        self.flagscale = FakeOwner("flagscale", self.root)
        self.flagscale_settings = FakeFlagScaleSettings()
        self.manager = CompositeSessionManager(self.codex, self.claude, self.settings,
                    flagscale=self.flagscale, flagscale_settings=self.flagscale_settings)

    def test_flagscale_create_and_controls_are_owned_only_by_flagscale(self):
        self.enable_flagscale()
        row = self.manager.create({**self.payload, "provider": "flagscale"})
        ident = row["id"]
        self.assertEqual(row["provider"], "flagscale")
        self.assertEqual(self.flagscale.calls[0][1]["provider"], "flagscale")
        self.manager.start(ident)
        self.manager.message(ident, "continue with saved context")
        self.manager.respond(ident, 4, {"decision": "decline"})
        self.manager.interrupt(ident)
        self.assertEqual(self.manager.event_history(ident, cursor="next", limit=1)["provider"], "flagscale")
        self.assertEqual([call[0] for call in self.flagscale.calls],
                         ["create", "start", "message", "respond", "interrupt", "event_history"])
        self.assertFalse(self.codex.calls or self.claude.calls)

    def test_unavailable_flagscale_never_falls_back_to_existing_executors(self):
        with self.assertRaisesRegex(SessionError, "flagscale.*unavailable"):
            self.manager.create({**self.payload, "provider": "flagscale"})
        self.assertFalse(self.codex.calls or self.claude.calls)
        manager = CompositeSessionManager(self.codex, self.claude, self.settings,
                    errors={"flagscale": "The FlagScale session owner is unavailable"})
        self.assertFalse(manager.snapshot()["providers"]["flagscale"]["available"])

    def test_flagscale_settings_are_independent_and_all_public_fields_are_filtered(self):
        self.enable_flagscale()
        self.manager.update_settings({"provider": "flagscale", "llm_provider": "anthropic",
                                      "api_key": "explicit_dummy_updated_key", "model": "other-model"})
        self.assertFalse(self.settings.calls)
        self.assertEqual(self.flagscale_settings.calls[0]["provider"], "flagscale")
        self.assertEqual(self.manager.settings()["provider"], "claude")
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["agent_settings"]["provider"], "claude")
        public = snapshot["agent_settings_by_provider"]["flagscale"]
        for field in ("llm_provider", "max_iterations", "max_continuations", "max_output_tokens",
                      "thinking_budget", "time_budget_sec", "max_context_tokens"):
            self.assertIn(field, public)
        self.assertEqual(public["model"], "other-model")
        self.assertEqual(public["llm_provider"], "anthropic")
        self.assertNotIn("api_key", public)
        self.assertNotIn("private_config", public)
        self.assertNotIn("explicit_dummy", json.dumps(snapshot))
        with self.assertRaisesRegex(SessionError, "settings provider"):
            self.manager.update_settings({"provider": "codex", "api_key": "explicit_dummy"})
        self.assertEqual(len(self.flagscale_settings.calls), 1)

    def test_flagscale_health_reports_fresh_turn_capabilities_without_native_resume(self):
        self.enable_flagscale()
        self.flagscale.health = Mock(return_value={"available": True, "capabilities": {
            "supports_resume": True, "supports_mid_turn_steer": True, "unrelated": True}})
        health = self.manager.health()["providers"]["flagscale"]
        self.assertEqual(health["follow_up_mode"], "fresh_turn_with_context")
        self.assertFalse(health["supports_resume"])
        self.assertFalse(health["supports_mid_turn_steer"])
        self.assertFalse(health["capabilities"]["supports_resume"])
        self.assertTrue(health["capabilities"]["unrelated"])
        snapshot = self.manager.snapshot()["providers"]["flagscale"]
        self.assertTrue(snapshot["configured"])
        self.assertFalse(snapshot["capabilities"]["supports_mid_turn_steer"])

    def test_flagscale_leases_and_profile_configuration_share_all_three_owners(self):
        self.enable_flagscale()
        self.codex.profiles = [{"id": "gpu", "backend": "cuda", "ssh_alias": "first-gpu",
                                "ssh_connection": None, "private_key": "never copy",
                                "last_check": {"ok": True}}]
        self.manager.create({**self.payload, "provider": "flagscale", "profile_id": "gpu"})
        self.assertEqual(self.flagscale.profiles[0]["id"], "gpu")
        self.assertNotIn("private_key", self.flagscale.profiles[0])
        self.assertNotIn("last_check", self.flagscale.profiles[0])
        with self.assertRaisesRegex(SessionError, "machine"):
            self.manager.create(self.payload)
        with self.assertRaisesRegex(SessionError, "machine"):
            self.manager.create({**self.payload, "provider": "claude"})
        self.assertFalse(self.codex.rows or self.claude.rows)
        self.assertEqual({row["provider"] for row in self.manager.allocation_sessions()}, {"flagscale"})
        self.assertIs(self.flagscale.machine_lock, self.codex.machine_lock)
        self.assertEqual(self.flagscale.peer_managers, [self.codex, self.claude, self.flagscale])

    def test_flagscale_owner_lookup_remains_unique_and_allocation_fails_closed(self):
        self.enable_flagscale()
        self.flagscale.rows = [{"id": "c" * 32, "status": "running"}]
        self.codex.allocation_sessions = Mock(side_effect=SessionError("offline"))
        self.claude.allocation_sessions = Mock(side_effect=SessionError("offline"))
        self.assertEqual(self.manager.interrupt("c" * 32)["provider"], "flagscale")
        with self.assertRaisesRegex(SessionError, "offline"):
            self.manager.allocation_sessions()
        self.codex.allocation_sessions = Mock(return_value=self.flagscale.rows)
        with self.assertRaisesRegex(SessionError, "ambiguous"):
            self.manager.start("c" * 32)
        self.assertEqual([call[0] for call in self.flagscale.calls], ["interrupt"])

    def test_flagscale_only_composite_has_no_implicit_codex_or_claude_owner(self):
        flagscale = FakeOwner("flagscale", self.root)
        manager = CompositeSessionManager(None, None, None, flagscale=flagscale,
                                          flagscale_settings=FakeFlagScaleSettings())
        self.assertEqual(manager.create({**self.payload, "provider": "flagscale"})["provider"], "flagscale")
        self.assertEqual(manager.snapshot()["sessions"][0]["provider"], "flagscale")
        manager.save_profile({"id": "gpu", "provider": "codex"})
        self.assertEqual(flagscale.calls[-1][0], "save_profile")
        manager.close()
        self.assertTrue(flagscale.closed)
        with self.assertRaisesRegex(SessionError, "codex.*unavailable"):
            manager.create(self.payload)

    def test_registry_shares_leases_across_flagscale_and_another_project(self):
        self.enable_flagscale()
        other_codex, other_claude = FakeOwner("codex", self.root / "other"), FakeOwner("claude", self.root / "other")
        other_flagscale = FakeOwner("flagscale", self.root / "other")
        other = CompositeSessionManager(other_codex, other_claude, FakeSettings(),
                  flagscale=other_flagscale, flagscale_settings=FakeFlagScaleSettings())
        registry = ProjectRegistry([ProjectContext("vllm", "vLLM", sessions=self.manager),
                                    ProjectContext("sglang", "SGLang", sessions=other)])
        self.manager.create(self.payload)
        with self.assertRaisesRegex(SessionError, "machine"):
            other.create({**self.payload, "provider": "flagscale"})
        self.assertFalse(other_flagscale.rows)
        self.assertIs(other_flagscale.machine_lock, self.codex.machine_lock)
        self.assertEqual(len(other_flagscale.peer_managers), 6)
        registry.close()
        self.assertTrue(all(manager.closed for manager in other_flagscale.peer_managers))

    def test_registry_keeps_settings_stores_separate_in_one_existing_directory(self):
        config = self.root / "projects.json"
        config.write_text(json.dumps({"projects": [{"id": "vllm", "label": "vLLM",
                                                   "monitor_config": "monitor.json"}]}), encoding="utf8")
        (self.root / "monitor.json").write_text(json.dumps({"repo": ".", "branch": "upgrade",
            "sessions_api_url": "http://127.0.0.1:8765", "sessions_api_project": "legacy"}), encoding="utf8")
        settings_factory, flagscale_settings_factory = Mock(side_effect=FakeSettings), Mock(side_effect=FakeFlagScaleSettings)
        flagscale_factory = Mock(return_value=FakeOwner("flagscale", self.root))
        registry = ProjectRegistry.from_config(config, monitor_factory=lambda cfg: Mock(config=cfg),
                    claude_sessions_factory=lambda *args, **kwargs: self.claude,
                    settings_factory=settings_factory, flagscale_settings_factory=flagscale_settings_factory,
                    flagscale_sessions_factory=flagscale_factory)
        project = registry.projects[0]
        self.assertIsNot(project.agent_settings, project.flagscale_settings)
        self.assertEqual(settings_factory.call_args.args[0], self.root / "runs/agent-settings")
        self.assertEqual(flagscale_settings_factory.call_args.args[0], settings_factory.call_args.args[0])
        local = flagscale_factory.call_args.args[0]
        self.assertFalse(any(key.startswith("sessions_api_") for key in local))
        self.assertEqual(Path(local["sessions_dir"]), self.root / "runs/vllm/flagscale-sessions")
        self.assertIs(flagscale_factory.call_args.kwargs["settings_store"], project.flagscale_settings)
        registry.close()


    def test_registry_overlapping_flagscale_store_never_constructs_a_provider(self):
        config = self.root / "projects.json"
        config.write_text(json.dumps({"projects": [{"id": "vllm", "label": "vLLM",
                                                   "monitor_config": "monitor.json"}]}), encoding="utf8")
        (self.root / "monitor.json").write_text(json.dumps({"repo": ".", "branch": "upgrade",
            "flagscale_sessions_dir": "runs/codex-sessions/inside"}), encoding="utf8")
        codex, claude, flagscale = Mock(), Mock(), Mock()
        registry = ProjectRegistry.from_config(config, monitor_factory=lambda cfg: Mock(config=cfg),
                    sessions_factory=codex, claude_sessions_factory=claude,
                    settings_factory=FakeSettings, flagscale_settings_factory=FakeFlagScaleSettings,
                    flagscale_sessions_factory=flagscale)
        self.assertIn("FlagScale session directory", registry.projects[0].monitor_error)
        self.assertIsNone(registry.projects[0].sessions)
        codex.assert_not_called()
        claude.assert_not_called()
        flagscale.assert_not_called()
        registry.close()

    def test_registry_failed_flagscale_provider_does_not_hide_healthy_existing_owners(self):
        config = self.root / "projects.json"
        config.write_text(json.dumps({"projects": [{"id": "vllm", "label": "vLLM",
                                                   "monitor_config": "monitor.json"}]}), encoding="utf8")
        (self.root / "monitor.json").write_text(json.dumps({"repo": ".", "branch": "upgrade"}), encoding="utf8")
        factory = Mock(side_effect=RuntimeError("private provider config"))
        registry = ProjectRegistry.from_config(config, monitor_factory=lambda cfg: Mock(config=cfg),
                    sessions_factory=lambda *args: self.codex, claude_sessions_factory=lambda *args, **kwargs: self.claude,
                    settings_factory=FakeSettings, flagscale_settings_factory=FakeFlagScaleSettings,
                    flagscale_sessions_factory=factory)
        snapshot = registry.projects[0].sessions.snapshot()
        self.assertTrue(snapshot["providers"]["codex"]["available"])
        self.assertTrue(snapshot["providers"]["claude"]["available"])
        self.assertFalse(snapshot["providers"]["flagscale"]["available"])
        self.assertNotIn("private provider config", json.dumps(snapshot))
        with self.assertRaisesRegex(SessionError, "flagscale.*unavailable"):
            registry.projects[0].sessions.create({**self.payload, "provider": "flagscale"})
        self.assertFalse(self.codex.calls or self.claude.calls)
        registry.close()


if __name__ == "__main__": unittest.main()
