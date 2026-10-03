import copy
from http.client import HTTPConnection
import json
import stat
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from codex_sessions import CodexSessionManager, SessionError
from flflow import make_server


class EventHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.manager = CodexSessionManager({"repo": str(self.base), "codex_executable": "fake-codex"}, self.base)
        self.sid = "a" * 32
        self.session = {"id": self.sid, "backend": "cuda", "branch": "baseline-example", "thread_id": "root-example",
                        "status": "running", "events": [], "pending_requests": [], "plan": [],
                        "reported_provenance": {"observed_at": "2000-01-01T00:00:00Z"}, "gate": {"status": "missing"}}
        self.manager._sessions[self.sid] = self.session
        self.path = self.manager.root / self.sid / "events.jsonl"
        self.path.parent.mkdir()

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def record(self, index, text="output"):
        return {"at": "2000-01-01T00:00:00Z", "method": "item/commandExecution/outputDelta",
                "source_scope": "root", "source_thread_id": "root-example",
                "params": {"threadId": "root-example", "itemId": str(index), "delta": text}}

    def write(self, records):
        self.path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")

    def test_completion_survives_more_than_80_deltas_and_history_does_not_mutate_acceptance(self):
        item = {"id": "exact-command", "type": "commandExecution", "status": "completed", "exitCode": 0}
        self.manager._event(self.sid, {"method": "item/completed", "params": {"threadId": "root-example", "turnId": "turn-example", "item": item}})
        for index in range(100):
            self.manager._event(self.sid, {"method": "item/commandExecution/outputDelta", "params": self.record(index)["params"]})
        self.assertEqual(len(self.session["events"]), 80)
        self.assertFalse(any(event["method"] == "item/completed" for event in self.session["events"]))
        before = copy.deepcopy(self.session)
        page = self.manager.event_history(self.sid)
        self.assertEqual(len(page["events"]), 80)
        self.assertTrue(page["has_more"])
        older = self.manager.event_history(self.sid, page["next_cursor"])
        completion = older["events"][0]
        self.assertEqual(completion["params"]["item"], item)
        self.assertEqual(completion["source_thread_id"], "root-example")
        self.assertEqual(completion["source_scope"], "root")
        self.assertEqual(completion["params"]["turnId"], "turn-example")
        self.assertFalse(older["has_more"])
        self.assertEqual(self.session, before, "a history read cannot refresh lifecycle, provenance or acceptance")

    def test_pages_are_chronological_nonoverlapping_and_survive_appends(self):
        self.write([self.record(index) for index in range(15)])
        page = self.manager.event_history(self.sid, limit=5)
        self.assertEqual([r["params"]["itemId"] for r in page["events"]], [str(i) for i in range(10, 15)])
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(self.record(15)) + "\n")
        older = self.manager.event_history(self.sid, page["next_cursor"], limit=5)
        self.assertEqual([r["params"]["itemId"] for r in older["events"]], [str(i) for i in range(5, 10)])
        oldest = self.manager.event_history(self.sid, older["next_cursor"], limit=5)
        self.assertEqual([r["params"]["itemId"] for r in oldest["events"]], [str(i) for i in range(5)])
        self.assertFalse(oldest["has_more"])

    def test_cursor_rejects_tampering_other_session_and_truncated_log(self):
        self.write([self.record(index) for index in range(5)])
        cursor = self.manager.event_history(self.sid, limit=1)["next_cursor"]
        self.manager._sessions["b" * 32] = {"id": "b" * 32}
        for bad_sid, bad_cursor in ((self.sid, "../private"), (self.sid, cursor[:-1] + ("0" if cursor[-1] != "0" else "1")),
                                    ("b" * 32, cursor)):
            with self.subTest(session=bad_sid), self.assertRaises(SessionError):
                self.manager.event_history(bad_sid, bad_cursor)
        self.path.write_bytes(b"")
        with self.assertRaises(SessionError):
            self.manager.event_history(self.sid, cursor)
        for bad_limit in (0, 81, -1, True, "80"):
            with self.subTest(limit=bad_limit), self.assertRaises(SessionError):
                self.manager.event_history(self.sid, limit=bad_limit)
        with self.assertRaises(SessionError):
            self.manager.event_history("../private")

    def test_byte_and_response_caps_do_not_skip_a_normal_record_at_window_boundary(self):
        self.write([self.record(index, "\u4e2d" * 10000) for index in range(20)])
        cursor, seen = None, []
        while True:
            page = self.manager.event_history(self.sid, cursor)
            self.assertLessEqual(page["bytes_read"], self.manager.HISTORY_BYTE_LIMIT)
            self.assertLessEqual(len(json.dumps(page, indent=2).encode("utf-8")), self.manager.HISTORY_BYTE_LIMIT)
            self.assertLessEqual(len(page["events"]), 80)
            seen = [r["params"]["itemId"] for r in page["events"]] + seen
            if not page["has_more"]:
                break
            self.assertNotEqual(cursor, page["next_cursor"])
            cursor = page["next_cursor"]
        self.assertEqual(seen, [str(index) for index in range(20)])

    def test_missing_corrupt_incomplete_and_oversized_records_are_explicit(self):
        missing = self.manager.event_history(self.sid)
        self.assertEqual(missing["status"], "unavailable")
        self.assertEqual(missing["warnings"], ["history_missing"])
        self.path.write_bytes((json.dumps(self.record(0)) + "\nnot-json\n" + json.dumps(self.record(1)) + "\n{unfinished").encode())
        page = self.manager.event_history(self.sid)
        self.assertEqual([r["params"]["itemId"] for r in page["events"]], ["0", "1"])
        self.assertEqual(set(page["warnings"]), {"corrupt_records", "incomplete_record"})
        self.assertEqual(page["status"], "partial")
        self.write([self.record(0), self.record(1, "x" * (self.manager.HISTORY_BYTE_LIMIT + 10000))])
        huge = self.manager.event_history(self.sid)
        self.assertEqual(huge["warnings"], ["record_exceeds_window"])
        self.assertTrue(huge["has_more"])
        older = self.manager.event_history(self.sid, huge["next_cursor"])
        self.assertEqual(older["events"][0]["params"]["itemId"], "0")
        self.assertIn("incomplete_record", older["warnings"])

    def test_corrupt_record_scan_work_is_bounded_and_oversized_response_is_reported(self):
        self.path.write_bytes(b"bad\n" * 1000)
        page = self.manager.event_history(self.sid)
        self.assertEqual(page["records_scanned"], self.manager.HISTORY_SCAN_LIMIT)
        self.assertTrue(page["has_more"])
        self.write([self.record(0, "\u4e2d" * 50000)])
        page = self.manager.event_history(self.sid)
        self.assertEqual(page["events"], [])
        self.assertIn("record_exceeds_response_limit", page["warnings"])
        self.assertLessEqual(len(json.dumps(page, indent=2).encode("utf-8")), self.manager.HISTORY_BYTE_LIMIT)

    def test_duplicate_identity_fields_and_nonfinite_json_are_corrupt_not_reinterpreted(self):
        good = json.dumps(self.record(0))
        duplicate_outer = good[:-1] + ',"params":{"itemId":"changed-identity"}}'
        duplicate_nested = good.replace('"itemId": "0"', '"itemId":"original-identity","itemId":"changed-identity"')
        numbers = [good[:-1] + ',"untrusted":' + number + '}' for number in ('NaN', 'Infinity', '-Infinity', '1e999')]
        self.path.write_text("\n".join([good, duplicate_outer, duplicate_nested, *numbers]) + "\n", encoding="utf-8")
        page = self.manager.event_history(self.sid)
        self.assertEqual([r["params"]["itemId"] for r in page["events"]], ["0"])
        self.assertEqual(page["warnings"], ["corrupt_records"])
        self.assertEqual(page["records_scanned"], 7)
        self.assertFalse(page["has_more"])

    def test_nonregular_open_file_is_unavailable_before_any_read(self):
        self.write([self.record(0)])
        actual = self.path.stat()
        metadata = mock.Mock(st_mode=stat.S_IFIFO, st_dev=actual.st_dev, st_ino=actual.st_ino, st_size=actual.st_size)
        with mock.patch("codex_sessions.os.fstat", return_value=metadata):
            page = self.manager.event_history(self.sid)
        self.assertEqual(page["status"], "unavailable")
        self.assertEqual(page["warnings"], ["history_unavailable"])
        self.assertEqual(page["bytes_read"], 0)

    def test_http_route_validates_query_and_exposes_only_readonly_history(self):
        self.write([self.record(0)])
        server = make_server(object(), "127.0.0.1", 0, sessions=self.manager)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for suffix, expected in (("?limit=1", 200), ("?limit=81", 400), ("?limit=1&limit=2", 400),
                                     ("?path=private", 400), ("?cursor=", 400)):
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", f"/api/codex-sessions/{self.sid}/events" + suffix)
                response = connection.getresponse()
                body = json.loads(response.read())
                self.assertEqual(response.status, expected, body)
                if expected == 200:
                    self.assertEqual(body["session_id"], self.sid)
                    self.assertEqual(body["events"][0]["params"]["itemId"], "0")
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)


if __name__ == "__main__":
    unittest.main()
