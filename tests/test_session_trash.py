"""Recycle-bin changes preserve tasks and enforce execution/lease boundaries."""
import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

import test_codex_sessions as fixtures
from test_composite_sessions import FakeOwner, FakeSettings
from fl_dashboard.agents.codex_sessions import CodexSessionManager, SessionError
from fl_dashboard.agents.composite_sessions import CompositeSessionManager
from fl_dashboard.projects.project_registry import RemoteSessionManager
from fl_dashboard.tasks.readonly_session_observer import ReadonlySessionObserver


class TaskTrashTests(unittest.TestCase):
    setUp = fixtures.SessionsTest.setUp
    tearDown = fixtures.SessionsTest.tearDown
    git = fixtures.SessionsTest.git
    create = fixtures.SessionsTest.create
    start = fixtures.SessionsTest.start

    def test_delete_preserves_artifacts_and_persists_across_restart(self):
        session = self.create()
        ident = session["id"]
        stored = self.manager._sessions[ident]
        stored.update(thread_id="persistent-thread", events=[{"method": "saved-event"}])
        self.manager._save(stored)
        log = self.manager.root / ident / "events.jsonl"
        log.write_text("saved log\n", encoding="utf8")
        evidence = self.manager.evidence_dir / "saved-evidence.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text('{"accepted":true}', encoding="utf8")
        worktree = Path(session["worktree"])
        row = self.manager.delete(ident)
        self.assertTrue(row["deleted_at"])
        self.assertFalse(row["machine_reserved"])
        self.assertEqual(self.manager.allocation_sessions(), [])
        self.assertEqual(self.manager.snapshot()["sessions"], [])
        self.assertEqual(self.manager.snapshot()["deleted_sessions"], [row])
        self.assertNotIn("events", row)
        self.assertNotIn("prompt", row)
        self.assertEqual(self.manager.delete(ident), row)
        self.manager.close()
        self.manager = CodexSessionManager(self.config, self.base, fixtures.FakeClient)
        self.assertEqual(self.manager.snapshot()["sessions"], [])
        restored = self.manager.restore(ident)
        self.assertNotIn("deleted_at", restored)
        self.assertEqual(restored["thread_id"], "persistent-thread")
        self.assertEqual(self.manager.restore(ident), restored)
        self.assertEqual(self.manager.snapshot()["sessions"][0]["events"], [{"method": "saved-event"}])
        self.assertEqual(log.read_text(encoding="utf8"), "saved log\n")
        self.assertEqual(evidence.read_text(encoding="utf8"), '{"accepted":true}')
        self.assertTrue(worktree.is_dir())
        self.assertEqual(fixtures.FakeClient.instances, [])

    def test_replacement_can_be_created_but_restore_prepared_checks_device(self):
        original = self.create()
        self.manager.delete(original["id"])
        replacement = self.create()
        self.assertNotEqual(original["id"], replacement["id"])
        with self.assertRaisesRegex(SessionError, "reserved"):
            self.manager.restore(original["id"])
        self.assertTrue(self.manager._sessions[original["id"]]["deleted_at"])
        self.manager.interrupt(replacement["id"])
        self.manager.restore(original["id"])
        self.assertEqual(len(self.manager.snapshot()["sessions"]), 2)

    def test_all_direct_mutations_and_late_events_are_blocked_in_trash(self):
        session = self.create()
        ident = session["id"]
        self.manager.delete(ident)
        before = copy.deepcopy(self.manager._sessions[ident])
        actions = [lambda: self.manager.start(ident), lambda: self.manager.message(ident, "continue"),
                   lambda: self.manager.interrupt(ident), lambda: self.manager.respond(ident, 1, {}),
                   lambda: self.manager.desktop(ident)]
        for action in actions:
            with self.subTest(action=action), self.assertRaisesRegex(SessionError, "recycle bin"):
                action()
        self.manager._event(ident, {"method": "turn/started", "params": {"turn": {"id": "late"}}})
        self.assertEqual(self.manager._sessions[ident], before)
        for invalid in ("native-example", "../outside", "f" * 32, None):
            for action in (self.manager.delete, self.manager.restore):
                with self.subTest(invalid=invalid), self.assertRaises(SessionError):
                    action(invalid)

    def test_active_startup_and_live_provider_reader_refuse_delete(self):
        entered, release = threading.Event(), threading.Event()
        class DelayedClient(fixtures.FakeClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                entered.set()
                release.wait(5)
        self.manager.client_factory = DelayedClient
        session = self.create()
        ident = session["id"]
        try:
            self.manager.start(ident)
            self.assertTrue(entered.wait(3))
            with self.assertRaisesRegex(SessionError, "active"):
                self.manager.delete(ident)
            # Cancellation while the worker is blocked must not release the lease.
            self.manager.interrupt(ident)
            with self.assertRaisesRegex(SessionError, "active"):
                self.manager.delete(ident)
        finally:
            release.set()
            self.manager._workers[ident].join(5)
        self.manager._sessions[ident]["status"] = "completed"
        client = self.manager._clients[ident]
        client.active = True
        with self.assertRaisesRegex(SessionError, "active"):
            self.manager.delete(ident)
        client.active = False
        self.assertTrue(self.manager.delete(ident)["deleted_at"])

    def test_desktop_lease_requires_confirmed_completion_without_deleting_the_thread(self):
        session = self.create()
        ident = session["id"]
        stored = self.manager._sessions[ident]
        stored.update(status="completed", execution_owner="codex_desktop",
                      desktop_lease_released=False, thread_id="preserved-desktop-thread")
        with patch("fl_dashboard.agents.codex_sessions.refresh_desktop"):
            with self.assertRaisesRegex(SessionError, "active"):
                self.manager.delete(ident)
            stored["desktop_lease_released"] = True
            row = self.manager.delete(ident)
            self.assertEqual(row["thread_id"], "preserved-desktop-thread")
            self.assertEqual(row["execution_owner"], "codex_desktop")
            self.assertEqual(self.manager.restore(ident)["thread_id"], "preserved-desktop-thread")
        self.assertEqual(fixtures.FakeClient.instances, [])

    def test_failed_persistence_does_not_release_or_reacquire_a_machine(self):
        session = self.create()
        ident = session["id"]
        with patch("fl_dashboard.agents.codex_sessions.atomic_json", side_effect=PermissionError()):
            with self.assertRaisesRegex(SessionError, "could not be saved"):
                self.manager.delete(ident)
        self.assertFalse(self.manager._sessions[ident].get("deleted_at"))
        self.assertEqual(self.manager.allocation_sessions()[0]["id"], ident)
        self.manager.delete(ident)
        with patch("fl_dashboard.agents.codex_sessions.atomic_json", side_effect=PermissionError()):
            with self.assertRaisesRegex(SessionError, "could not be saved"):
                self.manager.restore(ident)
        self.assertTrue(self.manager._sessions[ident]["deleted_at"])
        self.assertEqual(self.manager.allocation_sessions(), [])

    def test_composite_retains_provider_ownership_and_cross_provider_restore_conflict(self):
        claude = FakeOwner("claude", self.base)
        composite = CompositeSessionManager(self.manager, claude, FakeSettings())
        session = self.create()
        ident = session["id"]
        self.assertEqual(composite.delete(ident)["provider"], "codex")
        self.assertEqual(composite.snapshot()["sessions"], [])
        self.assertEqual(composite.snapshot()["deleted_sessions"][0]["id"], ident)
        with self.assertRaises(SessionError):
            composite.start(ident)
        claude.create({**self.payload, "provider": "claude"})
        with self.assertRaisesRegex(SessionError, "reserved"):
            composite.restore(ident)
        claude.rows.clear()
        self.assertEqual(composite.restore(ident)["provider"], "codex")

    def test_readonly_observer_removes_cached_tombstone_and_rediscovers_restore(self):
        session = self.create()
        observer = ReadonlySessionObserver([{"provider": "codex", "directory": self.manager.root}])
        before = observer.snapshot()
        self.assertEqual(len(before), 1)
        self.manager.delete(session["id"])
        self.assertEqual(observer.snapshot(), [])
        self.manager.restore(session["id"])
        self.assertEqual(observer.snapshot()[0]["id"], before[0]["id"])

    def test_remote_proxy_retains_trash_and_checks_peer_lease_before_restore(self):
        ident = "a" * 32
        row = {"id": ident, "status": "prepared", "deleted_at": "2026-01-01T00:00:00Z",
               "backend": "cuda", "branch": self.payload["branch"], "ssh_alias": "gpu_remote"}
        proxy = RemoteSessionManager({**self.config, "sessions_api_url": "http://127.0.0.1:8765",
            "sessions_dir": str(self.base / "proxy"), "evidence_dir": str(self.base / "proxy-evidence")}, self.base)
        snapshot = {"sessions": [], "deleted_sessions": [row]}
        def request(path, body=None):
            return copy.deepcopy(snapshot if path.startswith("/api/codex-sessions?") else row)
        with patch.object(proxy, "_request", side_effect=request) as remote:
            self.assertEqual(proxy.snapshot(compact=True)["deleted_sessions"], [row])
            self.assertEqual(proxy.delete(ident), row)
            remote.assert_called_with(f"/api/codex-sessions/{ident}/delete", {})
            peer = Mock(allocation_sessions=Mock(return_value=[{**row, "id": "b"*32, "deleted_at": None}]))
            proxy.peer_managers = [proxy, peer]
            with self.assertRaisesRegex(SessionError, "active or prepared"):
                proxy.restore(ident)
            self.assertFalse(any(call.args[0].endswith("/restore") for call in remote.call_args_list))
            peer.allocation_sessions.return_value = []
            proxy.restore(ident)
            remote.assert_called_with(f"/api/codex-sessions/{ident}/restore", {})
            for action in (proxy.delete, proxy.restore):
                with self.assertRaises(SessionError):
                    action("native-task")
        self.assertFalse(proxy.root.exists())


if __name__ == "__main__":
    unittest.main()
