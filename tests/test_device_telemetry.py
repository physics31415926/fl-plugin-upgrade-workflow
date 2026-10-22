"""Synthetic and anonymized parser cases; upstream attribution: vendor/jms_top/NOTICE."""
import json
import math
import unittest
from unittest.mock import patch

from fl_dashboard.resources.telemetry import COMPLETE, MAX_REPLY_BYTES, parse_telemetry, probe_script
from fl_dashboard.vendor.jms_top.cpu import CpuSample
from fl_dashboard.vendor.jms_top.families import Card, Proc

SAMPLES = {
    'nvidia': '##NVIDIA\n0, NVIDIA A100, 0, 4, 40960, 30, 60, 00000000:01:00.0\nfl-dashboard-process-ok:nvidia',
    'ascend': '##ASCEND\n+------------------------------------------------------------------------------------------------+\n| npu-smi 25.5.0                   Version: 25.5.0                                               |\n+---------------------------+---------------+----------------------------------------------------+\n| NPU   Name                | Health        | Power(W)    Temp(C)           Hugepages-Usage(page)|\n| Chip  Phy-ID              | Bus-Id        | AICore(%)   Memory-Usage(MB)  HBM-Usage(MB)        |\n+===========================+===============+====================================================+\n| 0     Ascend910           | OK            | 168.7       53                0    / 0             |\n| 0     0                   | 0000:01:00.0  | 0           0    / 0          3179 / 65536         |\n+------------------------------------------------------------------------------------------------+\n| 0     Ascend910           | OK            | -           52                0    / 0             |\n| 1     1                   | 0000:01:00.0  | 0           0    / 0          2891 / 65536         |\nProcesses:\nNo running processes found',
    'kunlunxin': '##KUNLUNXIN\nWed Oct 21 19:07:46 2026       \n+-----------------------------------------------------------------------------+\n| XPU-SMI               Driver Version: 5.0.21.43    XPU-RT Version: 5.0.21   |\n|-------------------------------+----------------------+----------------------+\n| XPU  Name        Persistence-M| Bus-Id        Disp.A | Volatile Uncorr. ECC |\n| Fan  Temp  Perf  Pwr:Usage/Cap|         Memory-Usage | XPU-Util  Compute M. |\n|                               |             L3-Usage |            SR-IOV M. |\n|===============================+======================+======================|\n|   0  P800 OAM           N/A   | 00000000:01:00.0 N/A |                    0 |\n| N/A   36C  N/A    102W / 400W |    164MiB / 98304MiB |    100%      Default |\nProcesses:\nNo running processes found',
    'metax': '##METAX-WIDE\nGPU#0  MXC550  0000:01:00.0\n    Memory\n        vis_vram total                            : 67108864 KB\n        vis_vram used                             : 47663812 KB\n        vis_vram usage                            : 71 %\n        GPU                                      : 0 %\n        vram total                                : 67108864 KB\n        vram used                                 : 47663812 KB\n        vram usage                                : 71 %\nProcess:\nfl-dashboard-process-ok:metax',
    'enflame': '##ENFLAME\n-------------------------------------------------------------------------------\n--------------------- Enflame System Management Interface ---------------------\n--------- Enflame Tech, All Rights Reserved. 2024-2026 Copyright (C) ----------\n-------------------------------------------------------------------------------\n                                                                               \n+2000-01-01, 00:00:00 UTC-----------------------------------------------------+\n| EFSMI: 1.9.10                Driver Ver: 1.9.10                             |\n+-----------------------------+-------------------+---------------------------+\n| DEV    NAME                 | Boot FW VER       | BUS-ID      ECC           |\n| TEMP   Lpm   Pwr(Usage/Cap) | Mem      GCU Virt | DUsed       SN            |\n|=============================================================================|\n| 0      ZIXIAOC200           | 35.6.5            | 00:23:00.0  Enable        |\n| 51℃    LP0      132W / 300W | 42976MiB  Disable | 0%          REDACTED-SERIAL |\n*Dev Pid DevMem SysMem Command\n0 N/A N/A N/A no process running on\nfl-dashboard-process-ok:enflame',
    'mthreads': '##MTHREADS\nWed Oct 21 19:07:54 2026\n---------------------------------------------------------------------\n    mthreads-gmi:2.3.2           Driver Version:3.3.5-server\n---------------------------------------------------------------------\nID   Name                 |PCIe                |%GPU  Mem             \n     Device Type   Perf   |Pcie Lane Width     |Temp  MPC Capable     \n                                               |      ECC Mode        \n+-------------------------------------------------------------------+\n0    MTT S5000            |0000:01:00.0    |0%    0MiB(81920MiB)\nProcesses:\nNo running processes found',
    'tsingmicro': '##TSINGMICRO\n##TSINGMICRO\nThu Oct 22 05:09:53 2026\n+----------------------------------------------------------------------------------------------+\n| SMI V0.29842.5.01                          Chip count: 32                                    |\n|--------------------------+-------------------------------------------------------------------|\n|  Card  Name              | Chip    Bus-Id      Temp   Pwr-Usage      Mem-Usage      NPU-Util |\n|==========================+===================================================================|\n|  0    TX8110-256-00      |  0    0000:01:00.0   45C   68W / 200W   58997M / 65536M     0%    |\n|                          |-------------------------------------------------------------------|\n|                          |  1    0000:01:00.0   44C   64W / 200W   59427M / 65536M     0%    |\n|                          |-------------------------------------------------------------------|\n|                          |  2    0000:01:00.0   45C   63W / 200W   59427M / 65536M     0%    |\n|                          |-------------------------------------------------------------------|\n|                          |  3    0000:01:00.0   45C   64W / 200W   59427M / 65536M     0%    |\nProcesses:\nNo running processes found',
    'thead': '##THEAD\nWed Oct 21 13:29:16 2026\n+-------------------------------------------------------------------------------+\n| PPU-SMI 1.28          Driver Version: 1.3.2-d7f5a2  HGGC Version: 13.0        |\n+---------------------------------+----------------------+----------------------+\n| PPU  Name        Persistence M. | Bus-Id               | Volatile Uncorr. ECC |\n| Fan  Temp  Perf   Pwr:Usage/Cap | Memory-Usage         | PPU-Util  Compute M. |\n|                                 |                      |               MIG M. |\n+=================================+======================+======================+\n| 0  PPU-ZW810E        N/A        | 00000000:01:00.0     |                    0 |\n| N/A  33C   N/A       82W / 400W | 2MiB / 97920MiB      |   0%        Default  |\nProcesses:\nNo running processes found',
    'iluvatar': '##ILUVATAR\nTimestamp    Wed Oct 21 23:14:37 2026\n+--------------------------------------------------------------------------------+\n|  IX-ML: 4.4.0            Driver Version: 4.5.0          CUDA Version: 10.2     |\n|----------------------------------+----------------------+----------------------|\n| GPU  Name                        | Bus-Id               | Clock-SM  Clock-Mem  |\n| Fan  Temp  Perf    Pwr:Usage/Cap |      Memory-Usage    | GPU-Util  Compute M. |\n|==================================+======================+======================|\n| 0    Iluvatar BI-V150            | 00000000:01:00.0     | 1500MHz   1600MHz    |\n| N/A  31C   P0       N/A / N/A    | 68MiB / 32768MiB     | 0%        Default    |\nProcesses:\nNo running processes found',
    'hygon': '##HYGON\n##HYGON\n\n================================= System Management Interface ==================================\n================================================================================================\nHCU     Temp     AvgPwr     Perf     PwrCap     VRAM%      HCU%      Dec%      Enc%      Mode     \n0       51.0C    88.0W      auto     1000.0W    0%         0.0%      0.0%      0.0%      Normal   \n1       50.0C    91.0W      auto     1000.0W    59%        0.0%      0.0%      0.0%      Normal   \n2       51.0C    92.0W      auto     1000.0W    0%         0.0%      0.0%      0.0%      Normal   \n3       51.0C    89.0W      auto     1000.0W    0%         0.0%      0.0%      0.0%      Normal   \n4       52.0C    87.0W      auto     1000.0W    0%         0.0%      0.0%      0.0%      Normal   \n5       53.0C    82.0W      auto     1000.0W    0%         0.0%      0.0%      0.0%      Normal   \n6       52.0C    86.0W      auto     1000.0W    0%         0.0%      0.0%      0.0%      Normal   \n7       52.0C    93.0W      auto     1000.0W    0%         0.0%      0.0%      0.0%      Normal   \n================================================================================================\n======================================== End of SMI Log ========================================\n##HYGON-VRAM\ncard1 2666496 68702699520\ncard2 40758824960 68702699520\ncard3 2666496 68702699520\ncard4 2666496 68702699520\ncard5 2666496 68702699520\ncard6 2666496 68702699520\ncard7 2666496 68702699520\ncard8 2666496 68702699520\n##HYGON-PROCS\nfl-dashboard-process-ok:hygon',
}

SYSTEM = "##SYS\n0.20 0.10 0.10 1/40 100\n8\n8\n8388608 4194304 0\n120\n{total} {idle}\nfixture-node\n"


def reply(body, *, system=False):
    return "jms-top-ok\n" + (SYSTEM.format(total=1000, idle=600) if system else "") + body + "\n" + COMPLETE + "\n"


class DeviceTelemetryTests(unittest.TestCase):
    def test_ten_families_keep_counts_and_backend_names(self):
        expected = {
            "nvidia": ("cuda", 1, 1, 0, 0), "ascend": ("ascend", 2, 2, 0, 0),
            "kunlunxin": ("kunlunxin", 1, 0, 1, 0), "metax": ("metax", 1, 0, 1, 0),
            "enflame": ("enflame", 1, 0, 0, 1), "mthreads": ("musa", 1, 1, 0, 0),
            "tsingmicro": ("tsingmicro", 4, 0, 4, 0), "thead": ("thead", 1, 1, 0, 0),
            "iluvatar": ("corex", 1, 1, 0, 0), "hygon": ("hygon", 8, 7, 1, 0),
        }
        for family, values in expected.items():
            with self.subTest(family=family):
                host, _ = parse_telemetry("fixture-device", reply(SAMPLES[family], system=True))
                accel = host["accel"]
                self.assertEqual((host["backend"], *(accel[k] for k in ("total", "free", "busy", "unknown"))), values)
                self.assertTrue(host["reachable"])
                self.assertTrue(host["supported"])
                self.assertNotIn("raw", host)
                self.assertNotIn("top", host["node"])

    def test_ascend_and_tsingmicro_preserve_board_and_chip_counts(self):
        for family, chips in (("ascend", 2), ("tsingmicro", 4)):
            host, _ = parse_telemetry("fixture", reply(SAMPLES[family]))
            self.assertEqual(host["accel"]["physical_cards"], 1)
            self.assertEqual(host["accel"]["chips_per_card"], chips)
            self.assertEqual([c["card"] for c in host["accel"]["cards"]], [0] * chips)

    def test_ascend_process_belongs_to_correct_chip_on_repeated_board(self):
        body = SAMPLES["ascend"] + "\n| 0 1 | 4242 | python | 1024 |"
        host, _ = parse_telemetry("fixture", reply(body))
        cards = host["accel"]["cards"]
        self.assertFalse(cards[0]["busy"])
        self.assertTrue(cards[1]["busy"])
        self.assertEqual(cards[1]["procs"][0]["pid"], 4242)

    def test_cpu_percentage_needs_two_valid_samples(self):
        host, previous = parse_telemetry("fixture", reply(SAMPLES["nvidia"], system=True))
        self.assertIsNone(host["node"]["cpu_pct"])
        second = reply(SAMPLES["nvidia"], system=True).replace("1000 600", "1200 650")
        host, current = parse_telemetry("fixture", second, previous)
        self.assertEqual(host["node"]["cpu_pct"], 75.0)
        self.assertEqual(host["node"]["uptime_s"], 120)
        self.assertEqual(host["node"]["mem_pct"], 50)
        self.assertEqual(current.total, 1200)
        reboot = reply(SAMPLES["nvidia"], system=True).replace("1000 600", "200 100")
        host, _ = parse_telemetry("fixture", reboot, current)
        self.assertIsNone(host["node"]["cpu_pct"])

    def test_node_load_missing_or_high_prevents_whole_host_free(self):
        host, _ = parse_telemetry("fixture", reply(SAMPLES["nvidia"]))
        self.assertEqual(host["verdict"], "partial")
        self.assertEqual(host["accel"]["free"], 1)
        host, _ = parse_telemetry("fixture", reply(SAMPLES["nvidia"], system=True))
        self.assertEqual(host["verdict"], "free")
        raw = reply(SAMPLES["nvidia"], system=True).replace("0.20 0.10 0.10", "8.00 0.10 0.10")
        host, _ = parse_telemetry("fixture", raw)
        self.assertEqual(host["verdict"], "partial")

    def test_incomplete_stalled_and_truncated_never_expose_free_capacity(self):
        complete = reply(SAMPLES["nvidia"], system=True)
        for raw in (complete.replace(COMPLETE, ""),
                    complete.replace(COMPLETE, "##STALLED:nvidia:process-query-failed\n" + COMPLETE),
                    complete.replace(COMPLETE, "##STALLED:generic:output-truncated\n" + COMPLETE)):
            host, sample = parse_telemetry("fixture", raw)
            self.assertEqual(host["verdict"], "unknown")
            self.assertEqual(host["accel"]["free"], 0)
            self.assertEqual(host["accel"]["unknown"], 1)
            self.assertIsNone(sample)

    def test_missing_process_evidence_never_becomes_empty_process_table(self):
        for family in ("nvidia", "ascend", "mthreads", "iluvatar", "thead"):
            body = SAMPLES[family].replace("fl-dashboard-process-ok:nvidia", "").replace("Processes:", "").replace("No running processes found", "")
            host, _ = parse_telemetry("fixture", reply(body))
            self.assertEqual(host["accel"]["free"], 0, family)
        body = "##METAX-WIDE\nGPU#0 C550 0000:01:00.0\n  vis_vram total : 67108864 KB\n  vis_vram used : 0 KB\n  GPU : 0 %"
        host, _ = parse_telemetry("fixture", reply(body))
        self.assertEqual(host["accel"]["free"], 0)

    def test_unsupported_sunrise_and_no_accelerators_are_unknown(self):
        for body in ("", "##SUNRISE\nunsupported device utility"):
            host, _ = parse_telemetry("sunrise-fixture", reply(body, system=True))
            self.assertTrue(host["reachable"])
            self.assertEqual(host["verdict"], "unknown")
            self.assertFalse(host["supported"])
            self.assertEqual(host["accel"]["free"], 0)

    def test_residual_is_busy_capacity_and_not_free(self):
        host, _ = parse_telemetry("fixture", reply(SAMPLES["tsingmicro"]))
        self.assertEqual(host["verdict"], "residual")
        self.assertEqual(host["accel"]["residual"], 4)
        self.assertEqual(host["accel"]["busy"], 4)
        self.assertEqual(host["accel"]["free"], 0)

    def test_shared_hygon_processes_are_not_attributed_to_one_card(self):
        body = SAMPLES["hygon"] + "\n4242 tester /usr/bin/python --token private-argument"
        host, _ = parse_telemetry("fixture", reply(body))
        self.assertTrue(host["accel"]["cards"][0]["procs_shared"])
        self.assertEqual(host["accel"]["cards"][0]["procs"][0]["name"], "python")
        self.assertEqual(host["accel"]["free"], 0)
        self.assertNotIn("private-argument", json.dumps(host))

    def test_untrusted_errors_and_fields_are_bounded_and_sanitized(self):
        raw = "permission denied: secret-example\n" + COMPLETE
        host, _ = parse_telemetry("fixture", raw)
        self.assertEqual(host["verdict"], "down")
        self.assertNotIn("secret-example", json.dumps(host))
        with patch("fl_dashboard.resources.telemetry.parse_reply", side_effect=ValueError("secret-example")):
            host, _ = parse_telemetry("fixture", reply(SAMPLES["nvidia"]))
            self.assertEqual(host["verdict"], "unknown")
            self.assertNotIn("secret-example", json.dumps(host))
        for raw in (None, b"invalid", "jms-top-ok\n" + "a" * MAX_REPLY_BYTES + "\n" + COMPLETE):
            host, _ = parse_telemetry("fixture", raw)
            self.assertEqual(host["verdict"], "down")
            self.assertEqual(host["accel"]["free"], 0)

    def test_malformed_numeric_values_never_serialize_nan_or_advertise_free(self):
        from fl_dashboard.resources.telemetry import _card
        for util in (math.nan, math.inf, -1, 101, 10**1000):
            result = _card(Card(index=0, util=util, mem_used=0, mem_total=1000))
            self.assertIsNone(result["busy"])
            json.dumps(result, allow_nan=False)
        result = _card(Card(index=0, name="GPU\x00" + "x" * 200,
                            procs=[Proc(pid=4242, user="tester", name="/tmp/python --api-key private-value", cmdline="private-value")]))
        self.assertLessEqual(len(result["name"]), 100)
        self.assertNotIn("\x00", result["name"])
        self.assertNotIn("private-value", json.dumps(result))
        self.assertNotIn("cmdline", result["procs"][0])

    def test_an_unrelated_failed_vendor_tool_does_not_invalidate_real_cards(self):
        raw = reply(SAMPLES["metax"], system=True).replace(COMPLETE, "##STALLED:nvidia:12s\n" + COMPLETE)
        host, _ = parse_telemetry("fixture", raw)
        self.assertTrue(host["collection_complete"])
        self.assertEqual(host["backend"], "metax")
        self.assertEqual(host["accel"]["busy"], 1)
        self.assertEqual(host["accel"]["unknown"], 0)
        own = raw.replace("##STALLED:nvidia:12s", "##STALLED:metax:query-failed")
        host, _ = parse_telemetry("fixture", own)
        self.assertFalse(host["collection_complete"])
        self.assertEqual(host["accel"]["unknown"], 1)

    def test_other_vendor_process_tables_cannot_prove_selected_cards_free(self):
        for family in ("ascend", "mthreads", "iluvatar", "thead"):
            own = SAMPLES[family].replace("Processes:", "").replace("No running processes found", "")
            other = "NVIDIA" if family == "iluvatar" else "ILUVATAR"
            host, _ = parse_telemetry("fixture", reply(own + "\n##" + other + "\nProcesses:\nNo running processes found"))
            self.assertEqual(host["accel"]["free"], 0, family)
        own = SAMPLES["thead"].replace("Processes:", "").replace("No running processes found", "")
        foreign_table = "\n##ILUVATAR\n| Processes: |\n| PPU GI CI PID Type Process name PPU Memory |\n| 0 N/A N/A 4242 C worker 2MiB |\n+--------+"
        host, _ = parse_telemetry("fixture", reply(own + foreign_table))
        self.assertEqual(host["accel"]["unknown"], 1)
        self.assertEqual(host["accel"]["cards"][0]["procs"], [])

    def test_sunrise_timeout_names_the_actual_tool_and_never_advertises_capacity(self):
        host, _ = parse_telemetry("fixture", reply("##SUNRISE\n##STALLED:sunrise:tool-timeout", system=True))
        self.assertEqual(host["backend"], "sunrise")
        self.assertIn("pt_smi", host["error"])
        self.assertFalse(host["collection_complete"])
        self.assertEqual(host["accel"]["free"], 0)
        self.assertIn('timeout -k 1s 6s "$P"', probe_script())

    def test_complete_but_unknown_readings_are_fresh_and_explain_missing_evidence(self):
        host, _ = parse_telemetry("fixture", reply(SAMPLES["nvidia"].replace("fl-dashboard-process-ok:nvidia", "")))
        self.assertTrue(host["collection_complete"])
        self.assertEqual(host["verdict"], "unknown")
        self.assertIn("进程", host["accel"]["cards"][0]["reason"])

    def test_fixed_probe_is_read_only_bounded_and_cpu_layout_is_correct(self):
        script = probe_script()
        self.assertIn("timeout -k 1s 28s bash", script)
        self.assertIn("T='timeout -k 1s 12'", script)
        self.assertIn("$8+$9, $5+$6", script)
        self.assertLess(script.index("cut -d. -f1 /proc/uptime"), script.index("awk '/^cpu /"))
        for forbidden in ("/cmdline", "##TOP", "##WHO", "T=timeout ", "head -", "--showproductname", "[ $? -eq 124 ]", "[ $rc -eq 124 ]", "curl ", "wget ", "kill ", "tmux "):
            self.assertNotIn(forbidden, script)
        for required in ("/proc/$p/comm", "bounded_head()", "##STALLED:generic:output-truncated",
                         "##STALLED:metax:process-query-failed", "##STALLED:enflame:process-query-failed",
                         "fl-dashboard-process-ok:nvidia", "fl-dashboard-process-ok:hygon"):
            self.assertIn(required, script)
        self.assertTrue(script.endswith("echo '" + COMPLETE + "'\n"))


if __name__ == "__main__":
    unittest.main()
