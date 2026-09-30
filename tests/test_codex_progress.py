import json
import os
from pathlib import Path
import time
from unittest import TestCase, mock

from codex_sessions import atomic_json
from tests import test_codex_sessions as fixtures


class ProgressArtifactTest(TestCase):
    setUp = fixtures.SessionsTest.setUp
    tearDown = fixtures.SessionsTest.tearDown
    git = fixtures.SessionsTest.git
    start = fixtures.SessionsTest.start

    def artifact(self, session, **changes):
        return {"schema_version": 1, "session_id": session["id"], "backend": session["backend"],
                "plan": [{"step": "环境检查", "status": "completed"}, {"step": "Gate 验收", "status": "in_progress"}],
                "summary": "环境检查通过，验收仍在执行。", **changes}

    def write_progress(self, session, **changes):
        file = Path(session["worktree"]) / ".flflow-progress.json"
        atomic_json(file, self.artifact(session, **changes))
        return file

    def test_actual_artifact_updates_plan_without_changing_running_status_or_gate(self):
        session = self.manager.create(self.payload)
        self.start(session)
        self.write_progress(session, status="completed", gate_passed=True)
        state = self.manager.snapshot()["sessions"][0]
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["plan_source"], "artifact")
        self.assertEqual(state["plan"][1]["status"], "in_progress")
        self.assertIn("仍在执行", state["progress_summary"])
        self.assertEqual(state["progress_artifact"], ".flflow-progress.json")
        self.assertNotIn("gate_passed", state)
        self.assertTrue(state["plan_updated_at"])
        stored = json.loads((self.manager.root / session["id"] / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["plan"], state["plan"])

    def test_invalid_artifacts_retain_last_valid_plan(self):
        session = self.manager.create(self.payload)
        file = self.write_progress(session)
        original = self.manager.snapshot()["sessions"][0]["plan"]
        invalid = [{"session_id": "wrong-session"}, {"backend": "wrong-backend"}, {"schema_version": True},
                   {"plan": [{"step": "Gate", "status": "success"}]},
                   {"plan": [{"step": "", "status": "pending"}]},
                   {"plan": [{"step": "Gate", "status": ["pending"]}]},
                   {"plan": [{"step": "Gate", "status": "pending"}] * 31},
                   {"summary": {"unsafe": "not text"}}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.write_progress(session, **changes)
                self.assertEqual(self.manager.snapshot()["sessions"][0]["plan"], original)
        for text in ("{incomplete json", "x" * 65537):
            file.write_text(text, encoding="utf-8")
            self.assertEqual(self.manager.snapshot()["sessions"][0]["plan"], original)
        file.write_text(json.dumps(self.artifact(session, plan=[{"step": "\ud800", "status": "pending"}])), encoding="utf-8")
        self.assertEqual(self.manager.snapshot()["sessions"][0]["plan"], original)

    def test_resolved_file_outside_worktree_is_rejected_without_opening_it(self):
        session = self.manager.create(self.payload)
        self.write_progress(session)
        outside = self.base / "outside-progress.json"
        outside.write_text(json.dumps(self.artifact(session)), encoding="utf-8")
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            # Simulate a symlink target without requiring Windows symlink
            # privileges. The actual outside file must never be opened.
            if path.name == ".flflow-progress.json":
                return outside
            return original_resolve(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", resolve), mock.patch.object(Path, "open", side_effect=AssertionError("outside file read")):
            state = self.manager.snapshot()["sessions"][0]
        self.assertEqual(state["plan"], [])

    def test_newer_notification_has_priority_until_artifact_is_updated(self):
        session = self.manager.create(self.payload)
        file = self.write_progress(session)
        old_time = time.time() - 20
        os.utime(file, (old_time, old_time))
        self.manager.snapshot()
        notification_plan = [{"step": "服务启动", "status": "in_progress"}]
        self.manager._sessions[session["id"]]["thread_id"] = "thread-123"
        self.manager._event(session["id"], {"method": "turn/plan/updated", "params": {"threadId": "thread-123", "plan": notification_plan}})
        state = self.manager.snapshot()["sessions"][0]
        self.assertEqual(state["plan"], notification_plan)
        self.assertEqual(state["plan_source"], "notification")
        self.assertIsNone(state["progress_summary"])
        file = self.write_progress(session, plan=[{"step": "Gate 验收", "status": "completed"}], summary="验收结果已归档。")
        new_time = time.time() + 2
        os.utime(file, (new_time, new_time))
        state = self.manager.snapshot()["sessions"][0]
        self.assertEqual(state["plan_source"], "artifact")
        self.assertEqual(state["plan"][0]["status"], "completed")
        self.assertEqual(state["status"], "prepared")

    def test_connection_diagnostic_survives_metadata_and_workspace_updates_but_not_alias_change(self):
        profile = self.manager.save_profile(self.payload)
        diagnostic = {"ok": True, "checked_at": "2026-01-01T00:00:00+00:00", "detail": "SSH connected"}
        self.manager._profiles[profile["id"]]["last_connection_check"] = diagnostic
        updated = self.manager.save_profile({"id": profile["id"], "label": "Renamed", "remote_workspace": "/work/new-plugin"})
        self.assertEqual(updated["last_connection_check"], diagnostic)
        updated = self.manager.save_profile({"id": profile["id"], "ssh_alias": "other_gpu"})
        self.assertIsNone(updated["last_connection_check"])

    def test_connection_diagnostic_is_invalidated_when_managed_endpoint_changes(self):
        profile = self.manager.save_profile({**self.payload, "ssh_connection": {"host": "gpu.example.test", "user": "compute", "port": 22}})
        self.manager._profiles[profile["id"]]["last_connection_check"] = {"ok": True}
        profile = self.manager.save_profile({"id": profile["id"], "ssh_connection": {"host": "other.example.test", "user": "compute", "port": 22}})
        self.assertIsNone(profile["last_connection_check"])
