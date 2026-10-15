import json
from datetime import datetime, timedelta, timezone
from http.client import HTTPSConnection
from pathlib import Path
import tempfile
import ssl
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fl_dashboard.agents.codex_sessions import CodexSessionManager, SessionError, now, require_free_machine
from fl_dashboard.agents.codex_desktop import DesktopRolloutObserver


THREAD = "01a124ca-a49b-7753-af99-1b23be9d5e79"
CHILD = "01a124ca-a49b-7753-af99-1b23be9d5e70"
TASK = "d" * 32


class Client:
    def __init__(self, callback, path=None):
        self.callback, self.path = callback, path
        self.requests, self.closed, self.timeout = [], False, 20
        self.process = self
        self.interrupt = lambda params: self.end(params["turnId"])
        self.fail_name = False
        self.fail_close = False

    def poll(self):
        return 0 if self.closed else None

    def close(self):
        if self.fail_close:
            raise OSError("explicit_dummy close failure")
        self.closed = True

    def end(self, turn="original", thread=THREAD, status="interrupted"):
        self.callback({"method": "turn/completed", "params": {
            "threadId": thread, "turn": {"id": turn, "status": status}}})

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/name/set" and self.fail_name:
            raise SessionError("explicit_dummy unsupported method")
        if method in ("thread/start", "thread/resume", "thread/read"):
            return {"thread": {"id": THREAD, "path": str(self.path) if self.path else None}}
        if method == "turn/interrupt":
            self.interrupt(params)
        if method in ("turn/start", "turn/steer"):
            raise AssertionError("No model turns may be invoked by desktop handoff")
        return {}


class DesktopTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo, self.worktree = self.root / "plugin", self.root / "worktree"
        self.repo.mkdir()
        self.worktree.mkdir()
        self.config = {"repo": str(self.repo), "branch": "main", "codex_project_id": "existing-project"}
        self.manager = CodexSessionManager(self.config, self.root)
        self.manager.executable = None
        self.addCleanup(self.manager.close)
        self.session = {"id": TASK, "backend": "enflame", "branch": "main", "provider": "codex",
                        "ssh_alias": "explicit_dummy_gpu", "ssh_connection": None,
                        "worktree": str(self.worktree), "created_at": now(), "updated_at": now(),
                        "status": "completed", "prompt": "task", "thread_id": THREAD,
                        "turn_id": None, "pending_requests": [], "events": []}
        self.manager._sessions[TASK] = self.session
        self.manager._save(self.session)
        self.launcher = Mock()
        self.resolver = patch.object(self.manager, "_resolve_executable", return_value=None).start()
        self.addCleanup(patch.stopall)
        self.manager.client_factory = Mock(side_effect=AssertionError("No process should be needed"))
        self.client = Client(lambda event: self.manager._event(TASK, event))

    def active(self):
        self.session.update(status="waiting_approval", turn_id="original")
        self.manager._clients[TASK] = self.client

    def handoff(self, **kwargs):
        return self.manager.desktop(TASK, launcher=self.launcher, **kwargs)

    def rollout(self, events):
        path = self.root / "rollout.jsonl"
        header = {"type": "session_meta", "payload": {"id": THREAD}}
        path.write_text("".join(json.dumps(e) + "\n" for e in [header, *events]), encoding="utf-8")
        self.session["rollout_path"] = str(path)
        return path

    @staticmethod
    def event(kind, stamp, turn="desktop-turn", **fields):
        return {"timestamp": stamp, "type": "event_msg", "payload": {
            "type": kind, "turn_id": turn, "thread_id": THREAD, **fields}}

    def append(self, path, events):
        with path.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")

    def test_idle_persisted_thread_transfers_without_runtime_or_new_thread(self):
        result = self.handoff()
        self.assertEqual(result["desktop_url"], "codex://threads/" + THREAD)
        self.assertEqual(result["session"]["id"], TASK)
        self.assertEqual(result["session"]["status"], "unknown")
        self.assertTrue(result["session"]["machine_reserved"])
        self.assertFalse(result["session"]["controllable"])
        self.assertEqual(result["session"]["execution_owner"], "codex_desktop")
        self.launcher.assert_called_once_with(result["desktop_url"])
        self.manager.client_factory.assert_not_called()

    def test_active_transfer_waits_for_matching_turn_and_closes_original_client(self):
        self.active()
        self.client.interrupt = lambda params: (self.client.end(thread=CHILD), self.client.end(turn="other"), self.client.end())
        self.handoff()
        self.assertTrue(self.client.closed)
        self.assertEqual(self.client.requests[0], ("turn/interrupt", {"threadId": THREAD, "turnId": "original"}))
        self.assertNotIn(TASK, self.manager._clients)
        self.assertTrue(self.session["desktop_handoff_confirmed"])
        self.assertFalse(any(m in ("thread/start", "thread/resume", "turn/start", "turn/steer") for m, _ in self.client.requests))
        self.assertIn(("thread/metadata/update", {"threadId": THREAD, "projectId": "existing-project"}), self.client.requests)

    def test_unrelated_or_missing_terminal_never_opens_or_releases_ownership(self):
        for terminal in (lambda: self.client.end(thread=CHILD), lambda: self.client.end(turn="other"),
                         lambda: self.client.end(status="inProgress"), lambda: None):
            with self.subTest(terminal=terminal):
                self.active()
                self.client.interrupt = lambda params: terminal()
                with self.assertRaises(SessionError):
                    self.handoff(timeout=0.05)
                self.assertNotEqual(self.session.get("execution_owner"), "codex_desktop")
                self.assertTrue(self.manager._session_view(self.session)["machine_reserved"])
                self.assertFalse(self.client.closed)
        self.launcher.assert_not_called()

    def test_completed_turn_still_waits_until_startup_worker_exits(self):
        self.active()
        worker = Mock()
        worker.is_alive.return_value = True
        self.manager._workers[TASK] = worker
        with self.assertRaises(SessionError):
            self.handoff(timeout=0.05)
        self.launcher.assert_not_called()
        self.assertFalse(self.client.closed)
        worker.is_alive.return_value = False
        self.handoff()
        self.assertTrue(self.client.closed)

    def test_lost_active_client_cannot_transfer_after_restart(self):
        self.session.update(status="disconnected", turn_id="original")
        with self.assertRaises(SessionError):
            self.handoff()
        self.assertTrue(self.manager._session_view(self.session)["machine_reserved"])
        self.launcher.assert_not_called()

    def test_close_failure_keeps_original_owner_and_lease(self):
        self.active()
        self.client.fail_close = True
        with self.assertRaises(SessionError):
            self.handoff()
        self.assertNotEqual(self.session.get("execution_owner"), "codex_desktop")
        self.assertTrue(self.manager._session_view(self.session)["machine_reserved"])
        self.launcher.assert_not_called()
        self.client.fail_close = False

    def test_open_failure_can_retry_same_url_without_reclaiming_or_interrupting(self):
        self.active()
        self.launcher.side_effect = OSError("explicit_dummy desktop URL unavailable")
        result = self.handoff()
        count = len(self.client.requests)
        self.assertFalse(result["desktop_opened"])
        self.assertEqual(result["session"]["execution_owner"], "codex_desktop")
        self.launcher.side_effect = None
        self.assertTrue(self.handoff()["desktop_opened"])
        self.assertEqual(len(self.client.requests), count)
        self.assertEqual(self.launcher.call_args[0][0], "codex://threads/" + THREAD)

    def test_control_methods_reject_transferred_owner_after_restart(self):
        self.handoff()
        reloaded = CodexSessionManager(self.config, self.root)
        self.addCleanup(reloaded.close)
        for manager in (self.manager, reloaded):
            for call in (lambda: manager.start(TASK), lambda: manager.message(TASK, "continue"),
                         lambda: manager.interrupt(TASK), lambda: manager.respond(TASK, "r", {}),
                         lambda: manager._client(manager._sessions[TASK])):
                with self.assertRaises(SessionError):
                    call()
            self.assertTrue(manager.snapshot()["sessions"][0]["machine_reserved"])

    def test_unknown_monitor_provider_and_nonpersisted_thread_reject_before_launch(self):
        with self.assertRaises(SessionError):
            self.manager.desktop("0" * 32, launcher=self.launcher)
        for changes in ({"provider": "claude"}, {"thread_id": None}, {"thread_id": THREAD + "?bad"}):
            previous = dict(self.session)
            self.session.update(changes)
            with self.assertRaises(SessionError):
                self.handoff()
            self.session.clear()
            self.session.update(previous)
        self.manager.backend_task_policies = {"enflame": {"mode": "monitor", "branch": "main", "reason": "existing PR", "pr": 1}}
        with self.assertRaises(SessionError):
            self.handoff()
        self.launcher.assert_not_called()

    def test_metadata_only_client_never_resumes_and_recovers_protocol_returned_path(self):
        path = self.rollout([])
        self.session.pop("rollout_path")
        self.client.path = path
        self.resolver.return_value = "explicit_dummy_codex.exe"
        self.manager.client_factory = Mock(return_value=self.client)
        self.handoff()
        self.assertTrue(self.client.closed)
        self.assertEqual(self.session["rollout_path"], str(path))
        self.assertEqual([m for m, _ in self.client.requests], ["thread/read", "thread/name/set", "thread/metadata/update"])
        self.assertFalse(self.manager._clients)

    def test_optional_metadata_timeout_preserves_budget_for_safe_transfer(self):
        self.active()
        original = self.client.request
        def request(method, params):
            if method == "thread/read":
                time.sleep(self.client.timeout + 0.002)
                raise SessionError("explicit_dummy optional metadata timeout")
            return original(method, params)
        self.client.request = request
        result = self.handoff(timeout=0.15)
        self.assertTrue(result["desktop_opened"])
        self.assertTrue(self.client.closed)
        self.assertEqual(result["session"]["execution_owner"], "codex_desktop")
        self.assertIn("desktop_metadata_error", result["session"])

    def test_client_close_timeout_does_not_open_and_retry_uses_persisted_thread(self):
        self.active()
        original = self.client.close
        def close():
            time.sleep(0.08)
            original()
        self.client.close = close
        with self.assertRaises(SessionError):
            self.handoff(timeout=0.04)
        self.launcher.assert_not_called()
        self.assertNotEqual(self.session.get("execution_owner"), "codex_desktop")
        self.assertTrue(self.manager._session_view(self.session)["machine_reserved"])
        self.assertTrue(self.handoff()["desktop_opened"])

    def test_thread_identity_and_path_saved_before_best_effort_title(self):
        path = self.rollout([])
        self.session.update(thread_id=None)
        self.client.path, self.client.fail_name = path, True
        self.manager.executable = "explicit_dummy_codex.exe"
        self.resolver.return_value = self.manager.executable
        self.manager.client_factory = Mock(return_value=self.client)
        self.manager._client(self.session)
        params = self.client.requests[0][1]
        self.assertFalse(params["ephemeral"])
        self.assertNotIn("projectId", params)
        self.assertIn(("thread/metadata/update", {"threadId": THREAD, "projectId": "existing-project"}), self.client.requests)
        persisted = json.loads((self.manager.root / TASK / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["thread_id"], THREAD)
        self.assertEqual(persisted["rollout_path"], str(path))
        self.assertIn("thread_name_error", persisted)
        self.manager._client(self.session)
        self.assertEqual(sum(method == "thread/start" for method, _ in self.client.requests), 1)

    def test_invalid_optional_project_does_not_prevent_same_persisted_turn_start(self):
        path = self.rollout([])
        self.client.path = path
        self.session.update(thread_id=None, status="starting")
        original = self.client.request
        def request(method, params):
            if method == "thread/start":
                self.assertNotIn("projectId", params)
            if method == "thread/metadata/update":
                self.client.requests.append((method, params))
                raise SessionError("explicit_dummy private-path project not found")
            if method == "turn/start":
                stored = json.loads((self.manager.root / TASK / "session.json").read_text(encoding="utf-8"))
                self.assertEqual(stored["thread_id"], THREAD)
                self.assertEqual(stored["rollout_path"], str(path))
                self.assertNotIn("explicit_dummy private-path", json.dumps(stored))
                self.client.requests.append((method, params))
                self.client.callback({"method": "turn/started", "params": {
                    "threadId": THREAD, "turn": {"id": "new-turn"}}})
                return {"turn": {"id": "new-turn"}}
            return original(method, params)
        self.client.request = request
        self.manager.client_factory = Mock(return_value=self.client)
        self.manager.executable = "explicit_dummy_codex.exe"
        self.resolver.return_value = self.manager.executable
        self.manager._run(TASK, "synthetic task")
        self.assertEqual(self.session["status"], "running")
        self.assertEqual(self.session["thread_id"], THREAD)
        self.assertIn("thread_project_error", self.session)
        self.assertEqual([method for method, _ in self.client.requests],
                         ["thread/start", "thread/name/set", "thread/metadata/update", "turn/start"])
        self.assertEqual(self.client.requests[-1][1]["threadId"], THREAD)
        self.manager._client(self.session)
        self.assertEqual(sum(method == "thread/start" for method, _ in self.client.requests), 1)

    def test_old_interruption_cannot_release_desktop_lease_even_through_prefix_recovery(self):
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        path = self.rollout([self.event("task_started", old, turn="original"),
                             self.event("task_interrupted", old, turn="original"),
                             {"type": "response_item", "payload": {"type": "function_call_output", "output": "private-tool-output" * 100}}])
        with patch.object(DesktopRolloutObserver, "INITIAL_BYTES", 256):
            result = self.handoff()
        self.assertEqual(result["session"]["status"], "unknown")
        self.assertTrue(result["session"]["machine_reserved"])
        self.assertNotIn("private-tool-output", json.dumps(result))
        self.assertTrue(path.exists())

    def test_new_native_turn_progress_and_matching_terminal_release_machine(self):
        path = self.rollout([])
        self.handoff()
        stamp = (datetime.fromisoformat(self.session["desktop_handed_off_at"]) + timedelta(seconds=1)).isoformat()
        plan = {"timestamp": stamp, "type": "response_item", "payload": {"type": "function_call", "name": "update_plan", "arguments": json.dumps({"plan": [{"step": "适配", "status": "in_progress"}]})}}
        self.append(path, [self.event("task_started", stamp), plan])
        running = self.manager.snapshot()["sessions"][0]
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["plan"], [{"step": "适配", "status": "in_progress"}])
        self.assertTrue(running["machine_reserved"])
        self.append(path, [self.event("task_complete", stamp, turn="other")])
        self.assertTrue(self.manager.snapshot()["sessions"][0]["machine_reserved"])
        self.append(path, [self.event("task_complete", stamp, last_agent_message="已完成本轮检查")])
        done = self.manager.snapshot()["sessions"][0]
        self.assertEqual(done["status"], "completed")
        self.assertFalse(done["machine_reserved"])
        stored = json.loads((self.manager.root / TASK / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["last_agent_message"], "已完成本轮检查")
        self.assertFalse(stored["machine_reserved"])

    def test_large_prefix_preserves_new_turn_epoch_and_matching_terminal(self):
        path = self.rollout([])
        self.handoff()
        stamp = (datetime.fromisoformat(self.session["desktop_handed_off_at"]) + timedelta(seconds=1)).isoformat()
        self.append(path, [self.event("task_started", stamp),
                           {"type": "response_item", "payload": {"type": "function_call_output", "output": "explicit_dummy_private" * 100}},
                           self.event("task_complete", stamp)])
        self.manager._desktop_observers.clear()
        with patch.object(DesktopRolloutObserver, "INITIAL_BYTES", 256):
            row = self.manager.snapshot()["sessions"][0]
        self.assertEqual(row["status"], "completed")
        self.assertFalse(row["machine_reserved"])
        self.assertNotIn("explicit_dummy_private", json.dumps(row))

    def test_invalid_rollout_identity_and_partial_terminal_hold_lease(self):
        path = self.rollout([])
        self.handoff()
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": CHILD}}) + "\n", encoding="utf-8")
        self.assertTrue(self.manager.snapshot()["sessions"][0]["machine_reserved"])
        self.rollout([])
        stamp = (datetime.fromisoformat(self.session["desktop_handed_off_at"]) + timedelta(seconds=1)).isoformat()
        self.append(path, [self.event("task_started", stamp)])
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.event("task_complete", stamp)))
        self.assertTrue(self.manager.snapshot()["sessions"][0]["machine_reserved"])
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        self.assertFalse(self.manager.snapshot()["sessions"][0]["machine_reserved"])

    def test_unknown_new_turn_clears_previous_terminal_proof(self):
        path = self.rollout([])
        self.handoff()
        stamp = (datetime.fromisoformat(self.session["desktop_handed_off_at"]) + timedelta(seconds=1)).isoformat()
        self.append(path, [self.event("task_started", stamp), self.event("task_complete", stamp)])
        self.assertFalse(self.manager.snapshot()["sessions"][0]["machine_reserved"])
        for missing in (None, "", 12):
            self.append(path, [self.event("task_started", stamp, turn=missing), self.event("task_complete", stamp)])
            row = self.manager.snapshot()["sessions"][0]
            self.assertTrue(row["machine_reserved"])
            self.assertEqual(row["status"], "unknown")

    def test_lan_post_never_calls_control_manager_or_desktop_launcher(self):
        from fl_dashboard.services.share_server import BoundedHTTPServer, make_server, load_tls_context
        from tests.test_share_server import FakeMonitor, SnapshotCache, EXPLICIT_DUMMY_TLS_CERT, EXPLICIT_DUMMY_TLS_KEY
        cache = SnapshotCache(FakeMonitor(), refresh_interval=0)
        index = self.root / "index.html"
        index.write_text("<html>read only</html>", encoding="utf-8")
        cert, key = self.root / "explicit_dummy-cert.pem", self.root / "explicit_dummy-key.pem"
        cert.write_text(EXPLICIT_DUMMY_TLS_CERT, encoding="ascii")
        key.write_text(EXPLICIT_DUMMY_TLS_KEY, encoding="ascii")
        with patch("fl_dashboard.services.share_server.BoundedHTTPServer", side_effect=lambda address, handler:
                   BoundedHTTPServer(("127.0.0.1", address[1]), handler)):
            server = make_server(cache, "192.168.10.20", 0, index_path=index, anonymous_lan=True,
                                 tls_context=load_tls_context(cert, key))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.object(self.manager, "desktop") as control, patch("fl_dashboard.agents.codex_desktop.open_desktop_url") as open_url:
                connection = HTTPSConnection("127.0.0.1", server.server_port, timeout=3,
                                             context=ssl.create_default_context(cafile=str(cert)))
                connection.request("POST", "/api/codex-sessions/" + TASK + "/desktop", body="{}", headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 405)
                response.read()
                connection.close()
                control.assert_not_called()
                open_url.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)
            cache.close()

    def test_cross_project_snapshot_lease_blocks_machine_even_when_status_unknown(self):
        self.handoff()
        row = self.manager.snapshot(compact=True)["sessions"][0]
        peer = Mock()
        peer.allocation_sessions.return_value = [row]
        with self.assertRaises(SessionError):
            require_free_machine({"ssh_alias": self.session["ssh_alias"]}, [peer])
        self.assertTrue(self.manager.allocation_sessions()[0]["machine_reserved"])

    def test_late_original_event_cannot_overwrite_transferred_owner(self):
        self.handoff()
        self.client.end()
        self.assertEqual(self.session["status"], "unknown")
        self.assertTrue(self.session["machine_reserved"])

    def test_no_changes_do_not_rewrite_persisted_safe_progress(self):
        self.rollout([])
        self.handoff()
        with patch.object(self.manager, "_save") as save:
            self.manager.snapshot()
            self.manager.snapshot()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
