import json
import os
from pathlib import Path
import time
from unittest import TestCase, mock

from fl_dashboard.agents.codex_sessions import atomic_json
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

    def test_oversized_artifact_warns_and_valid_progress_recovers(self):
        session = self.manager.create(self.payload)
        self.start(session)
        self.manager._sessions[session["id"]]["error"] = "Existing execution diagnostic"
        provenance = {"branch": session["branch"], "candidate_commit": session["commit"],
                      "remote_task_workspace": "/work/task", "observed_at": "2000-01-01T00:00:00Z",
                      "evidence_paths": [{"path": "README.md"}]}
        file = self.write_progress(session, summary="Last valid progress", provenance=provenance)
        original = self.manager.snapshot()["sessions"][0]
        self.write_progress(session, summary="Unloaded recent progress", history="x" * 70000,
                            provenance={**provenance, "candidate_commit": "b" * 40,
                                        "observed_at": "2000-01-02T00:00:00Z"})
        self.assertGreater(file.stat().st_size, 65536)
        rejected = self.manager.snapshot()["sessions"][0]
        self.assertEqual(rejected["progress_warning"], {"code": "artifact_too_large"})
        for key in ("plan", "progress_summary", "plan_source", "plan_updated_at",
                    "reported_provenance", "status", "error", "turn_id", "thread_id"):
            self.assertEqual(rejected[key], original[key], key)
        self.assertNotIn("gate_passed", rejected)
        stored_file = self.manager.root / session["id"] / "session.json"
        stored = json.loads(stored_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["progress_warning"], rejected["progress_warning"])
        repeated = self.manager.snapshot()["sessions"][0]
        self.assertEqual(repeated["progress_warning"], rejected["progress_warning"])
        self.assertEqual(repeated["updated_at"], rejected["updated_at"])
        self.write_progress(session, summary="Updated valid progress", provenance=provenance)
        next_time = time.time() + 2
        os.utime(file, (next_time, next_time))
        recovered = self.manager.snapshot()["sessions"][0]
        self.assertNotIn("progress_warning", recovered)
        self.assertEqual(recovered["progress_summary"], "Updated valid progress")
        self.assertNotEqual(recovered["plan_updated_at"], original["plan_updated_at"])
        for key in ("reported_provenance", "status", "error", "turn_id", "thread_id"):
            self.assertEqual(recovered[key], original[key], key)
        self.assertNotIn("progress_warning", json.loads(stored_file.read_text(encoding="utf-8")))

    def test_optional_missing_progress_is_quiet_but_loaded_missing_progress_warns(self):
        session = self.manager.create(self.payload)
        self.assertNotIn("progress_warning", self.manager.snapshot()["sessions"][0])
        file = self.write_progress(session)
        original = self.manager.snapshot()["sessions"][0]
        file.unlink()
        missing = self.manager.snapshot()["sessions"][0]
        self.assertEqual(missing["progress_warning"], {"code": "artifact_unavailable"})
        for key in ("plan", "progress_summary", "plan_source", "plan_updated_at", "status", "error"):
            self.assertEqual(missing[key], original[key], key)
        self.write_progress(session)
        self.assertNotIn("progress_warning", self.manager.snapshot()["sessions"][0])

    def test_malformed_plan_cannot_refresh_provenance_before_valid_recovery(self):
        session = self.manager.create(self.payload)
        self.start(session)
        provenance = {"branch": session["branch"], "candidate_commit": session["commit"],
                      "remote_task_workspace": "/work/task", "observed_at": "2000-01-01T00:00:00Z",
                      "evidence_paths": [{"path": "README.md"}]}
        file = self.write_progress(session, provenance=provenance)
        original = self.manager.snapshot()["sessions"][0]
        newer_provenance = {**provenance, "candidate_commit": "b" * 40,
                            "observed_at": "2000-01-02T00:00:00Z"}
        self.write_progress(session, plan=[{"step": "Adapt backend", "status": "invalid"}],
                            summary="Rejected progress", provenance=newer_provenance)
        rejected = self.manager.snapshot()["sessions"][0]
        self.assertEqual(rejected["progress_warning"], {"code": "artifact_invalid"})
        for key in ("reported_provenance", "plan", "progress_summary", "plan_updated_at",
                    "plan_source", "status", "error", "thread_id", "turn_id"):
            self.assertEqual(rejected[key], original[key], key)
        self.write_progress(session, plan=[{"step": "Adapt backend", "status": "in_progress"}],
                            summary="Valid progress", provenance=newer_provenance)
        next_time = time.time() + 2
        os.utime(file, (next_time, next_time))
        recovered = self.manager.snapshot()["sessions"][0]
        self.assertNotIn("progress_warning", recovered)
        self.assertEqual(recovered["reported_provenance"]["candidate_commit"], newer_provenance["candidate_commit"])
        self.assertEqual(recovered["reported_provenance"]["observed_at"], newer_provenance["observed_at"])
        self.assertEqual(recovered["progress_summary"], "Valid progress")
        self.assertNotEqual(recovered["plan_updated_at"], original["plan_updated_at"])
        for key in ("status", "error", "thread_id", "turn_id"):
            self.assertEqual(recovered[key], original[key], key)

    def test_resolved_file_outside_worktree_is_rejected_without_opening_it(self):
        session = self.manager.create(self.payload)
        self.write_progress(session)
        outside = self.base / "outside-progress.json"
        outside.write_text(json.dumps(self.artifact(session)), encoding="utf-8")
        original_resolve = Path.resolve
        original_open = Path.open

        def resolve(path, *args, **kwargs):
            # Simulate a symlink target without requiring Windows symlink
            # privileges. The actual outside file must never be opened.
            if path.name == ".flflow-progress.json":
                return outside
            return original_resolve(path, *args, **kwargs)

        def open_file(path, *args, **kwargs):
            if path == outside:
                raise AssertionError("outside file read")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", resolve), mock.patch.object(Path, "open", open_file):
            state = self.manager.snapshot()["sessions"][0]
        self.assertEqual(state["plan"], [])
        self.assertEqual(state["progress_warning"], {"code": "artifact_unsafe_path"})

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
