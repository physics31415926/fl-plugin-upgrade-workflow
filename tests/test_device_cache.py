import copy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fl_dashboard.resources.device_cache import DeviceTelemetryCache


def item(identity="device-1", project="vllm", source="jumpserver"):
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()
    key = hashlib.sha256((project + ":" + fingerprint).encode()).hexdigest()
    row = {"fingerprint": fingerprint, "project_id": project, "source_type": source,
           "id": identity, "ssh_alias": identity, "label": "A100 测试设备", "backend": "cuda", "backend_label": "NVIDIA",
           "updated_at": "2026-10-22T01:02:03Z",
           "view": {"reachable": True, "verdict": "free", "supported": True, "stale": False,
                    "backend": "cuda", "backend_label": "NVIDIA", "error": None,
                    "accel": {"family": "nvidia", "label": "NVIDIA", "card_name": "NVIDIA A100", "total": 1,
                              "free": 1, "busy": 0, "unknown": 0, "residual": 0, "physical_cards": 1,
                              "cards": [{"index": 0, "name": "A100", "card": None, "util": 1.0,
                                         "mem_used": 1024, "mem_total": 40960, "temp": 40, "power": 90,
                                         "busy": False, "state": "free", "residual": False, "procs": []}]},
                    "node": {"cpu_pct": 30.5, "load_per_cpu": 0.2, "mem_pct": 25,
                             "mem_total_mib": 65536, "mem_avail_mib": 49152, "uptime_s": 10000}}}
    return key, row


class DeviceTelemetryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime" / "device-resource-cache.json"
        self.cache = DeviceTelemetryCache(self.path)

    def test_round_trip_keeps_metrics_and_marks_every_availability_unknown(self):
        key, row = item()
        self.assertTrue(self.cache.save({key: row}))
        loaded = DeviceTelemetryCache(self.path).load()[key]
        self.assertEqual(loaded["updated_at"], "2026-10-22T01:02:03+00:00")
        self.assertEqual(loaded["backend_label"], "NVIDIA")
        self.assertEqual(loaded["source_type"], "jumpserver")
        self.assertEqual(loaded["fingerprint"], row["fingerprint"])
        view = loaded["view"]
        self.assertTrue(view["stale"])
        self.assertEqual(view["verdict"], "unknown")
        self.assertIsNone(view["reachable"])
        self.assertEqual(view["node"]["cpu_pct"], 30.5)
        self.assertEqual(view["accel"]["card_name"], "NVIDIA A100")
        self.assertEqual(view["accel"]["cards"][0]["mem_total"], 40960)
        self.assertEqual((view["accel"]["free"], view["accel"]["busy"], view["accel"]["residual"], view["accel"]["unknown"]), (0, 0, 0, 1))
        self.assertIsNone(view["accel"]["cards"][0]["busy"])
        self.assertEqual(row["view"]["verdict"], "free", "save must not mutate live caller state")
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(disk["entries"][key]["view"]["stale"])
        self.assertEqual(disk["entries"][key]["view"]["accel"]["free"], 0)

    def test_only_display_allowlist_survives_and_process_history_is_removed(self):
        key, row = item()
        private = "private-material-must-never-be-cached"
        row.update(profile={"identity_file": private, "ssh_connection": private}, api_token=private,
                   address=private, username=private, sample=private)
        row["view"].update(raw=private, error=private, cmdline=private, address=private,
                           last_known_accel={"secret": private}, top=private)
        row["view"]["node"].update(top=[private], logins=[private], cpu_sample={"secret": private})
        row["view"]["accel"]["cards"][0].update(procs=[{"pid": 4242, "user": private, "name": private, "cmdline": private}],
                                                   health=private, arbitrary=private)
        self.assertTrue(self.cache.save({key: row}))
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(private, text)
        for field in ("profile", "api_token", "identity_file", "ssh_connection", "address", "cmdline", "cpu_sample", "last_known_accel"):
            self.assertNotIn('"' + field + '"', text)
        self.assertEqual(self.cache.load()[key]["view"]["accel"]["cards"][0]["procs"], [])

    def test_load_resanitizes_tampered_disk_fields_and_never_replays_free(self):
        key, row = item()
        row["view"]["accel"]["free"] = 900
        row["view"]["node"]["logins"] = ["private login"]
        row["api_token"] = "private api credential"
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"schema_version": 1, "entries": {key: row}}), encoding="utf-8")
        loaded = self.cache.load()[key]
        self.assertEqual(loaded["view"]["accel"]["free"], 0)
        self.assertTrue(loaded["view"]["stale"])
        self.assertNotIn("private", json.dumps(loaded))

    def test_corrupt_wrong_schema_deeply_nested_and_oversized_files_are_ignored(self):
        self.assertEqual(self.cache.load(), {})
        self.path.parent.mkdir()
        for content in ("not-json", "[1, 2]", '{"schema_version":2,"entries":{}}',
                        '{"schema_version":true,"entries":{}}', '{"schema_version":1,"entries":[]}', "[" * 2000 + "]" * 2000):
            self.path.write_text(content, encoding="utf-8")
            self.assertEqual(self.cache.load(), {})
        with patch("fl_dashboard.resources.device_cache.MAX_CACHE_BYTES", 100):
            self.path.write_bytes(b"x" * 101)
            self.assertEqual(self.cache.load(), {})

    def test_invalid_connection_fingerprints_and_identifier_injection_are_ignored(self):
        key, row = item()
        for changes in ({"fingerprint": "private-string"}, {"project_id": "../../project"},
                        {"source_type": "arbitrary"}, {"id": "device\nHost *"}, {"ssh_alias": "-oProxyCommand=bad"}):
            self.assertFalse(self.cache.save({key: {**row, **changes}}))
            self.assertEqual(self.cache.load(), {})
        self.assertFalse(self.cache.save({"f" * 64: row}))
        self.assertEqual(self.cache.load(), {})

    def test_numbers_and_display_strings_are_bounded(self):
        key, row = item()
        row["label"] = "\x00" + "A" * 1000
        row["backend_label"] = "sk-" + "q" * 32
        card = row["view"]["accel"]["cards"][0]
        card.update(index=10**1000, util=math.nan, mem_used=1000, mem_total=100, temp=math.inf, power=-10)
        row["view"]["node"].update(cpu_pct=101, load_per_cpu=10**1000, mem_avail_mib=999999)
        row["view"]["accel"]["family"] = {"invalid": "type"}
        row["updated_at"] = "invalid"
        self.assertTrue(self.cache.save({key: row}))
        loaded = self.cache.load()[key]
        self.assertEqual(len(loaded["label"]), 200)
        self.assertNotIn("\x00", loaded["label"])
        self.assertEqual(loaded["backend_label"], "[redacted]")
        self.assertIsNone(loaded["updated_at"])
        card = loaded["view"]["accel"]["cards"][0]
        for field in ("index", "util", "mem_used", "temp", "power"):
            self.assertIsNone(card[field], field)
        self.assertIsNone(loaded["view"]["node"]["cpu_pct"])
        self.assertIsNone(loaded["view"]["node"]["load_per_cpu"])
        self.assertIsNone(loaded["view"]["node"]["mem_avail_mib"])
        json.dumps(loaded, allow_nan=False)

    def test_host_card_and_file_size_limits_are_enforced(self):
        entries = dict(item("device-" + str(i)) for i in range(5))
        for row in entries.values():
            row["view"]["accel"]["cards"] *= 10
            row["view"]["accel"]["total"] = 10
        with patch("fl_dashboard.resources.device_cache.MAX_ENTRIES", 2), patch("fl_dashboard.resources.device_cache.MAX_CARDS", 2):
            self.assertFalse(self.cache.save(entries))
            loaded = self.cache.load()
            self.assertEqual(len(loaded), 2)
            self.assertTrue(all(len(row["view"]["accel"]["cards"]) == 2 for row in loaded.values()))
            self.assertTrue(all(row["view"]["accel"]["total"] == 2 for row in loaded.values()))
        self.cache.save(dict([item()]))
        limit = self.path.stat().st_size + 64
        with patch("fl_dashboard.resources.device_cache.MAX_CACHE_BYTES", limit):
            self.assertFalse(self.cache.save(dict(item("device-" + str(i)) for i in range(5))))
            self.assertLessEqual(self.path.stat().st_size, limit)
            self.assertEqual(len(self.cache.load()), 1)

    def test_atomic_replace_failure_keeps_previous_cache_and_removes_temp(self):
        key, row = item()
        self.assertTrue(self.cache.save({key: row}))
        before = self.path.read_bytes()
        row["label"] = "New label"
        with patch("fl_dashboard.resources.device_cache.os.replace", side_effect=OSError("write unavailable")):
            self.assertFalse(self.cache.save({key: row}))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertNotEqual(self.cache.load()[key]["label"], "New label")

    def test_saved_file_is_owner_only_on_posix(self):
        self.assertTrue(self.cache.save(dict([item()])))
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_empty_or_invalid_input_does_not_raise(self):
        self.assertFalse(self.cache.save(None))
        self.assertFalse(self.cache.save([]))
        self.assertTrue(self.cache.save({}))
        self.assertEqual(self.cache.load(), {})


if __name__ == "__main__":
    unittest.main()
