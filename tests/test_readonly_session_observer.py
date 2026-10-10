from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from project_registry import ProjectContext
from readonly_session_observer import ReadonlySessionObserver


class ReadonlySessionObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        self.codex, self.claude = self.root / "old codex", self.root / "new claude"
        self.flagscale = self.root / "independent flagscale"
        self.observer = ReadonlySessionObserver([
            {"provider": "codex", "directory": self.codex}, {"provider": "claude", "directory": self.claude},
            {"provider": "flagscale", "directory": self.flagscale}],
            clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, provider="codex", identity="a" * 32, **changes):
        directory = {"codex":self.codex,"claude":self.claude,"flagscale":self.flagscale}[provider]
        path = directory / identity / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"id": identity, "status": "running", "backend": "thead", "branch": "upgrade",
                  "updated_at": self.now.isoformat(), "provider": provider,
                  "thread_id": "private-controller", "goal": "private goal", "api_key": "explicit_dummy_key",
                  "last_agent_message": "private output", "events": [{"stderr": "private raw result"}],
                  "worktree": "C:/private/path", "plan": [{"step": "private plan", "status": "completed"}],
                  **changes}
        path.write_text(json.dumps(record), encoding="utf8")
        return path

    def test_missing_stores_are_not_created_and_future_tasks_are_discovered(self):
        self.assertEqual(self.observer.snapshot(), [])
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.claude.exists())
        self.assertFalse(self.flagscale.exists())
        self.save("claude", "b" * 32)
        self.assertEqual(self.observer.snapshot()[0]["provider"], "claude")

    def test_safe_row_has_hash_id_and_never_returns_private_task_payload(self):
        self.save()
        row = self.observer.snapshot()[0]
        self.assertEqual(set(row), ReadonlySessionObserver.PUBLIC_FIELDS)
        self.assertFalse(row["controllable"])
        self.assertFalse(row["historical"])
        self.assertIsNone(row["stage"])
        self.assertTrue(row["id"].startswith("task-"))
        for secret in ("a" * 32, "private", "api_key", "events", "plan", "worktree", str(self.root)):
            self.assertNotIn(secret, json.dumps(row))

    def test_changed_status_and_new_provider_task_appear_with_stable_public_identity(self):
        path = self.save()
        first = self.observer.snapshot()[0]
        self.now += timedelta(minutes=1)
        self.save(status="failed")
        self.save("claude", "b" * 32, backend="musa", status="waiting_input")
        rows = self.observer.snapshot()
        codex = next(row for row in rows if row["provider"] == "codex")
        self.assertEqual(codex["id"], first["id"])
        self.assertEqual(codex["status"], "failed")
        self.assertEqual(len(rows), 2)
        path.unlink()
        self.assertEqual(len(self.observer.snapshot()), 1)

    def test_corrupt_or_mismatched_schema_is_not_published(self):
        cases = ({"id": "b" * 32}, {"provider": "claude"}, {"status": "Gate passed"}, {"backend": "192.0.2.1"},
                 {"branch": "bad\nbranch"}, {"updated_at": "2026-01-02T12:00:00"})
        for changed in cases:
            with self.subTest(changed=changed):
                path = self.save()
                record = json.loads(path.read_text(encoding="utf8"))
                record.update(changed)
                path.write_text(json.dumps(record), encoding="utf8")
                self.assertEqual(self.observer.snapshot(), [])
        path = self.save()
        path.write_text("{incomplete", encoding="utf8")
        self.assertEqual(self.observer.snapshot(), [])

    def test_legacy_codex_missing_provider_remains_codex(self):
        path = self.save()
        record = json.loads(path.read_text(encoding="utf8"))
        del record["provider"]
        path.write_text(json.dumps(record), encoding="utf8")
        self.assertEqual(self.observer.snapshot()[0]["provider"], "codex")

    def test_iluvatar_human_title_uses_requested_vendor_name(self):
        self.save(backend="corex")
        row = self.observer.snapshot()[0]
        self.assertEqual(row["backend"], "corex")
        self.assertTrue(row["title"].startswith("iluvatar"))
        self.assertNotIn("COREX", row["title"])

    def test_flagscale_is_safe_metadata_only_and_has_no_native_controller_identity(self):
        self.save("flagscale", "c"*32, backend="cuda", status="waiting_input",
                  flagscale_session_id="1234abcd", flagscale_result={"text":"private output"},
                  agent_settings={"api_key":"dummy-flagscale-key"}, follow_up_mode="fresh_turn_with_context")
        row=self.observer.snapshot()[0]
        self.assertEqual(row["provider"],"flagscale")
        self.assertEqual(row["title"],"NVIDIA · FlagScale Agent 适配")
        self.assertEqual(row["status"],"waiting_input")
        self.assertEqual(set(row),ReadonlySessionObserver.PUBLIC_FIELDS)
        self.assertFalse(row["controllable"])
        for secret in ("c"*32,"1234abcd","private","dummy-flagscale-key","agent_settings","flagscale_result",str(self.root)):
            self.assertNotIn(secret,json.dumps(row))
        self.now+=timedelta(minutes=31)
        self.assertTrue(self.observer.snapshot()[0]["stale"])

    def test_flagscale_tasks_stay_current_alongside_native_codex_history(self):
        self.save("flagscale","c"*32)
        native=Mock(snapshot=Mock(return_value=[{"id":"native-thead","backend":"thead","branch":"upgrade"}]))
        project=ProjectContext("vllm","vLLM",native_tasks=native,readonly_sessions=self.observer)
        rows=project.adaptation_tasks(read_only=True)
        self.assertEqual(rows[1]["provider"],"flagscale")
        self.assertFalse(rows[1]["historical"])

    def test_size_bound_rejects_large_file_instead_of_publishing_cached_old_status(self):
        path = self.save()
        self.assertEqual(len(self.observer.snapshot()), 1)
        self.observer.MAX_FILE_BYTES = 128
        self.assertEqual(self.observer.snapshot(), [])
        self.assertTrue(path.exists())

    def test_running_freshness_uses_persisted_timestamp_and_terminal_state_stays_terminal(self):
        self.save()
        self.assertFalse(self.observer.snapshot()[0]["stale"])
        self.now += timedelta(minutes=31)
        self.assertTrue(self.observer.snapshot()[0]["stale"])
        self.save(status="completed")
        self.now += timedelta(days=1)
        self.assertFalse(self.observer.snapshot()[0]["stale"])

    def test_readonly_project_replaces_old_codex_but_keeps_new_claude_current(self):
        self.save()
        self.save("claude", "b" * 32)
        native = Mock(snapshot=Mock(return_value=[{"id": "native-thead", "backend": "thead", "branch": "upgrade"}]))
        local = Mock(snapshot=Mock(side_effect=AssertionError("No executor allowed")))
        project = ProjectContext("vllm", "vLLM", native_tasks=native, readonly_sessions=self.observer, sessions=local)
        rows = project.adaptation_tasks(read_only=True)
        self.assertEqual(rows[0]["id"], "native-thead")
        self.assertEqual(rows[1]["provider"], "claude")
        self.assertFalse(rows[1]["historical"])
        self.assertEqual(rows[2]["provider"], "codex")
        self.assertTrue(rows[2]["historical"])
        local.snapshot.assert_not_called()
        self.assertTrue(ProjectContext("vllm", "vLLM", readonly_sessions=self.observer).metadata(read_only=True)["adaptation_tasks"])


if __name__ == "__main__": unittest.main()
