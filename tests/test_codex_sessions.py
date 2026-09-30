import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

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
            self.callback({"method": "turn/started", "params": {"threadId": "thread-123", "turn": {"id": "turn-1"}}})
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        if method == "turn/interrupt":
            self.callback({"method": "turn/completed", "params": {"threadId": "thread-123", "turn": {"id": "turn-1", "status": "interrupted"}}})
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

    def test_imported_machine_profiles_can_be_incomplete_but_cannot_start(self):
        profile = self.manager.save_profile({"id": "ssh-import", "ssh_alias": "gpu_unknown",
            "backend": "", "remote_workspace": "", "hardware": "Unverified",
            "source": "SSH config", "backend_source": "unknown", "notes": "Needs classification"})
        self.assertEqual(profile["backend"], "")
        with self.assertRaises(SessionError):
            self.manager.create({"profile_id": profile["id"]})
        profile = self.manager.save_profile({"id": profile["id"], "backend": "thead", "backend_source": "manual"})
        self.assertEqual(profile["source"], "SSH config")
        self.assertEqual(profile["hardware"], "Unverified")
        with self.assertRaises(SessionError):
            self.manager.create({"profile_id": profile["id"]})
        self.manager.save_profile({"id": profile["id"], "remote_workspace": "/work/plugin"})
        session = self.manager.create({"profile_id": profile["id"]})
        self.assertEqual(session["backend"], "thead")
        self.assertEqual(session["status"], "prepared")

    def test_gateway_profile_is_inventory_only(self):
        profile = self.manager.save_profile({"ssh_alias": "jump_host", "role": "gateway"})
        with self.assertRaisesRegex(SessionError, "gateway"):
            self.manager.create({"profile_id": profile["id"], "backend": "cuda", "remote_workspace": "/work/plugin"})

    def test_development_machine_stays_inventory_only_even_with_task_overrides(self):
        profile = self.manager.save_profile({"id": "dashboard-host", "ssh_alias": "dev_host",
            "role": "development", "backend": "cuda", "label": "Development / dashboard deployment"})
        self.assertEqual(profile["backend"], "")
        self.assertEqual(self.manager.snapshot()["machine_profiles"][0]["role"], "development")
        for overrides in ({"profile_id": profile["id"]}, {"ssh_alias": profile["ssh_alias"]}):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(SessionError, "development"):
                self.manager.create({**self.payload, **overrides, "role": "compute"})
        self.assertEqual(self.manager.snapshot()["sessions"], [])

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
        client.callback({"id": 10, "method": "item/permissions/requestApproval", "params": {"threadId": "thread-123", "permissions": {"network": {"enabled": True}}}})
        with self.assertRaises(SessionError):
            self.manager.respond(session["id"], 10, {"permissions": {"fileSystem": {"write": ["/"]}}})
        self.manager.respond(session["id"], 10, {"permissions": {"network": {"enabled": True}}, "scope": "session"})
        self.assertEqual(client.responses[-1][1]["scope"], "turn")

    def test_user_answers_are_bound_to_pending_question_ids(self):
        session = self.create()
        client = self.start(session)
        client.callback({"id": "q", "method": "item/tool/requestUserInput", "params": {"threadId": "thread-123", "questions": [{"id": "machine", "question": "Which GPU?"}]}})
        with self.assertRaises(SessionError):
            self.manager.respond(session["id"], "q", {"answers": {"other": {"answers": ["a"]}}})
        self.manager.respond(session["id"], "q", {"answers": {"machine": {"answers": ["A100"]}}})
        self.assertEqual(client.responses[-1][0], "q")

    def test_events_and_completed_status_survive_dashboard_restart(self):
        session = self.create()
        client = self.start(session)
        client.callback({"method": "item/agentMessage/delta", "params": {"threadId": "thread-123", "delta": "Gate results received"}})
        client.callback({"method": "turn/completed", "params": {"threadId": "thread-123", "turn": {"status": "completed"}}})
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

    def test_child_final_does_not_complete_parent_or_clear_parent_approval(self):
        session = self.create()
        client = self.start(session)
        parent_plan = [{"step": "版本适配", "status": "in_progress"}]
        client.callback({"method": "turn/plan/updated", "params": {"threadId": "thread-123", "plan": parent_plan}})
        client.callback({"method": "item/completed", "params": {"threadId": "thread-123", "item": {"type": "agentMessage", "text": "继续执行父任务适配。"}}})
        client.callback({"id": 1, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-123", "command": "ssh validation_host read-only"}})
        for method, params in (
            ("turn/started", {"turn": {"id": "child-turn"}}),
            ("turn/plan/updated", {"plan": [{"step": "只读审查", "status": "completed"}]}),
            ("item/completed", {"item": {"type": "agentMessage", "text": "子任务审查结束。"}}),
            ("thread/status/changed", {"status": {"type": "idle"}}),
            ("turn/completed", {"turn": {"id": "child-turn", "status": "completed"}}),
        ):
            client.callback({"method": method, "params": {"threadId": "child-thread", **params}})
        state = self.manager._copy(session["id"])
        self.assertEqual(state["status"], "waiting_approval")
        self.assertEqual(state["turn_id"], "turn-1")
        self.assertEqual(state["plan"], parent_plan)
        self.assertEqual(state["last_agent_message"], "继续执行父任务适配。")
        self.assertEqual([r["id"] for r in state["pending_requests"]], [1])
        self.assertEqual(client.responses, [])
        self.assertTrue(any(event["source_scope"] == "child" for event in state["events"]))

    def test_child_approval_stays_visible_until_human_response_even_after_parent_completion(self):
        session = self.create()
        client = self.start(session)
        client.callback({"id": 2, "method": "item/commandExecution/requestApproval", "params": {"threadId": "child-thread", "command": "ssh validation_host read-only"}})
        state = self.manager._copy(session["id"])
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["pending_requests"][0]["source_thread_id"], "child-thread")
        self.assertEqual(state["pending_requests"][0]["source_scope"], "child")
        self.assertEqual(client.responses, [])
        client.callback({"method": "turn/completed", "params": {"threadId": "thread-123", "turn": {"id": "turn-1", "status": "completed"}}})
        self.assertEqual(self.manager._copy(session["id"])["pending_requests"][0]["id"], 2)
        self.manager.respond(session["id"], 2, {"decision": "accept"})
        state = self.manager._copy(session["id"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["pending_requests"], [])
        self.assertEqual(client.responses, [(2, {"decision": "accept"})])

    def test_child_request_resolved_does_not_clear_other_parent_requests(self):
        session = self.create()
        client = self.start(session)
        for request_id, thread in ((1, "thread-123"), (2, "child-thread")):
            client.callback({"id": request_id, "method": "item/commandExecution/requestApproval", "params": {"threadId": thread, "command": "read-only"}})
        client.callback({"method": "serverRequest/resolved", "params": {"threadId": "child-thread", "requestId": 2}})
        state = self.manager._copy(session["id"])
        self.assertEqual([r["id"] for r in state["pending_requests"]], [1])
        self.assertEqual(state["status"], "waiting_approval")
        self.assertEqual(client.responses, [])

    def test_unscoped_final_is_ignored_and_nested_root_scope_is_supported(self):
        session = self.create()
        client = self.start(session)
        client.callback({"method": "turn/completed", "params": {"turn": {"id": "unknown-turn", "status": "completed"}}})
        client.callback({"method": "item/completed", "params": {"item": {"threadId": "thread-123", "type": "agentMessage", "text": "父任务仍在执行。"}}})
        state = self.manager._copy(session["id"])
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["turn_id"], "turn-1")
        self.assertEqual(state["last_agent_message"], "父任务仍在执行。")

    def test_monitor_policy_survives_reload_and_blocks_all_execution_entry_points(self):
        session = self.create()
        client = self.start(session)
        client.callback({"method": "turn/plan/updated", "params": {"threadId": "thread-123", "plan": [
            {"step": "Version adaptation", "status": "in_progress"},
            {"step": "Gate", "status": "pending"}]}})
        client.callback({"method": "turn/completed", "params": {"threadId": "thread-123", "turn": {"status": "completed"}}})
        self.manager.close()
        self.config.update(github_repo="example/plugin", backend_task_policies={
            "cuda": {"mode": "monitor", "pr": 17, "reason": "Existing adaptation owner"}})
        self.manager = CodexSessionManager(self.config, self.base, FakeClient)
        before = len(FakeClient.instances)
        expected = {"mode": "monitor", "branch": self.payload["branch"], "pr": 17,
                    "pr_url": "https://github.com/example/plugin/pull/17", "reason": "Existing adaptation owner"}
        state = self.manager.snapshot()
        self.assertEqual(state["backend_task_policies"]["cuda"], expected)
        self.assertEqual(state["sessions"][0]["task_policy"], expected)
        self.assertEqual(state["sessions"][0]["status"], "completed")
        self.assertEqual(state["sessions"][0]["plan"][-1]["status"], "pending")
        with patch.object(self.manager, "_check_connection") as probe, patch.object(self.manager, "_git") as git:
            for operation in (lambda: self.manager.start(session["id"]),
                              lambda: self.manager.message(session["id"], "Continue"),
                              lambda: self.manager._launch(session["id"], "Continue")):
                with self.assertRaisesRegex(SessionError, "monitor-only"):
                    operation()
            with self.assertRaisesRegex(SessionError, session["id"]):
                self.manager.create({**self.payload, "ssh_alias": "another_gpu", "require_preflight": True})
            probe.assert_not_called()
            git.assert_not_called()
        self.assertEqual(len(FakeClient.instances), before)
        self.assertEqual(self.manager._copy(session["id"])["status"], "completed")

    def test_monitor_create_is_rejected_before_probe_or_worktree_creation(self):
        self.manager.close()
        self.config["backend_task_policies"] = {"cuda": {"mode": "monitor"}}
        self.manager = CodexSessionManager(self.config, self.base, FakeClient)
        with patch.object(self.manager, "_check_connection") as probe, patch.object(self.manager, "_git") as git:
            with self.assertRaisesRegex(SessionError, "monitor-only"):
                self.manager.create({**self.payload, "require_preflight": True})
            probe.assert_not_called()
            git.assert_not_called()
        self.assertEqual(self.manager.snapshot()["sessions"], [])
        self.assertFalse((self.manager.root / "worktrees").exists())
        self.assertEqual(FakeClient.instances, [])

    def test_monitor_active_task_can_only_deny_requests_or_interrupt(self):
        session = self.create()
        client = self.start(session)
        self.manager.backend_task_policies = self.manager._task_policies({"cuda": {"mode": "monitor"}})
        client.callback({"id": 1, "method": "item/commandExecution/requestApproval",
                         "params": {"threadId": "thread-123", "availableDecisions": ["accept", "decline", "cancel"]}})
        client.callback({"id": 2, "method": "item/fileChange/requestApproval", "params": {"threadId": "thread-123"}})
        client.callback({"id": 3, "method": "item/permissions/requestApproval",
                         "params": {"threadId": "thread-123", "permissions": {"network": {"enabled": True}}}})
        client.callback({"id": 4, "method": "item/tool/requestUserInput",
                         "params": {"threadId": "thread-123", "questions": [{"id": "q"}]}})
        for rid, response in ((1, {"decision": "accept"}), (2, {"decision": "accept"}),
                              (3, {"permissions": {"network": {"enabled": True}}}),
                              (4, {"answers": {"q": {"answers": ["Continue"]}}})):
            with self.subTest(request_id=rid), self.assertRaisesRegex(SessionError, "Monitor-only"):
                self.manager.respond(session["id"], rid, response)
        self.assertEqual(client.responses, [])
        with self.assertRaisesRegex(SessionError, "monitor-only"):
            self.manager.message(session["id"], "Continue")
        self.manager.respond(session["id"], 1, {"decision": "decline"})
        self.manager.respond(session["id"], 2, {"decision": "cancel"})
        self.manager.respond(session["id"], 3, {"permissions": {}})
        self.assertEqual(client.responses, [(1, {"decision": "decline"}), (2, {"decision": "cancel"}),
                                            (3, {"permissions": {}, "scope": "turn"})])
        result = self.manager.interrupt(session["id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["task_policy"]["mode"], "monitor")
        self.assertEqual(client.requests[-1][0], "turn/interrupt")

    def test_task_policy_applies_only_to_its_baseline_branch(self):
        self.git("branch", "another-baseline")
        self.manager.close()
        self.config["backend_task_policies"] = {"cuda": {"mode": "monitor", "branch": "another-baseline"}}
        self.manager = CodexSessionManager(self.config, self.base, FakeClient)
        session = self.create()
        self.assertEqual(session["task_policy"]["mode"], "adapt")
        self.start(session)
        self.manager.interrupt(session["id"])
        with self.assertRaisesRegex(SessionError, "monitor-only"):
            self.manager.create({**self.payload, "branch": "another-baseline"})
        other = self.manager.create({**self.payload, "backend": "metax", "branch": "another-baseline"})
        self.assertEqual(other["task_policy"]["mode"], "adapt")
        self.assertIsNone(other["task_policy"]["pr_url"])

    def test_backend_ownership_blocks_replacement_machine_and_new_head_after_ended_turn(self):
        session = self.create()
        client = self.start(session)
        client.callback({"method": "turn/completed", "params": {"threadId": "thread-123", "turn": {"status": "completed"}}})
        (self.repo / "README.md").write_text("new baseline commit\n")
        self.git("add", "README.md")
        self.git("commit", "-m", "Advance baseline")
        self.manager.close()
        self.manager = CodexSessionManager(self.config, self.base, FakeClient)
        for status in ("completed", "interrupted", "failed", "disconnected"):
            self.manager._sessions[session["id"]]["status"] = status
            with self.subTest(status=status), self.assertRaisesRegex(SessionError, session["id"]):
                self.manager.create({**self.payload, "ssh_alias": "replacement_gpu", "remote_workspace": "/other/plugin"})
        self.assertEqual(len(self.manager.snapshot()["sessions"]), 1)
        resumed = self.start(session)
        self.assertEqual(resumed.requests[0][0], "thread/resume")
        self.assertEqual(resumed.requests[0][1]["threadId"], session["thread_id"] or "thread-123")
        self.manager.interrupt(session["id"])
        self.git("branch", "next-baseline")
        next_session = self.manager.create({**self.payload, "branch": "next-baseline"})
        self.assertNotEqual(next_session["work_branch"], session["work_branch"])
        self.assertNotEqual(next_session["worktree"], session["worktree"])

    def test_invalid_policy_cannot_silently_enable_adaptation_or_build_unsafe_pr_url(self):
        for policies in ([], {"cuda": {"mode": "typo"}}, {"cuda": {"mode": []}},
                         {"cuda": {"mode": "monitor", "pr": True}}, {"cuda": {"mode": "monitor", "pr": -1}},
                         {"cuda": {"mode": "monitor", "branch": "bad branch"}}, {"unknown": {"mode": "monitor"}}):
            with self.subTest(policies=policies), self.assertRaises(SessionError):
                CodexSessionManager({**self.config, "backend_task_policies": policies}, self.base, FakeClient)
        for repo in ("https://github.com/example/plugin", "example/../plugin", "../plugin", "example/plugin?token=bad"):
            with self.subTest(repo=repo), self.assertRaises(SessionError):
                CodexSessionManager({**self.config, "github_repo": repo,
                                     "backend_task_policies": {"cuda": {"mode": "monitor", "pr": 17}}}, self.base, FakeClient)


if __name__ == "__main__":
    unittest.main()
