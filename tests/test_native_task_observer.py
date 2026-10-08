from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from native_task_observer import NativeTaskError, NativeTaskObserver
from project_registry import ProjectContext


THREAD = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class NativeTaskObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "rollout.jsonl"
        self.now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        self.config = {"id": "native-thead", "backend": "thead", "branch": "upgrade",
                       "title": "THead adaptation", "thread_id": THREAD, "rollout_path": "rollout.jsonl"}
        self.write({"type": "session_meta", "payload": {"id": THREAD}})
        self.observer = NativeTaskObserver([self.config], self.root, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, event, *, newline=True):
        with self.path.open("ab") as stream:
            stream.write(json.dumps(event, ensure_ascii=False).encode("utf8") + (b"\n" if newline else b""))

    def event(self, kind, **fields):
        self.write({"timestamp": self.now.isoformat(), "type": "event_msg", "payload": {"type": kind, **fields}})

    def snapshot(self, **kwargs):
        return self.observer.snapshot(**kwargs)[0]

    def test_real_lifecycle_tracks_new_turns_without_marking_acceptance_done(self):
        self.event("task_started", turn_id="a")
        self.event("item_completed", thread_id=THREAD, item={"type": "AgentMessage", "phase": "commentary",
                   "content": [{"type": "Text", "text": "CPU passed; Gate remains pending."}]})
        row = self.snapshot()
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["stage"])
        self.assertFalse(row["controllable"])
        self.event("task_complete", turn_id="a", last_agent_message="Gate is still pending")
        self.assertEqual(self.snapshot()["status"], "completed")
        self.event("task_started", turn_id="b")
        self.assertEqual(self.snapshot()["status"], "running")

    def test_partial_appended_lifecycle_is_retained_until_its_newline(self):
        self.event("task_started", turn_id="a")
        self.snapshot()
        self.write({"timestamp": self.now.isoformat(), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "a"}}, newline=False)
        self.assertEqual(self.snapshot()["status"], "running")
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(self.snapshot()["status"], "completed")

    def test_identity_mismatch_makes_status_unknown_and_never_exports_foreign_text(self):
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": OTHER}}) + "\n", encoding="utf8")
        self.event("task_complete", last_agent_message="foreign secret")
        row = self.snapshot()
        self.assertEqual(row["status"], "unknown")
        self.assertNotIn("foreign secret", json.dumps(row))
        self.assertNotIn(str(self.path), json.dumps(row))

    def test_nested_thread_notifications_cannot_change_parent_state_or_message(self):
        self.event("task_started", turn_id="root")
        self.event("task_complete", turn_id="root", thread_id=OTHER, last_agent_message="child output")
        self.event("item_completed", thread_id=OTHER, item={"type": "AgentMessage", "phase": "final_answer", "content": "child output"})
        row = self.snapshot()
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["progress_summary"])

    def test_foreign_turn_completion_does_not_end_active_turn(self):
        self.event("task_started", turn_id="new")
        self.event("task_complete", turn_id="old", last_agent_message="old output")
        row = self.snapshot()
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["progress_summary"])

    def test_missing_lifecycle_stays_unknown_after_bounded_tail(self):
        self.event("task_started", turn_id="a")
        self.write({"timestamp": self.now.isoformat(), "type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "x" * 4000}})
        self.event("item_completed", item={"type": "AgentMessage", "phase": "commentary", "content": "still working"})
        self.observer.INITIAL_BYTES = 1000
        row = self.snapshot()
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["progress_summary"], "still working")

    def test_running_freshness_uses_event_time_even_when_file_mtime_is_unchanged(self):
        self.event("task_started", turn_id="a")
        self.snapshot()
        self.now += timedelta(minutes=31)
        self.assertTrue(self.snapshot()["stale"])
        self.event("item_completed", item={"type": "CommandExecution", "command": "private-command"})
        self.assertFalse(self.snapshot()["stale"])
        self.event("task_complete", turn_id="a")
        self.now += timedelta(days=2)
        self.assertFalse(self.snapshot()["stale"])

    def test_public_projection_strips_messages_plans_paths_and_thread_identity(self):
        self.event("task_started", turn_id="a")
        self.event("item_completed", item={"type": "AgentMessage", "phase": "commentary", "content": "private C:/secret 192.0.2.4"})
        self.write({"timestamp": self.now.isoformat(), "type": "response_item", "payload": {"type": "function_call", "name": "functions.update_plan",
                   "arguments": json.dumps({"plan": [{"step": "Gate private verification", "status": "in_progress"}]})}})
        public = self.snapshot(read_only=True)
        self.assertEqual(set(public), set(NativeTaskObserver.PUBLIC_FIELDS))
        self.assertEqual(public["stage"], "gate")
        for forbidden in ("private", THREAD, "rollout", "plan", "progress_summary", "controllable"):
            self.assertNotIn(forbidden, json.dumps(public))
        self.assertIn("private", self.snapshot()["progress_summary"])

    def test_arbitrary_tool_results_and_reasoning_are_never_projected(self):
        for kind in ("custom_tool_call_output", "function_call_output", "reasoning", "agent_message"):
            self.write({"timestamp": self.now.isoformat(), "type": "response_item", "payload": {"type": kind, "output": "private secret", "content": "private secret"}})
        self.assertIsNone(self.snapshot()["progress_summary"])

    def test_truncated_file_resets_cached_lifecycle(self):
        self.event("task_started", turn_id="a")
        self.event("task_complete", turn_id="a")
        self.assertEqual(self.snapshot()["status"], "completed")
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": THREAD}}) + "\n", encoding="utf8")
        self.assertEqual(self.snapshot()["status"], "unknown")

    def test_bad_configuration_is_rejected_before_reading_any_log(self):
        for changed in ({"id": "../unsafe"}, {"thread_id": "not-a-thread"}, {"rollout_path": "secrets.txt"}, {"backend": "bad\nbackend"}):
            with self.subTest(changed=changed), self.assertRaises(NativeTaskError):
                NativeTaskObserver([{**self.config, **changed}], self.root)
        with self.assertRaises(NativeTaskError):
            NativeTaskObserver([self.config, self.config], self.root)

    def test_native_tasks_replace_overview_source_without_mutating_control_sessions(self):
        self.event("task_started", turn_id="a")
        project = ProjectContext("vllm", "vLLM", native_tasks=self.observer)
        old = {"id": "a" * 32, "backend": "thead", "branch": "upgrade", "status": "failed"}
        current = {"id": "b" * 32, "backend": "cuda", "branch": "upgrade", "status": "running"}
        rows = project.adaptation_tasks(session_rows=[old, current])
        self.assertEqual([row["id"] for row in rows], ["native-thead", "b" * 32, "a" * 32])
        self.assertTrue(rows[-1]["historical"])
        self.assertFalse(rows[0]["controllable"])
        self.assertNotIn("historical", old)
        self.assertEqual(len(project.adaptation_tasks(read_only=True)), 1)


if __name__ == "__main__":
    unittest.main()
