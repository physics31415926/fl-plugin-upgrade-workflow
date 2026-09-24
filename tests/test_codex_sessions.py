import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from codex_sessions import CodexSessionManager, SessionError


class FakeProcess:
    def __init__(self):
        self.closed = False

    def poll(self):
        return 0 if self.closed else None


class FakeClient:
    instances = []

    def __init__(self, executable, cwd, callback, timeout=20):
        self.callback = callback
        self.process = FakeProcess()
        self.requests = []
        self.responses = []
        self.info = {"userAgent": "test-codex"}
        self.__class__.instances.append(self)

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "account/read":
            return {"account": {"type": "chatgpt", "email": "not-exposed@example.test"}}
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": "thread-123"}, "model": "user-default"}
        if method == "turn/start":
            self.callback({"method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        if method == "turn/interrupt":
            self.callback({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "interrupted"}}})
        return {}

    def respond(self, request_id, result):
        self.responses.append((request_id, result))

    def close(self):
        self.process.closed = True


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "plugin"
        self.repo.mkdir()
        self.git("init", "-b", "0.4.0-dev")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.test")
        (self.repo / "README.md").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.sha = self.git("rev-parse", "HEAD")
        self.config = {"repo": str(self.repo), "branch": "0.4.0-dev", "codex_executable": "fake-codex"}
        self.manager = CodexSessionManager(self.config, self.base, FakeClient)
        self.payload = {"backend": "cuda", "branch": "0.4.0-dev", "ssh_alias": "gpu_a100", "remote_workspace": "/work/plugin", "model_paths": {"Qwen3.6-27B": "/models/Qwen"}}
        FakeClient.instances.clear()

    def tearDown(self):
        self.manager.close()
        # Git worktree metadata must be cleaned by Git before TemporaryDirectory.
        for worktree in (self.base / "runs/codex-sessions/worktrees").glob("*"):
            self.git("worktree", "remove", "--force", str(worktree))
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.STDOUT, text=True).strip()

    def create(self):
        return self.manager.create(self.payload)

    def start(self, session):
        self.manager.start(session["id"])
        self.manager._workers[session["id"]].join(timeout=5)
        self.assertFalse(self.manager._workers[session["id"]].is_alive())
        return FakeClient.instances[-1]

    def test_preparation_pins_branch_commit_and_preserves_source_checkout(self):
        (self.repo / "README.md").write_text("user change\n")
        session = self.create()
        self.assertEqual(session["commit"], self.sha)
        self.assertEqual(session["status"], "prepared")
        self.assertEqual((Path(session["worktree"]) / "README.md").read_text(), "base\n")
        self.assertEqual((self.repo / "README.md").read_text(), "user change\n")
        self.assertEqual(FakeClient.instances, [])
        with self.assertRaises(SessionError):
            self.create()

    def test_machine_exclusivity_and_cancel_prepared(self):
        session = self.create()
        with self.assertRaises(SessionError):
            self.manager.create({**self.payload, "backend": "metax"})
        self.manager.interrupt(session["id"])
        other = self.manager.create({**self.payload, "backend": "metax"})
        with self.assertRaises(SessionError):
            self.manager.start(session["id"])
        self.assertEqual(other["status"], "prepared")

    def test_cancel_during_client_start_keeps_machine_leased_until_acknowledged(self):
        entered, release = threading.Event(), threading.Event()

        class DelayedClient(FakeClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                entered.set()
                release.wait(5)

        self.manager.client_factory = DelayedClient
        session = self.create()
        self.manager.start(session["id"])
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.manager.interrupt(session["id"])["status"], "interrupting")
            with self.assertRaises(SessionError):
                self.manager.create({**self.payload, "backend": "metax"})
        finally:
            release.set()
            self.manager._workers[session["id"]].join(5)
        self.assertEqual(self.manager._copy(session["id"])["status"], "interrupted")
        self.assertFalse(any(method == "turn/start" for method, _ in FakeClient.instances[-1].requests))
        self.assertEqual(self.manager.create({**self.payload, "backend": "metax"})["status"], "prepared")

    def test_cancel_during_turn_start_keeps_machine_leased_until_turn_completion(self):
        entered, release = threading.Event(), threading.Event()

        class DelayedTurnClient(FakeClient):
            def request(self, method, params):
                if method == "turn/start":
                    entered.set()
                    release.wait(5)
                return super().request(method, params)

        self.manager.client_factory = DelayedTurnClient
        session = self.create()
        self.manager.start(session["id"])
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.manager.interrupt(session["id"])["status"], "interrupting")
            with self.assertRaises(SessionError):
                self.manager.create({**self.payload, "backend": "metax"})
        finally:
            release.set()
            self.manager._workers[session["id"]].join(5)
        state = self.manager._copy(session["id"])
        self.assertEqual(state["status"], "interrupted")
        self.assertIsNone(state["turn_id"])
        methods = [method for method, _ in FakeClient.instances[-1].requests]
        self.assertEqual(methods[-2:], ["turn/start", "turn/interrupt"])
        self.assertEqual(self.manager.create({**self.payload, "backend": "metax"})["status"], "prepared")

    def test_machine_profiles_reject_shell_option_alias_and_relative_workspace(self):
        for change in [{"ssh_alias": "-oProxyCommand=bad"}, {"ssh_alias": "host;whoami"}, {"remote_workspace": "../plugin"}, {"model_paths": "not-a-map"}, {"branch": "--help"}]:
            with self.assertRaises(SessionError):
                self.manager.create({**self.payload, **change})
        profile = self.manager.save_profile({**self.payload, "label": "A100"})
        other = CodexSessionManager(self.config, self.base, FakeClient)
        self.assertEqual(other.snapshot()["machine_profiles"][0]["id"], profile["id"])
        other.close()

    def test_uses_default_model_manual_approvals_and_resumes_persisted_thread(self):
        session = self.create()
        client = self.start(session)
        method, params = client.requests[0]
        self.assertEqual(method, "thread/start")
        self.assertNotIn("model", params)
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["approvalsReviewer"], "user")
        self.assertEqual(params["sandbox"], "workspace-write")
        self.manager.interrupt(session["id"])
        self.assertEqual(self.manager._copy(session["id"])["status"], "interrupted")
        self.manager.close()
        self.manager = CodexSessionManager(self.config, self.base, FakeClient)
        resumed = self.start(session)
        self.assertEqual(resumed.requests[0][0], "thread/resume")
        self.assertEqual(resumed.requests[0][1]["threadId"], "thread-123")

    def test_approvals_are_pending_until_explicit_response_and_cannot_persist_rules(self):
        session = self.create()
        client = self.start(session)
        client.callback({"id": 9, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-123", "command": "ssh gpu_a100 nvidia-smi"}})
        self.assertEqual(client.responses, [])
        self.assertEqual(self.manager._copy(session["id"])["status"], "waiting_approval")
        with self.assertRaises(SessionError):
            self.manager.respond(session["id"], 9, {"decision": "acceptForSession"})
        with self.assertRaises(SessionError):
            self.manager.respond(session["id"], "wrong-id", {"decision": "accept"})
        with self.assertRaises(SessionError):
            self.manager.respond(session["id"], 9, {"decision": {"acceptWithExecpolicyAmendment": ["ssh"]}})
        self.manager.respond(session["id"], "9", {"decision": "accept"})
        self.assertEqual(client.responses, [(9, {"decision": "accept"})])
        with self.assertRaises(SessionError):
            self.manager.respond(session["id"], 9, {"decision": "accept"})

    def test_permission_scope_is_limited_to_displayed_request(self):
        session = self.create()
        client = self.start(session)
        client.callback({"id": 10, "method": "item/permissions/requestApproval", "params": {"permissions": {"network": {"enabled": True}}}})
        with self.assertRaises(SessionError):
            self.manager.respond(session["id"], 10, {"permissions": {"fileSystem": {"write": ["/"]}}})
        self.manager.respond(session["id"], 10, {"permissions": {"network": {"enabled": True}}, "scope": "session"})
        self.assertEqual(client.responses[-1][1]["scope"], "turn")

    def test_user_answers_are_bound_to_pending_question_ids(self):
        session = self.create()
        client = self.start(session)
        client.callback({"id": "q", "method": "item/tool/requestUserInput", "params": {"questions": [{"id": "machine", "question": "Which GPU?"}]}})
        with self.assertRaises(SessionError):
            self.manager.respond(session["id"], "q", {"answers": {"other": {"answers": ["a"]}}})
        self.manager.respond(session["id"], "q", {"answers": {"machine": {"answers": ["A100"]}}})
        self.assertEqual(client.responses[-1][0], "q")

    def test_events_and_completed_status_survive_dashboard_restart(self):
        session = self.create()
        client = self.start(session)
        client.callback({"method": "item/agentMessage/delta", "params": {"delta": "Gate results received"}})
        client.callback({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
        manager = CodexSessionManager(self.config, self.base, FakeClient)
        state = manager.snapshot()["sessions"][0]
        self.assertEqual(state["status"], "completed")
        self.assertIsNone(state["turn_id"])
        self.assertTrue(any("Gate results" in json.dumps(e) for e in state["events"]))
        manager.close()

    def test_running_session_reloads_as_disconnected_without_starting_model(self):
        session = self.create()
        self.start(session)
        before = len(FakeClient.instances)
        manager = CodexSessionManager(self.config, self.base, FakeClient)
        self.assertEqual(manager.snapshot()["sessions"][0]["status"], "disconnected")
        self.assertEqual(manager.snapshot()["sessions"][0]["pending_requests"], [])
        self.assertEqual(len(FakeClient.instances), before)
        manager.close()

    def test_health_does_not_create_threads_or_expose_account_email(self):
        health = self.manager.health()
        self.assertTrue(health["authenticated"])
        self.assertEqual(FakeClient.instances[0].requests, [("account/read", {"refreshToken": False})])
        self.assertNotIn("email", json.dumps(health))
        self.assertTrue(FakeClient.instances[0].process.closed)


if __name__ == "__main__":
    unittest.main()
