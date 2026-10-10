from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import flflow
from project_registry import ProjectContext, ProjectError, ProjectRegistry, RemoteSessionManager
from codex_sessions import SessionError
from share_server import ProjectSnapshotCache, make_server as make_share_server


class FakeMonitor:
    def __init__(self, config):
        self.config = config
        self.branch = config["branch"]
        self.github_repo = config.get("github_repo", "")
        self.evidence_dir = Path(config["evidence_dir"])
        self.calls = []

    def status(self, branch=None):
        self.calls.append(("status", branch))
        if self.config.get("broken"):
            raise RuntimeError("private C:\\Users\\secret 192.168.1.20")
        return {"branch": branch or self.branch, "branches": [self.branch],
                "engine": self.config["engine"], "upstream_version": "0.5.0",
                "head": ("a" if self.config["engine"] == "vllm" else "b") * 40,
                "models": [], "modes": [], "backends": [],
                "source_thread": "secret-session-id", "repo": "C:\\Users\\secret"}

    def refresh_branch(self, branch=None):
        self.calls.append(("refresh", branch))
        return self.status(branch)

    def read_evidence(self, **fields):
        return {"engine": self.config["engine"], **fields}


class FakeSessions:
    def __init__(self, config, base):
        self.config = config
        self.root = Path(config["sessions_dir"])
        self.evidence_dir = Path(config["evidence_dir"])
        self.calls = []
        self.closed = False

    def snapshot(self, **kwargs):
        self.calls.append(("snapshot", kwargs))
        return {"sessions": [{"id": "1" * 32, "engine": self.config["engine"], "status": "prepared"}],
                "machines": [{"ssh_alias": "private-machine"}]}

    def create(self, body):
        self.calls.append(("create", body))
        return {"id": "1" * 32, "engine": self.config["engine"]}

    def start(self, session_id):
        self.calls.append(("start", session_id))
        return {"id": session_id, "engine": self.config["engine"]}

    def event_history(self, session_id, **kwargs):
        return {"id": session_id, "engine": self.config["engine"], **kwargs}

    def close(self):
        self.closed = True


class FakeFlagScaleSessions(FakeSessions):
    def __init__(self, config, base, *, settings_store=None):
        super().__init__(config, base)
        self.settings_store = settings_store

    def snapshot(self, **kwargs):
        value = super().snapshot(**kwargs)
        value["sessions"][0].update(id="3" * 32, provider="flagscale")
        return value


class FakeWorkflow:
    def __init__(self, path):
        self.path = path
        self.calls = []

    def status(self):
        return {"workflow": "vllm", "status": "idle"}

    def launch(self, **kwargs):
        self.calls.append(kwargs)


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "projects.json"
        self.config = {"projects": [
            {"id": "vllm", "label": "vLLM-Plugin-FL", "monitor_config": "vllm.json", "workflow_config": "workflow.json"},
            {"id": "sglang", "label": "SGLang-Plugin-FL", "monitor_config": "sglang.json"}]}
        self.monitors = {
            "vllm": {"repo": "repos/vllm", "branch": "adapt-vllm", "github_repo": "example/vllm-plugin"},
            "sglang": {"repo": "repos/sglang", "branch": "adapt-sglang", "github_repo": "example/sglang-plugin"}}
        self.registry = None
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.worker.join(3)
        if self.registry:
            self.registry.close()
        self.temp.cleanup()

    def load(self, *, read_only=False, sessions_factory=FakeSessions):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        for project, config in self.monitors.items():
            (self.root / (project + ".json")).write_text(json.dumps(config), encoding="utf-8")
        self.registry = ProjectRegistry.from_config(self.config_path, read_only=read_only,
                                                   monitor_factory=FakeMonitor, sessions_factory=sessions_factory,
                                                   workflow_factory=FakeWorkflow,
                                                   flagscale_sessions_factory=FakeFlagScaleSessions,
                                                   flagscale_settings_factory=lambda directory: Mock(snapshot=Mock(return_value={"provider": "flagscale", "configured": False})))
        return self.registry

    def serve(self, *, shared=False):
        if shared:
            self.cache = ProjectSnapshotCache(self.registry, refresh_interval=0)
            self.index = self.root / "index.html"
            self.index.write_text("<html>test</html>", encoding="utf-8")
            self.server = make_share_server(self.cache, "127.0.0.1", 0, index_path=self.index, base_path="/fl")
        else:
            self.server = flflow.make_server(None, "127.0.0.1", 0, registry=self.registry)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def request(self, path, *, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request("POST" if body is not None else "GET", path,
                               body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_runtime_paths_and_machine_allocator_are_project_scoped(self):
        registry = self.load()
        vllm, sglang = registry.projects
        self.assertEqual(vllm.sessions.root, self.root / "runs/codex-sessions")
        self.assertEqual(sglang.sessions.root, self.root / "runs/sglang/codex-sessions")
        self.assertEqual(vllm.sessions.evidence_dir, vllm.monitor.evidence_dir)
        self.assertNotEqual(vllm.sessions.evidence_dir, sglang.sessions.evidence_dir)
        self.assertIs(vllm.sessions.machine_lock, sglang.sessions.machine_lock)
        self.assertEqual(vllm.sessions.peer_managers, [vllm.sessions.codex, vllm.sessions.claude,
                                                     vllm.sessions.flagscale, sglang.sessions.codex,
                                                     sglang.sessions.claude, sglang.sessions.flagscale])
        self.assertEqual(vllm.sessions.flagscale.root, self.root / "runs/vllm/flagscale-sessions")
        self.assertEqual(sglang.sessions.flagscale.root, self.root / "runs/sglang/flagscale-sessions")
        self.assertIs(vllm.sessions.flagscale.machine_lock, sglang.sessions.codex.machine_lock)
        self.assertIs(vllm.flagscale_settings, sglang.flagscale_settings)
        self.assertEqual(sglang.monitor.config["repo"], str(self.root / "repos/sglang"))

    def test_invalid_ids_and_overlapping_runtime_paths_are_rejected(self):
        self.config["projects"][1]["id"] = "vllm"
        with self.assertRaisesRegex(ProjectError, "Duplicate"):
            self.load()
        self.config["projects"][1]["id"] = "sglang"
        self.monitors["sglang"]["evidence_dir"] = "runs/codex-sessions/inside"
        with self.assertRaisesRegex(ProjectError, "overlap"):
            self.load()
        del self.monitors["sglang"]["evidence_dir"]
        self.monitors["sglang"]["flagscale_sessions_dir"] = "runs/vllm/claude-sessions/inside"
        with self.assertRaisesRegex(ProjectError, "overlap"):
            self.load()

    def test_missing_monitor_and_runtime_failure_do_not_hide_peer(self):
        registry = self.load()
        registry.projects[1].monitor.config["broken"] = True
        rows = registry.statuses()["projects"]
        self.assertIsNotNone(rows[0]["status"])
        self.assertIsNone(rows[1]["status"])
        self.assertIn("private", rows[1]["error"])
        registry.close()
        self.config["projects"][1]["monitor_config"] = "absent.json"
        registry = self.load()
        self.assertIsNotNone(registry.projects[0].monitor)
        self.assertIsNone(registry.projects[1].monitor)
        self.assertIsNotNone(registry.statuses()["projects"][0]["status"])

    def test_existing_routes_select_context_for_get_post_and_history(self):
        self.load()
        self.serve()
        self.assertEqual(self.request("/api/branch-status")[1]["engine"], "vllm")
        self.assertEqual(self.request("/api/branch-status?project=sglang")[1]["engine"], "sglang")
        self.assertEqual(self.request("/api/codex-sessions?project=sglang&view=cards")[1]["sessions"][0]["engine"], "sglang")
        self.assertEqual(self.request("/api/codex-sessions?project=sglang", body={"start": True})[1]["engine"], "sglang")
        self.assertFalse(any(call[0] == "create" for call in self.registry.projects[0].sessions.codex.calls))
        self.assertEqual(self.request("/api/codex-sessions/" + "1" * 32 + "/events?project=sglang&limit=1")[1]["engine"], "sglang")
        self.assertEqual(self.request("/api/branch-refresh?project=sglang", body={})[1]["engine"], "sglang")
        self.assertFalse(any(call[0] == "refresh" for call in self.registry.projects[0].monitor.calls))
        self.assertEqual(self.request("/api/start?project=vllm", body={})[0], 202)
        self.assertEqual(self.request("/api/start?project=sglang", body={})[0], 503)
        self.assertEqual(self.request("/api/status?project=sglang")[0], 503)

    def test_unknown_or_duplicate_project_never_falls_back(self):
        self.load()
        self.serve()
        for query in ("project=unknown", "project=", "project=vllm&project=sglang"):
            self.assertEqual(self.request("/api/branch-status?" + query)[0], 400)
            self.assertEqual(self.request("/api/codex-sessions?" + query, body={})[0], 400)
        self.assertFalse(any(call[0] == "create" for project in self.registry.projects for call in project.sessions.codex.calls))

    def test_projects_overview_returns_compact_session_rows_and_capabilities(self):
        self.load()
        self.serve()
        metadata = self.request("/api/projects")[1]
        self.assertEqual([row["id"] for row in metadata["projects"]], ["vllm", "sglang"])
        self.assertTrue(metadata["projects"][0]["workflow"])
        self.assertFalse(metadata["projects"][1]["workflow"])
        rows = self.request("/api/projects-status")[1]["projects"]
        self.assertEqual(rows[1]["sessions"][0]["engine"], "sglang")
        self.assertNotIn("machines", rows[1])
        capabilities = self.request("/api/capabilities?project=sglang")[1]
        self.assertEqual(capabilities["project_id"], "sglang")
        self.assertEqual(capabilities["default_branch"], "adapt-sglang")
        self.assertFalse(capabilities["workflow"])

    def configure_native_task(self):
        thread = "11111111-1111-1111-1111-111111111111"
        now = "2026-01-02T12:00:00+00:00"
        rollout = self.root / "native.jsonl"
        events = [{"type": "session_meta", "payload": {"id": thread}},
                  {"timestamp": now, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "a"}},
                  {"timestamp": now, "type": "event_msg", "payload": {"type": "item_completed", "item": {
                      "type": "AgentMessage", "phase": "commentary", "content": "private native summary"}}}]
        rollout.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        self.monitors["vllm"]["external_tasks"] = [{"id": "native-thead", "backend": "thead", "branch": "adapt-vllm",
            "title": "THead adaptation", "thread_id": thread, "rollout_path": "native.jsonl"}]
        return thread

    def test_native_task_api_is_separate_from_control_session_api(self):
        thread = self.configure_native_task()
        self.load()
        self.serve()
        rows = self.request("/api/adaptation-tasks?project=vllm")[1]["tasks"]
        self.assertEqual(rows[0]["source"], "codex_native")
        self.assertEqual(rows[0]["thread_id"], thread)
        self.assertFalse(rows[0]["controllable"])
        self.assertEqual(self.request("/api/codex-sessions?project=vllm")[1]["sessions"][0]["id"], "1" * 32)
        self.assertEqual(self.request("/api/codex-sessions/native-thead/start?project=vllm", body={})[0], 400)
        self.assertFalse(any(call[0] == "start" for call in self.registry.projects[0].sessions.codex.calls))
        self.assertEqual(self.request("/api/projects-status")[1]["projects"][0]["adaptation_tasks"][0]["source"], "codex_native")
        self.assertTrue(self.request("/api/projects")[1]["projects"][0]["adaptation_tasks"])

    def test_readonly_native_observation_never_constructs_control_manager_or_exposes_private_fields(self):
        thread = self.configure_native_task()
        factory = Mock(side_effect=AssertionError("Must not create sessions"))
        self.load(read_only=True, sessions_factory=factory)
        factory.assert_not_called()
        row = self.registry.projects[0].adaptation_tasks(read_only=True)[0]
        self.assertEqual(row["status"], "running")
        self.assertNotIn(thread, json.dumps(row))
        self.assertNotIn("private", json.dumps(row))
        self.assertIsNone(self.registry.projects[0].sessions)

    def test_readonly_flagscale_store_uses_configured_directory_without_importing_or_reading_settings(self):
        import builtins
        from datetime import datetime, timezone
        identity="4"*32
        store=self.root/"archives"/"flagscale"
        path=store/identity/"session.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id":identity,"provider":"flagscale","status":"waiting_input",
            "backend":"cuda","branch":"adapt-vllm","updated_at":datetime.now(timezone.utc).isoformat(),
            "thread_id":"private-controller","flagscale_session_id":"private-native-id",
            "api_key":"private-dummy-key","worktree":"C:/private/path","events":[{"text":"private output"}]}),encoding="utf-8")
        self.monitors["vllm"]["flagscale_sessions_dir"]="archives/flagscale"
        self.config_path.write_text(json.dumps(self.config),encoding="utf-8")
        for project,config in self.monitors.items():
            (self.root/(project+".json")).write_text(json.dumps(config),encoding="utf-8")
        forbidden=Mock(side_effect=AssertionError("No executor or credential store may be constructed"))
        original_import=builtins.__import__
        def safe_import(name,*args,**kwargs):
            if name.split(".")[0] in {"flagscale_adapter","flagscale_worker","claude_agent","codex_sessions","agent_settings","composite_sessions"}:
                raise AssertionError("Readonly imported an executor or credential module")
            return original_import(name,*args,**kwargs)
        with patch("builtins.__import__",side_effect=safe_import):
            self.registry=ProjectRegistry.from_config(self.config_path,read_only=True,monitor_factory=FakeMonitor,
                sessions_factory=forbidden,claude_sessions_factory=forbidden,flagscale_sessions_factory=forbidden,
                workflow_factory=forbidden,settings_factory=forbidden,flagscale_settings_factory=forbidden)
            rows=self.registry.projects[0].adaptation_tasks(read_only=True)
        forbidden.assert_not_called()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["provider"],"flagscale")
        self.assertFalse(rows[0]["controllable"])
        self.assertNotIn("private",json.dumps(rows))
        self.assertNotIn(identity,json.dumps(rows))
        self.assertFalse((self.root/"runs").exists())
        self.assertIsNone(self.registry.projects[0].flagscale_settings)
        self.assertFalse(self.registry.projects[0].metadata(read_only=True)["flagscale_settings"])

    def test_native_observer_invalid_config_does_not_hide_branch_monitor_or_peer(self):
        self.monitors["vllm"]["external_tasks"] = [{"id": "unsafe"}]
        self.load()
        rows = self.registry.statuses()["projects"]
        self.assertIsNotNone(rows[0]["status"])
        self.assertIsNotNone(rows[1]["status"])
        self.assertIn("Native task observer unavailable", rows[0]["error"])

    def test_acceptance_original_is_plain_utf8_text_with_no_cache_and_strict_query(self):
        self.load()
        self.registry.projects[0].monitor.acceptance_record = Mock(return_value={"content": "Gate passed\n真实原件", "filename": "result.txt"})
        self.serve()
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/acceptance-record?project=vllm&backend=cuda&record=known&branch=adapt-vllm")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/plain; charset=utf-8")
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            body = response.read()
            self.assertEqual(int(response.getheader("Content-Length")), len(body))
            self.assertEqual(body.decode("utf8"), "Gate passed\n真实原件")
        finally:
            connection.close()
        self.registry.projects[0].monitor.acceptance_record.assert_called_once_with("cuda", "known", branch="adapt-vllm")
        for suffix in ("&record=duplicate", "&unexpected=1"):
            self.assertEqual(self.request("/api/acceptance-record?backend=cuda&record=known" + suffix)[0], 400)
        self.registry.projects[0].monitor.acceptance_record = Mock(side_effect=ValueError("Unknown record"))
        self.assertEqual(self.request("/api/acceptance-record?backend=cuda&record=unknown")[0], 400)

    def test_existing_codex_owner_is_proxied_with_separate_local_claude_manager(self):
        self.monitors["vllm"]["sessions_api_url"] = "http://127.0.0.1:8765"
        factory = Mock(side_effect=FakeSessions)
        registry = self.load(sessions_factory=factory)
        self.assertIsInstance(registry.projects[0].sessions.codex, RemoteSessionManager)
        self.assertEqual(registry.projects[0].sessions.claude.root, self.root / "runs/vllm/claude-sessions")
        self.assertEqual(factory.call_count, 1)
        self.assertIs(registry.projects[0].sessions.machine_lock, registry.projects[1].sessions.machine_lock)
        self.assertFalse((self.root / "runs/codex-sessions").exists())

    def test_shared_registry_has_no_sessions_and_sanitizes_every_project(self):
        sessions_factory = Mock(side_effect=AssertionError("Must not create sessions"))
        self.load(read_only=True, sessions_factory=sessions_factory)
        sessions_factory.assert_not_called()
        self.serve(shared=True)
        for path in ("/api/projects", "/fl/api/projects", "/api/projects-status", "/fl/api/projects-status"):
            code, value = self.request(path)
            self.assertEqual(code, 200)
            text = json.dumps(value)
            for forbidden in ("secret", "Users", "private-machine", "source_thread", "sessions_dir", "evidence_dir"):
                self.assertNotIn(forbidden, text)
        rows = self.request("/api/projects-status")[1]["projects"]
        self.assertNotIn("sessions", rows[0])
        self.assertEqual(rows[1]["status"]["engine"], "sglang")
        self.assertEqual(rows[1]["status"]["upstream_version"], "0.5.0")
        self.assertEqual(self.request("/fl/api/branch-status?project=sglang")[1]["engine"], "sglang")
        self.assertEqual(self.request("/fl/api/branch-refresh?project=sglang", body={})[1]["engine"], "sglang")
        self.assertFalse(any(call[0] == "refresh" for call in self.registry.projects[0].monitor.calls))
        self.assertEqual(self.request("/api/branch-status?project=unknown")[0], 400)
        self.assertEqual(self.request("/api/branch-refresh?project=vllm&project=vllm", body={})[0], 400)
        self.assertEqual(self.request("/api/codex-sessions?project=sglang")[0], 404)

    def test_shared_project_initial_failure_is_isolated_and_diagnostics_are_private(self):
        self.monitors["sglang"]["broken"] = True
        self.load(read_only=True)
        self.serve(shared=True)
        code, value = self.request("/api/projects-status")
        self.assertEqual(code, 200)
        self.assertIsNotNone(value["projects"][0]["status"])
        self.assertIsNone(value["projects"][1]["status"])
        self.assertNotIn("private", json.dumps(value))
        self.assertNotIn("192.168", json.dumps(value))
        self.assertEqual(self.request("/api/branch-status?project=sglang")[0], 503)
        self.assertEqual(self.request("/api/branch-status?project=vllm")[0], 200)


class RemoteSessionProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session_id = "1" * 32
        self.calls = []
        calls = self.calls
        session_id = self.session_id
        class Owner:
            ACTIVE = RemoteSessionManager.ACTIVE
            def __init__(self):
                self.closed = False
                self.rows = [{"id": session_id, "status": "prepared", "backend": "cuda",
                              "branch": "adapt-vllm", "ssh_alias": "gpu-a", "ssh_connection": None}]
            def snapshot(self, compact=False):
                calls.append(("snapshot", compact))
                return {"sessions": self.rows, "machine_profiles": [{"id": "gpu", "ssh_alias": "gpu-b"}]}
            def health(self):
                return {"available": True}
            def event_history(self, ident, **kwargs):
                return {"session_id": ident, **kwargs}
            def create(self, body):
                calls.append(("create", body))
                return {"id": "2" * 32, "status": "prepared"}
            def start(self, ident):
                calls.append(("start", ident))
                return {"id": ident, "status": "running"}
            def message(self, ident, text):
                calls.append(("message", ident, text))
                return {"id": ident}
            def interrupt(self, ident):
                calls.append(("interrupt", ident))
                return {"id": ident, "status": "interrupting"}
            def respond(self, ident, request_id, response):
                calls.append(("respond", ident, request_id, response))
                return {"id": ident}
            def save_profile(self, body):
                calls.append(("profile", body))
                return body
            def check_machine(self, body):
                calls.append(("check", body))
                return {"ok": True}
        self.owner = Owner()
        self.server = flflow.make_server(None, "127.0.0.1", 0, sessions=self.owner)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.config = {"sessions_api_url": f"http://127.0.0.1:{self.server.server_port}",
                       "sessions_dir": str(self.root / "untouched-sessions"),
                       "evidence_dir": str(self.root / "untouched-evidence"), "branch": "adapt-vllm"}
        self.proxy = RemoteSessionManager(self.config, self.root)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)
        self.temp.cleanup()

    def test_proxy_preserves_existing_owner_and_forwards_all_session_routes(self):
        self.assertEqual(self.proxy.snapshot(compact=True)["sessions"][0]["id"], self.session_id)
        self.assertTrue(self.proxy.health()["available"])
        history = self.proxy.event_history(self.session_id, cursor="abc+/=", limit=1)
        self.assertEqual(history["cursor"], "abc+/=")
        self.assertEqual(history["limit"], 1)
        self.assertEqual(self.proxy.save_profile({"id": "gpu"})["id"], "gpu")
        self.assertTrue(self.proxy.check_machine({"profile_id": "gpu"})["ok"])
        self.proxy.create({"profile_id": "gpu", "backend": "cuda", "start": True})
        create = next(call for call in self.calls if call[0] == "create")
        self.assertIs(create[1]["start"], False)
        self.assertFalse(any(call[0] == "start" for call in self.calls))
        self.proxy.start(self.session_id)
        self.proxy.message(self.session_id, "continue")
        self.proxy.respond(self.session_id, 7, {"decision": "decline"})
        self.proxy.interrupt(self.session_id)
        self.proxy.close()
        self.assertFalse(self.owner.closed)
        self.assertFalse(Path(self.config["sessions_dir"]).exists())
        self.assertFalse(Path(self.config["evidence_dir"]).exists())
        self.assertEqual(self.proxy.snapshot()["sessions"][0]["id"], self.session_id)

    def test_proxy_rejects_non_loopback_credentials_paths_and_invalid_session_ids(self):
        for value in ("https://127.0.0.1:8765", "http://example.com:8765", "http://127.0.0.2:8765",
                      "http://user:pass@127.0.0.1:8765", "http://127.0.0.1:8765/api", "http://127.0.0.1:8765?x=1",
                      "http://127.0.0.1:8765/#bad", "http://127.0.0.1", ""):
            with self.subTest(value=value), self.assertRaises(ProjectError):
                RemoteSessionManager({**self.config, "sessions_api_url": value}, self.root)
        with self.assertRaises(SessionError):
            self.proxy.interrupt("../private")
        self.assertFalse(self.calls)

    def test_proxy_machine_allocation_checks_live_peers_and_propagates_failure(self):
        peer = Mock()
        peer.allocation_sessions.return_value = [{"id": "3" * 32, "status": "waiting_approval", "ssh_alias": "gpu-b"}]
        self.proxy.peer_managers = [self.proxy, peer]
        with self.assertRaisesRegex(SessionError, "active or prepared"):
            self.proxy.create({"profile_id": "gpu", "backend": "cuda"})
        self.assertFalse(any(call[0] == "create" for call in self.calls))
        peer.allocation_sessions.side_effect = SessionError("Owner unavailable")
        with self.assertRaisesRegex(SessionError, "unavailable"):
            self.proxy.start(self.session_id)
        self.assertFalse(any(call[0] == "start" for call in self.calls))
        self.assertEqual(self.owner.rows[0]["status"], "prepared")

    def test_proxy_monitor_policy_blocks_new_work_and_acceptance_but_allows_decline(self):
        self.config["backend_task_policies"] = {"cuda": {"mode": "monitor", "branch": "adapt-vllm"}}
        with self.assertRaisesRegex(SessionError, "monitor-only"):
            self.proxy.start(self.session_id)
        with self.assertRaisesRegex(SessionError, "monitor-only"):
            self.proxy.message(self.session_id, "continue")
        with self.assertRaisesRegex(SessionError, "Monitor-only"):
            self.proxy.respond(self.session_id, 7, {"decision": "accept"})
        self.proxy.respond(self.session_id, 7, {"decision": "decline"})
        self.assertEqual(self.calls[-1][0], "respond")


if __name__ == "__main__":
    unittest.main()
