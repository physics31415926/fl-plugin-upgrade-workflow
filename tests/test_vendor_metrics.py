import json
import unittest

from fl_dashboard.resources.telemetry import _card
from fl_dashboard.resources.vendor_metrics import augment_vendor_metrics, probe_vendor_metrics, vendor_sections
from fl_dashboard.vendor.jms_top.families import Card, Proc, AccelResult
from fl_dashboard.vendor.jms_top.model import HostView


def view(family, count=2):
    result = HostView(name="fixture", reachable=True)
    result.accel = AccelResult(family=family, label=family, cards=[
        Card(index=index, util=0, mem_used=1, mem_total=65536, procs_known=False) for index in range(count)])
    return result


def memory(devices):
    return ("##FL-ENFLAME-MEMORY\nfl-dashboard-enflame-memory-ok\n" + json.dumps({"devices": devices})
            + "\n##FL-VENDOR-END\n")


def memory_row(index, total=42976, used=0):
    return {"id": index, "Device Mem Info": {"Total Size": total, "Reserved Size": 1129.9, "Used Size": used}}


def pids(rows):
    blocks = []
    for row in rows:
        pid, indices = row[:2]
        used = row[2] if len(row) == 3 else 128
        text = "[" + ", ".join("'" + str(index) + "'" for index in indices) + "]" if indices else ""
        memory_text = f"    VRAM USED(MiB): {used}\n" if used is not None else ""
        blocks.append(f"PID: {pid}\n    PASID: 10\n    HCU Index: {text} \n    GPUID: \n" + memory_text)
    return ("##FL-HYGON-PIDS\nfl-dashboard-hygon-pids-ok\nPIDs for KFD processes:\n" + "\n".join(blocks)
            + "End of SMI Log\n##FL-VENDOR-END\n")


class VendorMetricTests(unittest.TestCase):
    def test_enflame_summary_capacity_is_not_allocated_memory(self):
        host = view("enflame")
        for card in host.cards:
            card.mem_used = 42976  # Old summary parser assigns Mem capacity here.
            card.mem_total = None
            card.procs_known = True
        augment_vendor_metrics(host, memory([memory_row(0), memory_row(1)]))
        self.assertEqual([(card.mem_used, card.mem_total) for card in host.cards], [(0, 42976)] * 2)
        self.assertEqual([_card(card)["state"] for card in host.cards], ["free", "free"])

    def test_enflame_partial_invalid_or_missing_json_never_reports_empty(self):
        values = ["", "##FL-ENFLAME-MEMORY\nfl-dashboard-enflame-memory-ok\n{bad\n##FL-VENDOR-END",
                  memory([memory_row(0)]), memory([memory_row(0), memory_row(0)]),
                  memory([memory_row(0), memory_row(1, used=-1)]),
                  memory([memory_row(0), memory_row(1, used=50000)]),
                  memory([memory_row(0), memory_row(1, total=True)]),
                  memory([memory_row(0), memory_row(1)]).replace("memory-ok", "memory-unavailable")]
        for raw in values:
            with self.subTest(raw=raw):
                host = view("enflame")
                for card in host.cards: card.procs_known = True
                augment_vendor_metrics(host, raw)
                self.assertEqual([_card(card)["state"] for card in host.cards], ["unknown", "unknown"])
                self.assertTrue(all(card.mem_used is None for card in host.cards))

    def test_enflame_missing_process_evidence_still_prevents_free(self):
        host = view("enflame")
        augment_vendor_metrics(host, memory([memory_row(0), memory_row(1)]))
        self.assertTrue(all(_card(card)["state"] == "unknown" for card in host.cards))

    def test_enflame_live_process_stays_busy_without_memory_details(self):
        host = view("enflame")
        host.cards[0].procs = [Proc(pid=123, user=None, name="python")]
        augment_vendor_metrics(host, "")
        self.assertEqual(_card(host.cards[0])["state"], "busy")
        self.assertEqual(_card(host.cards[1])["state"], "unknown")

    def test_thead_blank_process_names_are_still_occupied(self):
        host = view("thead", 3)
        raw = """##THEAD
| Processes: |
| PPU    GI   CI   PID      Type  Process name          PPU Memory |
+===============================================================+
| 0      N/A  N/A  12345    C                             1332MiB |
| 0      N/A  N/A  12346    C       python                1492MiB |
| 1      N/A  N/A  12347    C                              128MiB |
+---------------------------------------------------------------+
"""
        augment_vendor_metrics(host, raw)
        self.assertEqual([_card(card)["state"] for card in host.cards], ["busy", "busy", "free"])
        self.assertEqual([p.pid for p in host.cards[0].procs], [12345, 12346])
        self.assertEqual(host.cards[0].procs[0].mem_mib, 1332)
        self.assertEqual(host.cards[0].procs[0].name, "未知进程")

    def test_thead_truncated_or_unknown_process_row_does_not_prove_free(self):
        prefix = "##THEAD\n| Processes: |\n| PPU GI CI PID Type Process name PPU Memory |\n"
        for raw in (prefix, prefix + "| 0 N/A N/A 123 C 2MiB |",
                    prefix + "| 0 N/A N/A 123 C malformed row |\n+---------+",
                    prefix + "| 99 N/A N/A 123 C 2MiB |\n+---------+"):
            host = view("thead")
            augment_vendor_metrics(host, raw)
            self.assertTrue(all(_card(card)["state"] == "unknown" for card in host.cards))

    def test_hygon_processes_use_explicit_hcu_indices(self):
        host = view("hygon", 4)
        host.cards[0].procs = [Proc(pid=123, user="tester", name="python"), Proc(pid=456, user="tester", name="worker")]
        host.cards[0].procs_shared = True
        augment_vendor_metrics(host, pids([(123, [], 0), (456, [1])]))
        self.assertEqual([_card(card)["state"] for card in host.cards], ["free", "busy", "free", "free"])
        self.assertFalse(any(card.procs_shared for card in host.cards))
        self.assertEqual(host.cards[1].procs[0].name, "worker")
        self.assertEqual(host.cards[1].procs[0].mem_mib, 128)

    def test_hygon_positive_unassigned_vram_keeps_six_cards_unknown(self):
        host = view("hygon", 8)
        host.cards[0].procs = [Proc(pid=123, user="tester", name="context")]
        host.cards[0].procs_shared = True
        augment_vendor_metrics(host, pids([(123, [], 4), (124, [], 4), (125, [], 4),
                                          (456, [0], 60418), (457, [1], 60417)]))
        self.assertEqual([_card(card)["state"] for card in host.cards],
                         ["busy", "busy"] + ["unknown"] * 6)
        self.assertEqual([card.procs_known for card in host.cards], [True, True] + [False] * 6)
        self.assertFalse(host.cards[0].procs_shared)
        self.assertFalse(host.cards[1].procs_shared)
        shared = [card for card in host.cards if card.procs_shared]
        self.assertEqual(len(shared), 1)
        self.assertEqual([proc.pid for proc in shared[0].procs], [123, 124, 125])
        self.assertEqual(shared[0].procs[0].name, "context")
        self.assertEqual(shared[0].procs[0].mem_mib, 4)

    def test_hygon_missing_or_invalid_unassigned_vram_cannot_prove_free(self):
        for used in (None, "", "N/A", "nan", "inf", -1, 2**41):
            with self.subTest(used=used):
                host = view("hygon")
                augment_vendor_metrics(host, pids([(123, [], used), (456, [1], 128)]))
                self.assertEqual([_card(card)["state"] for card in host.cards], ["unknown", "busy"])
                self.assertTrue(host.cards[0].procs_shared)
                self.assertFalse(host.cards[0].procs_known)
                self.assertIsNone(host.cards[0].procs[0].mem_mib)
                self.assertFalse(host.cards[1].procs_shared)

    def test_hygon_all_cards_mapped_keep_busy_and_unassigned_process_visible(self):
        host = view("hygon")
        augment_vendor_metrics(host, pids([(123, [], 4), (456, [0]), (457, [1])]))
        self.assertEqual([_card(card)["state"] for card in host.cards], ["busy", "busy"])
        self.assertTrue(all(card.procs_known for card in host.cards))
        self.assertTrue(host.cards[0].procs_shared)
        self.assertEqual([proc.pid for proc in host.cards[0].procs], [456, 123])
        self.assertFalse(host.cards[1].procs_shared)

    def test_vendor_sections_excludes_other_families_and_prefix_collisions(self):
        raw = ("outside\n##NVIDIA\nforeign\n##THEAD\nowned\n"
               "##THEAD-PROCESSES\nowned-child\n##THEADX\ncollision\n"
               "##FL-THEAD\nsupplement\n##SYS\nsystem")
        scoped = vendor_sections(raw, "thead")
        self.assertEqual(scoped, "##THEAD\nowned\n##THEAD-PROCESSES\nowned-child")
        self.assertEqual(vendor_sections(raw, "hygon"), "")

    def test_thead_does_not_consume_other_vendor_process_table(self):
        foreign = ("##NVIDIA\n| Processes: |\n"
                   "| PPU GI CI PID Type Process name PPU Memory |\n"
                   "| 0 N/A N/A 123 C 128MiB |\n+---------+\n")
        host = view("thead")
        augment_vendor_metrics(host, foreign + "##THEAD\nno process evidence")
        self.assertTrue(all(_card(card)["state"] == "unknown" for card in host.cards))
        own = foreign.replace("##NVIDIA", "##THEAD").replace("123 C", "456 C")
        augment_vendor_metrics(host, foreign + own)
        self.assertEqual([_card(card)["state"] for card in host.cards], ["busy", "free"])
        self.assertEqual(host.cards[0].procs[0].pid, 456)

    def test_hygon_multi_card_process_does_not_duplicate_total_memory(self):
        host = view("hygon")
        augment_vendor_metrics(host, pids([(123, [0, 1])]))
        self.assertTrue(all(_card(card)["state"] == "busy" for card in host.cards))
        self.assertTrue(all(card.procs[0].mem_mib is None for card in host.cards))

    def test_hygon_incomplete_or_foreign_mapping_keeps_unknown(self):
        for raw in ("", pids([]), pids([(123, [99])]), pids([(123, [0]), (123, [1])]),
                    pids([(123, [0])]).replace("HCU Index:", "Unavailable:"),
                    pids([(123, [0])]).replace("End of SMI Log", ""),
                    pids([(123, [0])]).replace("['0']", "not-an-index")):
            host = view("hygon")
            augment_vendor_metrics(host, raw)
            self.assertTrue(all(_card(card)["state"] == "unknown" for card in host.cards))

    def test_fixed_probe_uses_read_only_documented_queries(self):
        script = probe_vendor_metrics()
        self.assertIn("timeout 6 efsmi -q -d MEMORY --json-format", script)
        self.assertIn('timeout 6 "$H" --showpids', script)
        self.assertNotIn("--reset", script)
        self.assertNotIn("cmdline", script)
        self.assertIn("bounded_head 4096", script)


if __name__ == "__main__":
    unittest.main()
