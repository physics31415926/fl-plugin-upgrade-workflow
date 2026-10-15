from collections import deque
from concurrent.futures import Future
import errno
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest import TestCase, mock

from fl_dashboard.agents.codex_sessions import AppServerClient, SessionError, atomic_json
from tests import test_codex_sessions as fixtures


def windows_lock(code=5):
    error = PermissionError(errno.EACCES, "private-path-and-command-must-not-appear")
    error.winerror = code
    return error


class AtomicSaveTest(TestCase):
    def test_windows_lock_retry_retains_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_json(path, {"old": True})
            original_replace = __import__("os").replace
            attempts = []

            def replace(source, target):
                attempts.append(1)
                # The old state remains intact until replacement succeeds.
                self.assertEqual(json.loads(path.read_text()), {"old": True})
                if len(attempts) < 3:
                    raise windows_lock(5 if len(attempts) == 1 else 32)
                original_replace(source, target)

            with mock.patch("fl_dashboard.agents.codex_sessions.os.replace", side_effect=replace), mock.patch("fl_dashboard.agents.codex_sessions.time.sleep") as sleep:
                atomic_json(path, {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"new": True})
            self.assertEqual(len(attempts), 3)
            self.assertEqual(sleep.call_args_list, [mock.call(0.025), mock.call(0.05)])
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_persistent_lock_is_not_ignored_and_other_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_json(path, {"old": True})
            for error, count in ((windows_lock(33), 4), (OSError(errno.ENOSPC, "full"), 1)):
                with self.subTest(error=type(error).__name__), mock.patch("fl_dashboard.agents.codex_sessions.os.replace", side_effect=error) as replace, mock.patch("fl_dashboard.agents.codex_sessions.time.sleep") as sleep:
                    with self.assertRaises(OSError):
                        atomic_json(path, {"new": True})
                self.assertEqual(replace.call_count, count)
                self.assertEqual(sleep.call_count, count - 1)
                self.assertEqual(json.loads(path.read_text()), {"old": True})
                self.assertEqual(list(Path(directory).glob("*.tmp")), [])


class ReaderTest(TestCase):
    def client(self, frames, callback, exit_code=None):
        client = AppServerClient.__new__(AppServerClient)
        client.callback = callback
        client._lock = threading.Lock()
        client._closed = False
        client._reader_stopped = False
        client.last_callback_error = None
        client.stderr = deque(maxlen=20)
        client._pending = {1: Future()}
        client.process = mock.Mock(stdout=frames)
        client.process.poll.return_value = exit_code
        return client

    def test_callback_persistence_failure_cannot_prevent_following_response(self):
        records = []

        def callback(message):
            if message["method"] == "item/commandExecution/outputDelta":
                raise windows_lock()
            records.append(message)

        frames = io.StringIO(json.dumps({"method": "item/commandExecution/outputDelta", "params": {"delta": "output"}}) + "\n" +
                             json.dumps({"id": 1, "result": {"turn": {"id": "actual-turn"}}}) + "\n")
        client = self.client(frames, callback)
        with self.assertLogs("fl_dashboard.agents.codex_sessions", level="WARNING") as logs:
            client._read()
        self.assertEqual(client._pending[1].result(), {"turn": {"id": "actual-turn"}})
        self.assertEqual(records[0]["method"], "connection/callbackError")
        self.assertEqual(client.last_callback_error["winerror"], 5)
        self.assertNotIn("private-path", json.dumps(records) + " ".join(logs.output))
        self.assertEqual(records[-1]["params"]["reason"], "stdout_closed")
        self.assertNotIn("exited", records[-1]["params"]["detail"])

    def test_even_failed_diagnostic_callback_keeps_protocol_responses_alive(self):
        client = self.client(io.StringIO('{"method":"notification"}\n{"id":1,"result":{"ok":true}}\n'),
                             mock.Mock(side_effect=windows_lock()))
        with self.assertLogs("fl_dashboard.agents.codex_sessions", level="WARNING"):
            client._read()
        self.assertEqual(client._pending[1].result(), {"ok": True})
        self.assertTrue(client.stderr)

    def test_reader_failure_and_real_process_exit_have_distinct_reasons(self):
        class BrokenStream:
            def __iter__(self):
                raise OSError("private-path-and-command-must-not-appear")

        for stream, code, reason in ((BrokenStream(), None, "reader_error"), (io.StringIO(""), 7, "process_exit")):
            with self.subTest(reason=reason):
                records = []
                client = self.client(stream, records.append, code)
                client._read()
                self.assertEqual(records[-1]["params"]["reason"], reason)
                with self.assertRaises(SessionError) as failure:
                    client._pending[1].result()
                self.assertNotIn("private-path", str(failure.exception))
                self.assertTrue(client._reader_stopped)
                if reason == "reader_error":
                    self.assertNotIn("exited", str(failure.exception))
                else:
                    self.assertEqual(records[-1]["params"]["exit_code"], 7)


class ManualTimer:
    instances = []

    def __init__(self, delay, function, args=()):
        self.delay, self.function, self.args = delay, function, args
        self.cancelled = False
        self.__class__.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # Also permits simulating a callback already waiting on the manager lock
        # when cancellation occurs.
        self.function(*self.args)


class EventPersistenceTest(TestCase):
    setUp = fixtures.SessionsTest.setUp
    tearDown = fixtures.SessionsTest.tearDown
    git = fixtures.SessionsTest.git
    start = fixtures.SessionsTest.start

    def session(self):
        session = self.manager.create(self.payload)
        self.start(session)
        self.manager._saved_at[session["id"]] = 100
        return session

    def event(self, session, method="item/commandExecution/outputDelta", **params):
        self.manager._event(session["id"], {"method": method, "params": {"threadId": "thread-123", **params}})

    def stored(self, session):
        return json.loads((self.manager.root / session["id"] / "session.json").read_text(encoding="utf-8"))

    def test_output_burst_is_throttled_and_quiet_final_frame_is_saved(self):
        session = self.session()
        ManualTimer.instances.clear()
        with mock.patch("fl_dashboard.agents.codex_sessions.threading.Timer", ManualTimer), mock.patch("fl_dashboard.agents.codex_sessions.time.monotonic", return_value=100.1), mock.patch("fl_dashboard.agents.codex_sessions.atomic_json", wraps=atomic_json) as save:
            for index in range(500):
                self.event(session, delta="frame-" + str(index))
            self.assertEqual(save.call_count, 0)
            self.assertEqual(len(ManualTimer.instances), 1)
            self.assertAlmostEqual(ManualTimer.instances[0].delay, 0.9)
            self.assertNotEqual(self.stored(session)["events"][-1]["params"].get("delta"), "frame-499")
            ManualTimer.instances[0].fire()
            self.assertEqual(save.call_count, 1)
        state = self.stored(session)
        self.assertEqual(state["events"][-1]["params"]["delta"], "frame-499")
        self.assertEqual(state["status"], "running")
        self.assertFalse(self.manager._save_timers)
        self.assertFalse(self.manager._dirty_sessions)

    def test_failed_approval_save_is_visible_and_retries_without_approval_or_status_loss(self):
        session = self.session()
        original = self.stored(session)
        with mock.patch("fl_dashboard.agents.codex_sessions.threading.Timer", ManualTimer), mock.patch("fl_dashboard.agents.codex_sessions.atomic_json", side_effect=windows_lock()), self.assertLogs("fl_dashboard.agents.codex_sessions", level="WARNING"):
            self.manager._event(session["id"], {"id": 9, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-123", "command": "private-command"}})
            state = self.manager._copy(session["id"])
            self.assertEqual(state["status"], "waiting_approval")
            self.assertEqual([request["id"] for request in state["pending_requests"]], [9])
            self.assertEqual(state["persistence_error"]["winerror"], 5)
            self.assertNotIn("private", json.dumps(state["persistence_error"]))
            self.assertEqual(self.stored(session), original)
            timer = self.manager._save_timers[session["id"]]
            # A second failure remains observable and schedules a new attempt.
            timer.fire()
            self.assertEqual(self.manager._copy(session["id"])["persistence_error"]["attempts"], 2)
        self.manager._save_timers[session["id"]].fire()
        stored = self.stored(session)
        self.assertEqual(stored["status"], "waiting_approval")
        self.assertEqual([request["id"] for request in stored["pending_requests"]], [9])
        self.assertNotIn("persistence_error", stored)
        self.assertFalse(fixtures.FakeClient.instances[-1].responses)

    def test_event_log_failure_retains_approval_and_recovers_final_record(self):
        session = self.session()
        original_open = Path.open

        def open_file(path, *args, **kwargs):
            if path.name == "events.jsonl":
                raise windows_lock()
            return original_open(path, *args, **kwargs)

        with mock.patch("fl_dashboard.agents.codex_sessions.threading.Timer", ManualTimer), mock.patch.object(Path, "open", open_file), self.assertLogs("fl_dashboard.agents.codex_sessions", level="WARNING"):
            self.manager._event(session["id"], {"id": 3, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-123", "command": "read-only"}})
            state = self.stored(session)
            self.assertEqual(state["status"], "waiting_approval")
            self.assertEqual(state["pending_requests"][0]["id"], 3)
            self.assertIn("event_log_error", state)
        self.manager._save_timers[session["id"]].fire()
        self.assertNotIn("event_log_error", self.stored(session))
        records = [json.loads(line) for line in (self.manager.root / session["id"] / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["method"], "item/commandExecution/requestApproval")
        self.assertFalse(fixtures.FakeClient.instances[-1].responses)

    def test_completion_is_immediate_and_cancelled_tail_timer_cannot_replace_new_timer(self):
        session = self.session()
        with mock.patch("fl_dashboard.agents.codex_sessions.threading.Timer", ManualTimer), mock.patch("fl_dashboard.agents.codex_sessions.time.monotonic", return_value=100.1):
            self.event(session, delta="first")
            old_timer = self.manager._save_timers[session["id"]]
            self.event(session, "turn/completed", turn={"status": "completed"})
            self.assertEqual(self.stored(session)["status"], "completed")
            self.assertTrue(old_timer.cancelled)
            self.event(session, delta="final")
            new_timer = self.manager._save_timers[session["id"]]
            old_timer.fire()
            self.assertIs(self.manager._save_timers[session["id"]], new_timer)
            new_timer.fire()
        self.assertEqual(self.stored(session)["events"][-1]["params"]["delta"], "final")

    def test_close_cancels_timers_and_flushes_last_output(self):
        session = self.session()
        with mock.patch("fl_dashboard.agents.codex_sessions.threading.Timer", ManualTimer), mock.patch("fl_dashboard.agents.codex_sessions.time.monotonic", return_value=100.1):
            self.event(session, delta="last-output")
            timer = self.manager._save_timers[session["id"]]
            self.manager.close()
        self.assertTrue(timer.cancelled)
        self.assertFalse(self.manager._save_timers)
        self.assertEqual(self.stored(session)["events"][-1]["params"]["delta"], "last-output")

    def test_real_timer_saves_quiet_tail_without_another_protocol_event(self):
        session = self.session()
        self.manager._saved_at[session["id"]] = time.monotonic() - 0.95
        saved = threading.Event()

        def save(path, value):
            atomic_json(path, value)
            if value["events"][-1]["params"].get("delta") == "quiet-tail":
                saved.set()

        with mock.patch("fl_dashboard.agents.codex_sessions.atomic_json", side_effect=save):
            self.event(session, delta="quiet-tail")
            self.assertTrue(saved.wait(3), "tail timer did not persist the quiet final frame")
            with self.manager._lock:
                self.assertFalse(self.manager._dirty_sessions)
        self.assertEqual(self.stored(session)["events"][-1]["params"]["delta"], "quiet-tail")
