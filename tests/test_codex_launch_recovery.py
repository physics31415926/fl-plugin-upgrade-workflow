import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_sessions import CodexSessionManager, SessionError, now
from composite_sessions import CompositeSessionManager
from claude_agent import ClaudeSessionManager
from flagscale_adapter import FlagScaleSessionManager


class RecordingClient:
    instances = []
    account = {"type": "chatgpt"}

    def __init__(self, executable, cwd, callback, timeout=20):
        self.executable, self.cwd, self.callback = executable, cwd, callback
        self.requests, self.closed = [], False
        self.process = self
        self.info = {"userAgent": "test-codex"}
        self.instances.append(self)

    def poll(self):
        return 0 if self.closed else None

    def close(self):
        self.closed = True

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "account/read":
            return {"account": self.account}
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": params.get("threadId", "original-thread")}}
        if method == "turn/start":
            self.callback({"method": "turn/started", "params": {
                "threadId": params["threadId"], "turn": {"id": "new-turn"}}})
            return {"turn": {"id": "new-turn"}}
        return {}


class Settings:
    def snapshot(self):
        return {"provider": "claude", "configured": False}


class CodexLaunchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.worktree = self.root / "plugin", self.root / "original-worktree"
        self.repo.mkdir()
        self.worktree.mkdir()
        self.current = self.root / "current-codex.exe"
        self.current.write_bytes(b"explicit_dummy_executable")
        config = {"repo": str(self.repo), "branch": "main",
                  "codex_executable": str(self.root / "old-version" / "codex.exe")}
        # Keep the production resolver policy while replacing only protocol I/O.
        self.manager = CodexSessionManager(config, self.root)
        self.manager.client_factory = RecordingClient
        self.addCleanup(self.manager.close)
        self.resolver = patch("codex_runtime.resolve_codex_executable", return_value=str(self.current)).start()
        self.addCleanup(patch.stopall)
        RecordingClient.instances.clear()
        RecordingClient.account = {"type": "chatgpt"}
        self.session = {"id": "f" * 32, "backend": "enflame", "branch": "main",
                        "ssh_alias": "gpu", "ssh_connection": None, "role": "compute",
                        "worktree": str(self.worktree), "remote_workspace": "/future/candidate",
                        "created_at": now(), "updated_at": now(), "status": "failed",
                        "error": "old missing executable", "prompt": "existing task instructions",
                        "thread_id": None, "turn_id": None, "preflight_required": False,
                        "pending_requests": [], "events": []}
        self.manager._sessions[self.session["id"]] = self.session

    def test_missing_version_path_resolves_at_health_and_account_stays_separate(self):
        health = self.manager.health()
        self.assertTrue(health["available"])
        self.assertTrue(health["authenticated"])
        self.assertIsNone(health["error"])
        self.assertEqual(RecordingClient.instances[0].executable, str(self.current))
        self.assertEqual(RecordingClient.instances[0].requests[0][0], "account/read")
        self.assertFalse(any(method.startswith("turn/") for method, _ in RecordingClient.instances[0].requests))

    def test_cli_removal_invalidates_manager_and_proxy_health_cache(self):
        composite = CompositeSessionManager(self.manager, None, Settings())
        composite.health()
        self.resolver.return_value = None
        status = composite.snapshot(compact=True)
        self.assertFalse(status["available"])
        self.assertFalse(status["providers"]["codex"]["available"])
        self.assertIsNone(status["health"])
        self.assertNotIn("codex", composite._health)

    def test_new_runtime_clears_failed_cached_health_without_returning_local_path(self):
        composite = CompositeSessionManager(self.manager, None, Settings())
        self.resolver.return_value = None
        composite.health()
        self.resolver.return_value = str(self.current)
        snapshot = composite.snapshot(compact=True)
        self.assertTrue(snapshot["providers"]["codex"]["available"])
        self.assertIsNone(snapshot["providers"]["codex"].get("error"))
        self.assertNotIn(str(self.current), json.dumps(snapshot["providers"]))

    def test_same_path_binary_replacement_invalidates_cached_health(self):
        self.manager.health()
        previous = self.manager._runtime_revision
        timestamp = self.current.stat().st_mtime_ns + 1_000_000_000
        os.utime(self.current, ns=(timestamp, timestamp))
        snapshot = self.manager.snapshot()
        self.assertIsNone(snapshot["health"])
        self.assertNotEqual(snapshot["runtime_revision"], previous)

    def test_initialize_error_never_reports_available(self):
        self.manager.client_factory = Mock(side_effect=FileNotFoundError("explicit_dummy_launch_error"))
        health = self.manager.health()
        self.assertFalse(health["available"])
        self.assertIsNone(health["authenticated"])
        self.assertTrue(health["error"])

    def test_missing_authentication_does_not_mean_missing_binary(self):
        RecordingClient.account = None
        health = self.manager.health()
        self.assertTrue(health["available"])
        self.assertFalse(health["authenticated"])
        self.assertIn("login", health["error"])

    def test_missing_cli_fails_synchronously_without_worker_or_ssh_and_retains_task(self):
        self.resolver.return_value = None
        self.manager._check_connection = Mock(side_effect=AssertionError("unexpected SSH"))
        with self.assertRaisesRegex(SessionError, "Codex CLI"):
            self.manager.start(self.session["id"])
        self.assertEqual(self.session["status"], "failed")
        self.assertEqual(self.session["worktree"], str(self.worktree))
        self.assertIsNone(self.session["thread_id"])
        self.assertFalse(self.manager._workers)
        self.assertFalse(RecordingClient.instances)
        self.manager._check_connection.assert_not_called()
        self.assertIn("重试原任务", self.session["error"])

    def test_unavailable_cli_keeps_prepared_session_ready_for_later_retry(self):
        self.session["status"] = "prepared"
        self.resolver.return_value = None
        with self.assertRaises(SessionError):
            self.manager.start(self.session["id"])
        self.assertEqual(self.session["status"], "prepared")
        self.assertEqual(len(self.manager._sessions), 1)

    def test_retry_reuses_original_task_and_worktree_creates_only_one_thread(self):
        session = self.manager.start(self.session["id"])
        self.manager._workers[session["id"]].join(3)
        self.assertEqual(self.session["status"], "running")
        self.assertIsNone(self.session["error"])
        self.assertEqual(self.session["worktree"], str(self.worktree))
        self.assertEqual(len(self.manager._sessions), 1)
        client = RecordingClient.instances[-1]
        self.assertEqual(client.executable, str(self.current))
        self.assertEqual([method for method, _ in client.requests], ["thread/start", "turn/start"])
        with self.assertRaisesRegex(SessionError, "active"):
            self.manager.start(self.session["id"])
        self.assertEqual(len(RecordingClient.instances), 1)

    def test_reconnection_resumes_persisted_thread_after_upgrade(self):
        self.session.update(thread_id="persisted-thread", initial_prompt_sent=True)
        self.manager.start(self.session["id"])
        self.manager._workers[self.session["id"]].join(3)
        client = RecordingClient.instances[-1]
        self.assertEqual([method for method, _ in client.requests], ["thread/resume", "turn/start"])
        self.assertEqual(client.requests[0][1]["threadId"], "persisted-thread")

    def test_missing_worktree_has_specific_diagnostic(self):
        self.worktree.rmdir()
        with self.assertRaisesRegex(SessionError, "本地工作目录"):
            self.manager._client(self.session)
        self.assertFalse(RecordingClient.instances)

    def test_other_provider_clients_do_not_use_codex_discovery(self):
        manager = CodexSessionManager({"repo": str(self.repo), "codex_executable": "other-provider"},
                                     self.root / "other", RecordingClient)
        self.addCleanup(manager.close)
        self.resolver.side_effect = AssertionError("other provider must own its executable")
        self.assertEqual(manager._resolve_executable(), "other-provider")

    def other_provider_managers(self):
        for provider, factory, field in (("claude", ClaudeSessionManager, "claude_executable"),
                                         ("flagscale", FlagScaleSessionManager, "flagscale_python")):
            native = self.root / (provider + "-runtime.exe")
            native.write_bytes(b"explicit_dummy_provider_runtime")
            config = {"repo": str(self.repo), "branch": "main", "sessions_dir": "runs/" + provider,
                      "codex_executable": "explicit_dummy_unrelated_codex", field: str(native)}
            settings = Mock()
            settings.snapshot.return_value = {"provider": provider, "configured": False}
            settings.runtime.side_effect = AssertionError("unexpected credential or model access")
            manager = factory(config, self.root, settings)
            self.addCleanup(manager.close)
            yield provider, manager, native

    def test_real_other_provider_snapshots_preserve_their_own_executable(self):
        self.resolver.side_effect = AssertionError("Codex discovery must not run for another provider")
        for provider, manager, native in self.other_provider_managers():
            with self.subTest(provider=provider):
                snapshot = manager.snapshot(compact=True)
                self.assertEqual(snapshot["provider"], provider)
                self.assertTrue(snapshot["available"])
                self.assertIsNone(snapshot["runtime_revision"])
                self.assertFalse(manager._uses_codex_runtime)
                self.assertEqual(manager.executable, str(native))
                manager.agent_settings.runtime.assert_not_called()

    def test_real_other_provider_launches_do_not_apply_codex_runtime_checks(self):
        self.resolver.side_effect = AssertionError("Codex discovery must not run for another provider")
        for provider, manager, native in self.other_provider_managers():
            with self.subTest(provider=provider):
                session = {**self.session, "provider": provider, "status": "prepared"}
                manager._sessions[session["id"]] = session
                manager._save = Mock()
                manager._require_free_machine = Mock()
                manager._check_connection = Mock(side_effect=AssertionError("unexpected SSH"))
                manager._run = Mock(side_effect=AssertionError("unexpected model worker"))
                with patch("codex_sessions.threading.Thread") as worker:
                    result = manager._launch(session["id"], "synthetic task instructions")
                self.assertEqual(result["status"], "starting")
                self.assertEqual(manager.executable, str(native))
                worker.assert_called_once_with(target=manager._run,
                                               args=(session["id"], "synthetic task instructions"), daemon=True)
                worker.return_value.start.assert_called_once_with()
                manager._check_connection.assert_not_called()
                manager._run.assert_not_called()
                manager.agent_settings.runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
