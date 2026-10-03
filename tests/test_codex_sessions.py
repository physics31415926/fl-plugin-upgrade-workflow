import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_sessions import CodexSessionManager, SessionError, atomic_json


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

    def test_compact_snapshot_omits_events_and_prompt_without_changing_session(self):
        session = self.create()
        stored = self.manager._sessions[session["id"]]
        stored["events"] = [{"method": "item/outputDelta", "params": {"delta": "x" * 4000}}
                            for _ in range(80)]
        stored["prompt"] = "initial task instruction"
        stored["progress_summary"] = "CPU adaptation in progress"
        stored["last_agent_message"] = "Latest agent update"
        stored["plan"] = [{"step": "Run acceptance", "status": "pending"}]
        stored["pending_requests"] = [{"id": 42, "method": "item/commandExecution/requestApproval",
                                       "params": {"command": "read-only action"}}]
        stored["reported_provenance"] = {"candidate_commit": "a" * 40}

        full = self.manager.snapshot()["sessions"][0]
        cards = self.manager.snapshot(compact=True)["sessions"][0]
        self.assertEqual(len(full["events"]), 80)
        self.assertEqual(full["prompt"], "initial task instruction")
        self.assertNotIn("events", cards)
        self.assertNotIn("prompt", cards)
        self.assertEqual(cards, {key: value for key, value in full.items()
                                 if key not in ("events", "prompt")})
        for key in ("status", "error", "progress_summary", "last_agent_message", "plan",
                    "pending_requests", "reported_provenance", "task_policy", "thread_id"):
            self.assertEqual(cards[key], full[key])
        self.assertEqual(len(stored["events"]), 80)
        self.assertEqual(stored["prompt"], "initial task instruction")

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

    def test_root_approval_cancel_waits_for_matching_completion_before_same_thread_revision(self):
        session = self.create()
        client = self.start(session)
        original = {key: self.manager._copy(session["id"])[key] for key in
                    ("thread_id", "branch", "work_branch", "worktree", "remote_workspace")}
        client.callback({"id": 9, "method": "item/commandExecution/requestApproval",
                         "params": {"threadId": "thread-123", "availableDecisions": ["accept", "cancel"]}})
        state = self.manager.respond(session["id"], 9, {"decision": "cancel"})
        self.assertEqual(state["status"], "interrupting")
        self.assertTrue(state["approval_cancel_pending"])
        self.assertEqual(state["approval_cancel_turn_id"], "turn-1")
        self.assertFalse(state["cancel_requested"])
        self.assertFalse(any(method == "turn/interrupt" for method, _ in client.requests))
        saved = json.loads((self.manager.root / session["id"] / "session.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["approval_cancel_pending"])
        with self.assertRaisesRegex(SessionError, "本轮正在结束"):
            self.manager.message(session["id"], "修订原命令后继续")
        with self.assertRaisesRegex(SessionError, "active turn"):
            self.manager.start(session["id"])
        with self.assertRaises(SessionError):
            self.manager.create({**self.payload, "backend": "metax"})
        for method, params in (
            ("serverRequest/resolved", {"threadId": "thread-123", "requestId": 9}),
            ("turn/completed", {"threadId": "child-thread", "turn": {"id": "child-turn", "status": "completed"}}),
            ("turn/completed", {"threadId": "thread-123", "turn": {"id": "older-root-turn", "status": "completed"}}),
            ("turn/completed", {"threadId": "thread-123", "turn": {"status": "completed"}}),
        ):
            client.callback({"method": method, "params": params})
            self.assertEqual(self.manager._copy(session["id"])["status"], "interrupting")
        self.assertFalse(any(method == "turn/steer" for method, _ in client.requests))
        client.callback({"method": "turn/completed", "params": {"threadId": "thread-123",
                         "turn": {"id": "turn-1", "status": "interrupted"}}})
        ended = self.manager._copy(session["id"])
        self.assertFalse(ended["approval_cancel_pending"])
        self.assertIsNone(ended["turn_id"])
        self.assertNotIn("approval_cancel_turn_id", ended)
        self.manager.message(session["id"], "修订原命令后继续")
        self.manager._workers[session["id"]].join(5)
        resumed = self.manager._copy(session["id"])
        self.assertEqual({key: resumed[key] for key in original}, original)
        self.assertEqual(resumed["status"], "running")
        starts = [params for method, params in client.requests if method == "turn/start"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[-1], {"threadId": "thread-123",
                                     "input": [{"type": "text", "text": "修订原命令后继续"}]})
        self.assertEqual(client.responses, [(9, {"decision": "cancel"})])

    def test_child_cancel_does_not_end_parent_or_clear_parent_approval(self):
        session = self.create()
        client = self.start(session)
        for request_id, thread_id in ((1, "thread-123"), (2, "child-thread")):
            client.callback({"id": request_id, "method": "item/commandExecution/requestApproval",
                             "params": {"threadId": thread_id}})
        self.manager.respond(session["id"], 2, {"decision": "cancel"})
        client.callback({"method": "turn/completed", "params": {"threadId": "child-thread",
                         "turn": {"id": "child-turn", "status": "interrupted"}}})
        state = self.manager._copy(session["id"])
        self.assertEqual(state["status"], "waiting_approval")
        self.assertEqual(state["turn_id"], "turn-1")
        self.assertFalse(state.get("approval_cancel_pending"))
        self.assertEqual([request["id"] for request in state["pending_requests"]], [1])
        self.manager.message(session["id"], "给原任务补充只读要求")
        self.assertEqual(client.requests[-1][0], "turn/steer")
        self.assertEqual(client.responses, [(2, {"decision": "cancel"})])

    def test_immediate_root_completion_during_cancel_is_not_overwritten(self):
        session = self.create()
        client = self.start(session)
        client.callback({"id": 9, "method": "item/fileChange/requestApproval",
                         "params": {"threadId": "thread-123"}})

        def immediate_completion(request_id, result):
            client.responses.append((request_id, result))
            client.callback({"method": "turn/completed", "params": {"threadId": "thread-123",
                             "turn": {"id": "turn-1", "status": "interrupted"}}})

        with patch.object(client, "respond", side_effect=immediate_completion):
            state = self.manager.respond(session["id"], 9, {"decision": "cancel"})
        self.assertEqual(state["status"], "interrupted")
        self.assertFalse(state["approval_cancel_pending"])
        self.assertIsNone(state["turn_id"])
        self.assertEqual(state["pending_requests"], [])

    def test_failed_cancel_response_preserves_pending_approval_and_decline_can_continue(self):
        session = self.create()
        client = self.start(session)
        client.callback({"id": 9, "method": "item/commandExecution/requestApproval",
                         "params": {"threadId": "thread-123"}})
        with patch.object(client, "respond", side_effect=SessionError("Response was not sent")):
            with self.assertRaisesRegex(SessionError, "not sent"):
                self.manager.respond(session["id"], 9, {"decision": "cancel"})
        state = self.manager._copy(session["id"])
        self.assertEqual(state["status"], "waiting_approval")
        self.assertFalse(state.get("approval_cancel_pending"))
        self.assertEqual([request["id"] for request in state["pending_requests"]], [9])
        self.assertEqual(client.responses, [])
        self.manager.respond(session["id"], 9, {"decision": "decline"})
        self.assertEqual(self.manager._copy(session["id"])["status"], "running")
        self.manager.message(session["id"], "修改后再申请本次操作")
        self.assertEqual(client.requests[-1][0], "turn/steer")

    def test_cancel_wait_disconnect_requires_explicit_resume_and_keeps_stored_thread(self):
        session = self.create()
        client = self.start(session)
        client.callback({"id": 9, "method": "item/commandExecution/requestApproval",
                         "params": {"threadId": "thread-123"}})
        self.manager.respond(session["id"], 9, {"decision": "cancel"})
        client.close()
        client.callback({"method": "connection/closed", "params": {"detail": "test disconnect"}})
        before = len(FakeClient.instances)
        self.assertEqual(self.manager._copy(session["id"])["status"], "disconnected")
        self.assertEqual(len(FakeClient.instances), before)
        self.manager.message(session["id"], "核对已有远端任务，再按修订要求继续")
        self.manager._workers[session["id"]].join(5)
        resumed = FakeClient.instances[-1]
        self.assertEqual(resumed.requests[0], ("thread/resume", {"cwd": session["worktree"],
                         "approvalPolicy": "on-request", "approvalsReviewer": "user",
                         "sandbox": "workspace-write", "threadId": "thread-123"}))
        self.assertFalse(self.manager._copy(session["id"])["approval_cancel_pending"])

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


class ReportedProvenanceTest(unittest.TestCase):
    setUp = SessionsTest.setUp
    tearDown = SessionsTest.tearDown
    git = SessionsTest.git
    create = SessionsTest.create

    def report(self, session, **changes):
        return {"branch": session["branch"], "candidate_commit": "a" * 40,
                "remote_task_workspace": "/task/backend-" + session["id"],
                "observed_at": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
                "evidence_paths": [{"path": "evidence/resource.json", "sha256": "f" * 64}], **changes}

    def write_artifact(self, session, provenance=None, **changes):
        worktree = Path(session["worktree"])
        evidence = worktree / "evidence/resource.json"
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text("{}\n", encoding="utf-8")
        artifact = {"schema_version": 1, "session_id": session["id"], "backend": session["backend"],
                    "plan": [{"step": "Gate", "status": "pending"}], "summary": "Awaiting hardware evidence", **changes}
        if provenance is not None:
            artifact["provenance"] = provenance
        path = worktree / ".flflow-progress.json"
        atomic_json(path, artifact)
        return path

    def state(self):
        return self.manager.snapshot()["sessions"][0]

    def test_report_is_persisted_as_agent_report_without_verifying_hash_or_changing_execution(self):
        session = self.create()
        report = self.report(session, source="verified", gate_passed=True)
        self.write_artifact(session, report, status="completed", gate_passed=True)
        with patch.object(self.manager, "_git") as git, patch.object(self.manager, "_check_connection") as ssh:
            state = self.state()
            git.assert_not_called()
            ssh.assert_not_called()
        accepted = state["reported_provenance"]
        self.assertEqual(accepted["source"], "agent_report")
        self.assertEqual(accepted["evidence_paths"][0]["sha256"], "f" * 64)
        self.assertNotIn("hash_verified", accepted)
        self.assertNotIn("gate_passed", accepted)
        self.assertNotIn("gate_passed", state)
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(state["plan"][0]["status"], "pending")
        self.assertEqual(state["remote_workspace"], session["remote_workspace"])
        self.assertEqual(state["commit"], session["commit"])
        self.manager.close()
        self.manager = CodexSessionManager(self.config, self.base, FakeClient)
        self.assertEqual(self.state()["reported_provenance"], accepted)
        self.assertEqual(FakeClient.instances, [])

    def test_newer_plan_notification_cannot_suppress_valid_provenance(self):
        session = self.create()
        self.manager._sessions[session["id"]]["thread_id"] = "thread-123"
        plan = [{"step": "Version adaptation", "status": "in_progress"}]
        self.manager._event(session["id"], {"method": "turn/plan/updated", "params": {
            "threadId": "thread-123", "plan": plan, "explanation": "Live notification"}})
        report = self.report(session)
        path = self.write_artifact(session, report)
        old = (datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()
        os.utime(path, (old, old))
        state = self.state()
        self.assertEqual(state["reported_provenance"]["observed_at"], report["observed_at"])
        self.assertEqual(state["plan"], plan)
        self.assertEqual(state["plan_source"], "notification")
        self.assertEqual(state["progress_summary"], "Live notification")
        self.assertEqual(state["status"], "prepared")

    def test_missing_invalid_and_older_reports_preserve_observation_time(self):
        session = self.create()
        report = self.report(session)
        self.write_artifact(session, report)
        accepted = self.state()["reported_provenance"]
        older = (datetime.fromisoformat(report["observed_at"]) - timedelta(minutes=1)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        invalid = [None, {**report, "candidate_commit": "short"}, {**report, "branch": "another-baseline"},
                   {**report, "observed_at": "2026-01-01T00:00:00"}, {**report, "observed_at": future},
                   {**report, "observed_at": "not-a-time"}, {**report, "remote_task_workspace": "/task/../other"},
                   {**report, "remote_task_workspace": "//task/backend"},
                   {**report, "remote_task_workspace": "relative/task"}, {**report, "remote_task_workspace": "/task\nother"},
                   {**report, "remote_task_workspace": "/"}, {**report, "remote_task_workspace": "/" + "x" * 2000},
                   {**report, "candidate_commit": "b" * 40, "observed_at": older}]
        for value in invalid:
            with self.subTest(value=value):
                self.write_artifact(session, value, summary="A newer summary is not newer evidence")
                self.assertEqual(self.state()["reported_provenance"], accepted)

    def test_observation_requires_rfc3339_and_real_calendar_date(self):
        session = self.create()
        report = self.report(session, observed_at="2024-02-29T23:59:59.123456789Z")
        self.write_artifact(session, report)
        accepted = self.state()["reported_provenance"]
        self.assertEqual(accepted["observed_at"], report["observed_at"])
        invalid = ["2024-03-01 00:00:00Z", "2024-03-01t00:00:00Z", "2024-03-01T00:00:00z",
                   "20240301T000000Z", "2024-03-01T00:00:00", "2024-03-01T00:00:00+0800",
                   "2024-03-01T00:00:00+08:00:30", "2024-03-01T00:00:00+24:00",
                   "2024-03-01T00:00:00.1234567890Z", "2024-02-30T00:00:00Z",
                   "2025-02-29T00:00:00Z", "2024-03-01T24:00:00Z", "2024-03-01T00:60:00Z",
                   "2024-03-01T00:00:60Z", "0000-03-01T00:00:00Z"]
        for observed in invalid:
            with self.subTest(observed=observed):
                self.write_artifact(session, {**report, "candidate_commit": "b" * 40, "observed_at": observed})
                self.assertEqual(self.state()["reported_provenance"], accepted)
        offset = "2024-03-01T08:00:00+08:00"
        self.write_artifact(session, {**report, "candidate_commit": "b" * 40, "observed_at": offset})
        newer = self.state()["reported_provenance"]
        self.assertEqual(newer["candidate_commit"], "b" * 40)
        self.assertEqual(newer["observed_at"], offset)
        self.assertEqual(self.state()["status"], "prepared")
        self.assertEqual(self.state()["plan"][0]["status"], "pending")

    def test_newer_observation_can_update_when_file_mtime_is_older_than_plan(self):
        session = self.create()
        report = self.report(session)
        self.write_artifact(session, report)
        first = self.state()
        newer = (datetime.fromisoformat(report["observed_at"]) + timedelta(minutes=1)).isoformat()
        path = self.write_artifact(session, {**report, "candidate_commit": "b" * 40, "observed_at": newer})
        old = (datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()
        os.utime(path, (old, old))
        state = self.state()
        self.assertEqual(state["reported_provenance"]["candidate_commit"], "b" * 40)
        self.assertEqual(state["reported_provenance"]["observed_at"], newer)
        self.assertEqual(state["plan_updated_at"], first["plan_updated_at"])

    def test_observation_order_preserves_nine_fraction_digits_across_timezones(self):
        session = self.create()
        report = self.report(session, observed_at="2024-03-01T00:00:00.123456789Z")
        self.write_artifact(session, report)
        accepted = self.state()["reported_provenance"]
        self.write_artifact(session, {**report, "candidate_commit": "b" * 40,
                                     "observed_at": "2024-03-01T08:00:00.123456788+08:00"})
        self.assertEqual(self.state()["reported_provenance"], accepted)
        newer = "2024-03-01T00:00:00.123456790Z"
        self.write_artifact(session, {**report, "candidate_commit": "b" * 40, "observed_at": newer})
        self.assertEqual(self.state()["reported_provenance"]["observed_at"], newer)
        self.assertEqual(self.state()["reported_provenance"]["candidate_commit"], "b" * 40)

    def test_report_cannot_cross_session_or_backend_ownership(self):
        session = self.create()
        report = self.report(session)
        self.write_artifact(session, report)
        accepted = self.state()["reported_provenance"]
        for fields in ({"session_id": "0" * 32}, {"backend": "metax"}, {"schema_version": True}):
            with self.subTest(fields=fields):
                self.write_artifact(session, {**report, "candidate_commit": "b" * 40}, **fields)
                self.assertEqual(self.state()["reported_provenance"], accepted)

    def test_evidence_references_reject_escape_missing_files_and_invalid_hashes(self):
        session = self.create()
        report = self.report(session)
        self.write_artifact(session, report)
        accepted = self.state()["reported_provenance"]
        invalid = [[{"path": "../outside.json"}], [{"path": "/outside.json"}],
                   [{"path": "C:\\outside.json"}], [{"path": "evidence\\..\\outside.json"}],
                   [{"path": "evidence//resource.json"}], [{"path": "evidence/./resource.json"}],
                   [{"path": "evidence/resource.json/"}], [{"path": "."}],
                   [{"path": "evidence/resource.json:stream"}], [{"path": "missing.json"}],
                   [{"path": "evidence/resource.json", "sha256": "not-a-hash"}],
                   [{"path": "evidence/resource.json", "sha256": True}],
                   [{"path": "evidence/resource.json"}] * 21, "not-a-list"]
        for references in invalid:
            with self.subTest(references=references):
                self.write_artifact(session, {**report, "evidence_paths": references})
                self.assertEqual(self.state()["reported_provenance"], accepted)
        outside = self.base / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            return outside if path.name == "linked.json" else original_resolve(path, *args, **kwargs)

        self.write_artifact(session, {**report, "evidence_paths": [{"path": "linked.json"}]})
        with patch.object(Path, "resolve", resolve):
            self.assertEqual(self.state()["reported_provenance"], accepted)

    def test_evidence_path_length_boundary(self):
        session = self.create()
        relative = "/".join(["a" * 127] * 3 + ["b" * 128])
        self.assertEqual(len(relative), 512)
        report = self.report(session, evidence_paths=[{"path": relative}])
        self.write_artifact(session, report)
        worktree = Path(session["worktree"])
        target = worktree / relative
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            # Exercise the portable API bound without depending on the OS's
            # maximum full pathname length. All other paths resolve normally.
            if path == target:
                return original_resolve(worktree / "evidence/resource.json", *args, **kwargs)
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            accepted = self.state()["reported_provenance"]
        self.assertEqual(accepted["evidence_paths"], [{"path": relative}])
        self.write_artifact(session, {**report, "evidence_paths": [{"path": relative + "b"}]})
        self.assertEqual(self.state()["reported_provenance"], accepted)

    def test_legacy_fields_and_summary_are_not_inferred_as_provenance(self):
        session = self.create()
        self.write_artifact(session, candidate_commit="a" * 40, remote_workspace="/task/candidate",
                            summary="Candidate " + "a" * 40 + " in /task/candidate")
        self.assertNotIn("reported_provenance", self.state())


if __name__ == "__main__":
    unittest.main()
