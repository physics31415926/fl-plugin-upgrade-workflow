"""Synthetic AMD-SMI 26.x replies; no captured machines or credentials."""
from copy import deepcopy
import json
import math
import subprocess
import sys
import unittest

from fl_dashboard.resources.amd import AMD_COMPACTOR, parse_amd
from fl_dashboard.resources.telemetry import COMPLETE, MAX_REPLY_BYTES, parse_telemetry


SYSTEM = "##SYS\n0.20 0.10 0.10 1/40 100\n8\n8\n8388608 4194304 0\n120\n1000 600\nfixture-node\n"
NVIDIA = "##NVIDIA\n0, NVIDIA A100, 0, 4, 40960, 30, 60, 00000000:01:00.0\nfl-dashboard-process-ok:nvidia\n"


def fixtures(count=2):
    static, metrics, processes = [], [], []
    for gpu in range(count):
        static.append({"gpu": gpu, "asic": {"market_name": "AMD Radeon PRO W7900D", "vendor_id": "0x1002"}})
        metrics.append({"gpu": gpu, "usage": {"gfx_activity": {"value": 0, "unit": "%"}},
                        "mem_usage": {"total_vram": {"value": 49152, "unit": "MB"},
                                      "used_vram": {"value": 16, "unit": "MB"}},
                        "temperature": {"edge": {"value": 32, "unit": "C"}},
                        "power": {"socket_power": {"value": 22.5, "unit": "W"}}})
        processes.append({"gpu": gpu, "process_list": [{"process_info": "No running processes detected"}]})
    return static, metrics, processes


def section(kind, rows, *, wrapper=False, proof=True):
    payload = {"gpu_data": rows} if wrapper else rows
    return ("##AMD-" + kind.upper() + "\n" +
            ("fl-dashboard-amd-" + kind.lower() + "-ok\n" if proof else "") +
            json.dumps(payload, ensure_ascii=False) + "\n")


def body(parts=None, *, wrapper=False, omit=None, proof=True):
    parts = fixtures() if parts is None else parts
    return "".join(section(kind, rows, wrapper=wrapper, proof=proof)
                   for kind, rows in zip(("static", "metric", "process"), parts)
                   if kind != omit)


def amd(raw):
    return parse_amd(raw)[0]


def host(raw):
    return parse_telemetry("fixture-device", "jms-top-ok\n" + SYSTEM + raw + "\n##AMD-END\n" + COMPLETE + "\n")[0]


class AmdTelemetryTests(unittest.TestCase):
    def assert_not_free(self, raw):
        value = host(raw)
        self.assertEqual(0, value["accel"]["free"])
        self.assertNotEqual("free", value["verdict"])
        return value

    def test_array_and_gpu_data_wrapper_decode_same_metrics_and_empty_process_proof(self):
        for wrapper in (False, True):
            with self.subTest(wrapper=wrapper):
                parsed = amd(body(wrapper=wrapper))
                self.assertEqual("amd", parsed.family)
                self.assertEqual([0, 1], [card.index for card in parsed.cards])
                for card in parsed.cards:
                    self.assertEqual("AMD Radeon PRO W7900D", card.name)
                    self.assertEqual((0, 16, 49152, 32, 22.5),
                                     (card.util, card.mem_used, card.mem_total, card.temp, card.power))
                    self.assertTrue(card.procs_known)
                    self.assertEqual([], card.procs)
                value = host(body(wrapper=wrapper))
                self.assertEqual("rocm", value["backend"])
                self.assertTrue(value["collection_complete"])
                self.assertEqual("free", value["verdict"])
                self.assertEqual((2, 2, 0, 0), tuple(value["accel"][k] for k in ("total", "free", "busy", "unknown")))

    def test_completion_flag_distinguishes_absent_amd_from_incomplete_amd(self):
        self.assertEqual((None, True), parse_amd(NVIDIA))
        self.assertTrue(parse_amd(body())[1])
        self.assertFalse(parse_amd(body(omit="process"))[1])
        self.assertEqual((None, False), parse_amd("##AMD-STATIC\ninvalid\n"))

    def test_empty_process_arrays_are_complete(self):
        parts = fixtures()
        for row in parts[2]:
            row["process_list"] = []
        self.assertEqual(2, host(body(parts))["accel"]["free"])
        self.assertTrue(all(card.procs_known for card in amd(body(parts)).cards))

    def test_process_bytes_are_converted_to_mib_and_zero_util_does_not_mean_free(self):
        parts = fixtures()
        parts[2][1]["process_list"] = [{"process_info": {
            "pid": 4242, "name": "python", "mem_usage": {"value": 3 * 1024**3, "unit": "B"}}}]
        parsed = amd(body(parts))
        proc = parsed.cards[1].procs[0]
        self.assertEqual((4242, "python", 3072), (proc.pid, proc.name, proc.mem_mib))
        value = host(body(parts))
        self.assertEqual((1, 1), (value["accel"]["free"], value["accel"]["busy"]))
        self.assertEqual("busy", value["accel"]["cards"][1]["state"])

    def test_high_activity_or_memory_marks_card_busy_with_empty_process_list(self):
        for field, measurement in (("usage", {"gfx_activity": {"value": 50, "unit": "%"}}),
                                   ("mem_usage", {"total_vram": {"value": 49152, "unit": "MB"},
                                                  "used_vram": {"value": 32768, "unit": "MB"}})):
            with self.subTest(field=field):
                parts = fixtures(1)
                parts[1][0][field] = measurement
                self.assertEqual(1, host(body(parts))["accel"]["busy"])
                self.assert_not_free(body(parts))

    def test_optional_temperature_and_power_na_do_not_hide_valid_capacity(self):
        for optional in ({}, {"value": "N/A", "unit": "N/A"}, None):
            with self.subTest(optional=optional):
                parts = fixtures(1)
                parts[1][0]["temperature"] = {"edge": optional}
                parts[1][0]["power"] = {"socket_power": optional}
                value = host(body(parts))
                self.assertTrue(value["collection_complete"])
                self.assertEqual(1, value["accel"]["free"])
                self.assertIsNone(value["accel"]["cards"][0]["temp"])
                self.assertIsNone(value["accel"]["cards"][0]["power"])
        parts = fixtures(1)
        del parts[1][0]["temperature"]
        del parts[1][0]["power"]
        self.assertTrue(host(body(parts))["collection_complete"])

    def test_binary_memory_units_are_consistent_for_card_and_process_values(self):
        for unit, factor in (("MB", 1), ("MiB", 1), ("B", 1024**2)):
            with self.subTest(unit=unit):
                parts = fixtures(1)
                parts[1][0]["mem_usage"] = {
                    "used_vram": {"value": 4096 * factor, "unit": unit},
                    "total_vram": {"value": 49152 * factor, "unit": unit}}
                parts[2][0]["process_list"] = [{"process_info": {
                    "pid": 4242, "name": "python", "mem_usage": {"value": 4096 * factor, "unit": unit}}}]
                parsed, complete = parse_amd(body(parts))
                self.assertTrue(complete)
                card = parsed.cards[0]
                self.assertEqual((4096, 49152, 4096), (card.mem_used, card.mem_total, card.procs[0].mem_mib))

    def test_missing_process_section_keeps_metrics_without_claiming_free(self):
        raw = body(omit="process")
        parsed = amd(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual((16, 49152, 0), (parsed.cards[0].mem_used, parsed.cards[0].mem_total, parsed.cards[0].util))
        self.assertTrue(all(not card.procs_known for card in parsed.cards))
        value = self.assert_not_free(raw)
        self.assertEqual(2, value["accel"]["unknown"])
        self.assertEqual(16, value["accel"]["cards"][0]["mem_used"])

    def test_each_missing_success_proof_prevents_free(self):
        for kind in ("static", "metric", "process"):
            with self.subTest(kind=kind):
                self.assert_not_free(body().replace("fl-dashboard-amd-" + kind + "-ok\n", ""))

    def test_bad_json_truncated_or_duplicate_sections_never_claim_free(self):
        for raw in (body().replace('"gpu": 0', '"gpu":', 1),
                    body().rsplit("]", 1)[0],
                    body() + section("metric", fixtures()[1]),
                    body() + "##STALLED:amd:output-truncated\n",
                    body() + "##STALLED:amd:query-failed\n"):
            with self.subTest(raw=raw[-80:]):
                self.assert_not_free(raw)

    def test_invalid_metric_numbers_or_units_never_claim_free(self):
        mutations = [
            ("usage", "gfx_activity", {"value": math.nan, "unit": "%"}),
            ("usage", "gfx_activity", {"value": math.inf, "unit": "%"}),
            ("usage", "gfx_activity", {"value": True, "unit": "%"}),
            ("usage", "gfx_activity", {"value": 101, "unit": "%"}),
            ("usage", "gfx_activity", {"value": 0, "unit": "wrong"}),
            ("mem_usage", "total_vram", {"value": 0, "unit": "MB"}),
            ("mem_usage", "used_vram", {"value": -1, "unit": "MB"}),
            ("mem_usage", "used_vram", {"value": 50000, "unit": "MB"}),
            ("mem_usage", "used_vram", {"value": 16, "unit": "unknown"}),
        ]
        for group, key, measurement in mutations:
            with self.subTest(group=group, key=key, measurement=measurement):
                parts = fixtures(1)
                parts[1][0][group][key] = measurement
                self.assert_not_free(body(parts))

    def test_missing_required_metrics_are_not_treated_as_zero(self):
        for group, key in (("usage", "gfx_activity"), ("mem_usage", "total_vram"), ("mem_usage", "used_vram")):
            with self.subTest(group=group, key=key):
                parts = fixtures(1)
                del parts[1][0][group][key]
                self.assert_not_free(body(parts))

    def test_inconsistent_gpu_sets_duplicate_ids_and_invalid_ids_are_not_free(self):
        for section_index in range(3):
            for change in ("missing", "duplicate", "extra", "boolean", "negative"):
                with self.subTest(section=section_index, change=change):
                    parts = fixtures()
                    rows = parts[section_index]
                    if change == "missing":
                        rows.pop()
                    elif change == "duplicate":
                        rows.append(deepcopy(rows[0]))
                    elif change == "extra":
                        row = deepcopy(rows[0])
                        row["gpu"] = 3
                        rows.append(row)
                    else:
                        rows[0]["gpu"] = True if change == "boolean" else -1
                    self.assert_not_free(body(parts))

    def test_more_than_256_cards_is_not_silently_truncated_to_free(self):
        self.assert_not_free(body(fixtures(257)))

    def test_process_schema_failures_are_not_empty_process_proof(self):
        for process_list in (None, {}, "No running processes detected", [None],
                             [{"process_info": "unrecognized response"}],
                             [{"process_info": {"pid": True, "name": "python", "mem_usage": {"value": 0, "unit": "B"}}}],
                             [{"process_info": {"pid": 4242, "name": "python", "mem_usage": {"value": 0, "unit": "unknown"}}}]):
            with self.subTest(process_list=process_list):
                parts = fixtures(1)
                parts[2][0]["process_list"] = process_list
                parsed = amd(body(parts))
                if parsed is not None:
                    self.assertFalse(parsed.cards[0].procs_known)
                self.assert_not_free(body(parts))

    def test_other_asic_vendors_and_missing_identity_are_not_recognized_as_amd(self):
        for vendor in ("0x10de", "0x1d94", "0x1002-invalid", None):
            with self.subTest(vendor=vendor):
                parts = fixtures(1)
                if vendor is None:
                    del parts[0][0]["asic"]["vendor_id"]
                else:
                    parts[0][0]["asic"]["vendor_id"] = vendor
                self.assertIsNone(amd(body(parts)))
        self.assertIsNone(amd("##AMD-STATIC\n##STALLED:amd:query-failed\n"))

    def test_amd_failure_is_isolated_from_valid_nvidia_device(self):
        failure = "##AMD-STATIC\n##STALLED:amd:query-failed\n"
        for raw in (failure + NVIDIA, NVIDIA + failure):
            value = host(raw)
            self.assertEqual("cuda", value["backend"])
            self.assertTrue(value["collection_complete"])
            self.assertEqual(1, value["accel"]["free"])
            self.assertIsNone(value["error"])
        value = host("##NVIDIA\n##STALLED:nvidia:query-failed\n" + body())
        self.assertEqual("rocm", value["backend"])
        self.assertTrue(value["collection_complete"])
        self.assertEqual(2, value["accel"]["free"])

    def test_unrelated_json_fields_and_process_arguments_are_not_exposed(self):
        parts = fixtures(1)
        for rows in parts:
            rows[0]["api_key"] = "fixture-sensitive-extra"
            rows[0]["private_path"] = "/fixture/private/raw-path"
        parts[2][0]["process_list"] = [{"process_info": {
            "pid": 4242, "name": "/private/bin/python --token fixture-sensitive-argument",
            "cmdline": "fixture-sensitive-command", "user": "fixture-private-user",
            "mem_usage": {"value": 1024**2, "unit": "B"}}}]
        value = host(body(parts))
        serialized = json.dumps(value, ensure_ascii=False)
        for secret in ("fixture-sensitive", "fixture-private-user", "/private", "raw-path", "api_key", "cmdline"):
            self.assertNotIn(secret, serialized)
        self.assertEqual("python", value["accel"]["cards"][0]["procs"][0]["name"])
        self.assertEqual(1, value["accel"]["cards"][0]["procs"][0]["mem_mib"])

    def test_missing_footer_or_oversized_reply_never_reports_free(self):
        raw = "jms-top-ok\n" + SYSTEM + body()
        value = parse_telemetry("fixture", raw)[0]
        self.assertEqual(0, value["accel"]["free"])
        raw += "z" * MAX_REPLY_BYTES + "\n" + COMPLETE + "\n"
        value = parse_telemetry("fixture", raw)[0]
        self.assertEqual(0, value["accel"]["free"])


class AmdCompactorTests(unittest.TestCase):
    def execute(self, kind, value, *, raw=False):
        payload = value if raw else json.dumps(value)
        return subprocess.run([sys.executable, "-c", AMD_COMPACTOR, kind],
                              input=payload.encode("utf-8"), capture_output=True, timeout=5,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_both_wrapper_versions_produce_valid_complete_compact_sections(self):
        for wrapper in (False, True):
            with self.subTest(wrapper=wrapper):
                sections = []
                for kind, rows in zip(("static", "metric", "process"), fixtures()):
                    result = self.execute(kind, {"gpu_data": rows} if wrapper else rows)
                    self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
                    self.assertLess(len(result.stdout), 64 * 1024)
                    json.loads(result.stdout)
                    sections.append("##AMD-" + kind.upper() + "\nfl-dashboard-amd-" + kind + "-ok\n" + result.stdout.decode("utf-8"))
                value = host("\n".join(sections))
                self.assertEqual("rocm", value["backend"])
                self.assertEqual(2, value["accel"]["free"])
                self.assertTrue(value["collection_complete"])

    def test_large_process_json_is_compacted_before_ssh_reply_limit(self):
        parts = fixtures(1)
        parts[2][0]["process_list"] = [{"process_info": {
            "pid": 4242, "name": "/fixture/bin/python --token fixture-sensitive-argument",
            "mem_usage": {"value": 1024**2, "unit": "B"},
            "cmdline": "fixture-sensitive-command", "unused": "x" * (156 * 1024)}}]
        result = self.execute("process", {"gpu_data": parts[2]})
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertLess(len(result.stdout), 32 * 1024)
        self.assertNotIn(b"fixture-sensitive", result.stdout)
        self.assertNotIn(b"/fixture", result.stdout)
        self.assertNotIn(b"unused", result.stdout)
        self.assertNotIn(b"cmdline", result.stdout)
        raw = body(parts, omit="process") + "##AMD-PROCESS\nfl-dashboard-amd-process-ok\n" + result.stdout.decode("utf-8")
        value = host(raw)
        self.assertEqual(1, value["accel"]["busy"])
        self.assertEqual("python", value["accel"]["cards"][0]["procs"][0]["name"])

    def test_card_limit_accepts_256_complete_compacted_cards(self):
        sections = []
        for kind, rows in zip(("static", "metric", "process"), fixtures(256)):
            result = self.execute(kind, rows)
            self.assertEqual(0, result.returncode)
            sections.append("##AMD-" + kind.upper() + "\nfl-dashboard-amd-" + kind + "-ok\n" + result.stdout.decode("utf-8"))
        raw = "\n".join(sections)
        self.assertLess(len(raw.encode("utf-8")), MAX_REPLY_BYTES)
        value = host(raw)
        self.assertTrue(value["collection_complete"])
        self.assertEqual((256, 256), (value["accel"]["total"], value["accel"]["free"]))

    def test_input_and_card_and_process_limits_fail_instead_of_truncating(self):
        for kind, raw in (("static", "["),
                          ("static", json.dumps(fixtures(257)[0])),
                          ("process", json.dumps([{"gpu": 0, "process_list": [
                              {"process_info": {"pid": i + 1, "name": "python", "mem_usage": {"value": 0, "unit": "B"}}}
                              for i in range(2049)]}])),
                          ("static", json.dumps({"gpu_data": fixtures(1)[0], "unused": "x" * (1024 * 1024)}))):
            with self.subTest(kind=kind, length=len(raw)):
                result = self.execute(kind, raw, raw=True)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn(b"fl-dashboard-amd", result.stdout)
                self.assertLessEqual(len(result.stdout), 64 * 1024)


if __name__ == "__main__":
    unittest.main()
