"""MetaX query compatibility using synthetic complete eight-card SMI replies."""
from pathlib import Path
import math
import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from fl_dashboard.resources.metax import adapt_metax_probe, metax_metrics_complete
from fl_dashboard.vendor.jms_top.families import Card, MetaX
from fl_dashboard.vendor.jms_top.probe import ProbeLimits, build_script


def fixture_queries(count=8):
    memory, usage = ["Attached GPUs : " + str(count)], ["Attached GPUs : " + str(count)]
    for index in range(count):
        header = f"GPU#{index} MXC550 0000:{index+1:02x}:00.0"
        memory.extend([header, "    Memory", "        vis_vram total : 67108864 KB",
                       "        vis_vram used : 4096 KB"] + ["        unused counter : 123"] * 10)
        usage.extend([header, "    Utilization", "        GPU : 0 %", "        VPUE : 0 %", "        VPUD : 0 %", ""])
    return "\n".join(memory) + "\n", "\n".join(usage) + "\n", "Process:\nNo running processes\nEnd of Log\n"


class MetaXTelemetryTests(unittest.TestCase):
    def body(self):
        return build_script(limits=ProbeLimits(top_n=0, tile_n=260, half=90))

    def test_only_metax_queries_are_compacted_and_other_families_are_unchanged(self):
        original = self.body()
        adapted = adapt_metax_probe(original)
        before, after = original.splitlines(), adapted.splitlines()
        changed = [(a, b) for a, b in zip(before, after) if a != b]
        self.assertEqual(1, len(changed))
        self.assertIn("M=$(command -v mx-smi", changed[0][0])
        self.assertEqual(3, changed[0][1].count("head -2048"))
        self.assertIn("vis_vram", changed[0][1])
        self.assertIn("Attached GPUs", changed[0][1])
        self.assertIn("##STALLED:metax:6s", changed[0][1])

    def test_bounded_existing_probe_and_failure_markers_are_preserved(self):
        original = self.body().replace("head -90", "bounded_head 90")
        original = original.replace('m=$($T "$M" --show-memory 2>/dev/null);',
                                    'm=$($T "$M" --show-memory 2>/dev/null); echo "##STALLED:metax:query-failed";')
        adapted = adapt_metax_probe(original)
        self.assertIn("bounded_head 2048", adapted)
        self.assertIn("##STALLED:metax:query-failed", adapted)
        self.assertEqual(original.endswith("\n"), adapted.endswith("\n"))
        for body in ("no probe", original + "\n" + original):
            with self.assertRaises(RuntimeError):
                adapt_metax_probe(body)

    def test_compacted_eight_card_queries_keep_all_memory_and_utilization(self):
        bash = str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe") if os.name == "nt" else shutil.which("bash")
        if not bash or not Path(bash).is_file():
            self.skipTest("bash unavailable for generated fixed-probe verification")
        memory, usage, processes = fixture_queries()
        self.assertGreater(len(memory.splitlines()), 90)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, text in (("memory", memory), ("usage", usage), ("processes", processes)):
                (root / name).write_text(text, encoding="utf-8")
            utility = root / "mx-smi"
            utility.write_text('#!/bin/sh\ncase "$1" in\n--show-memory) cat memory;;\n--show-usage) cat usage;;\n--show-process) cat processes;;\nesac\n', encoding="utf-8")
            utility.chmod(0o700)
            line = next(row for row in adapt_metax_probe(self.body()).splitlines() if "M=$(command -v mx-smi" in row)
            line = line.replace("T=timeout 6;", "T='timeout 6';")
            completed = subprocess.run([bash, "--noprofile", "--norc", "-c", 'export PATH="$PWD:$PATH"; ' + line],
                                       cwd=root, capture_output=True, timeout=12,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(0, completed.returncode)
            raw = completed.stdout.decode("utf-8")
            self.assertLess(len(raw.splitlines()), 90)
            view = MetaX().parse(raw)
            self.assertEqual(list(range(8)), [card.index for card in view.cards])
            self.assertTrue(all(card.util == 0 and card.mem_total == 65536 and card.mem_used == 4 for card in view.cards))
            self.assertTrue(metax_metrics_complete(view, raw))
            self.assertNotIn("unused counter", raw)

    def test_count_mismatch_missing_or_invalid_card_metrics_are_incomplete(self):
        card = Card(index=0, util=0, mem_used=4, mem_total=65536)
        view = SimpleNamespace(cards=[card])
        self.assertTrue(metax_metrics_complete(view, "Attached GPUs : 1\n"))
        self.assertFalse(metax_metrics_complete(view, "Attached GPUs : 8\n"))
        self.assertFalse(metax_metrics_complete(SimpleNamespace(cards=[card, card]), ""))
        for values in ((None, 4, 65536), (0, None, 65536), (0, 4, None),
                       (0, 4, 0), (0, 65537, 65536), (101, 4, 65536),
                       (math.nan, 4, 65536), (True, 4, 65536), (10**1000, 4, 65536)):
            with self.subTest(values=values):
                invalid = Card(index=0, util=values[0], mem_used=values[1], mem_total=values[2])
                self.assertFalse(metax_metrics_complete(SimpleNamespace(cards=[invalid]), ""))


if __name__ == "__main__":
    unittest.main()
