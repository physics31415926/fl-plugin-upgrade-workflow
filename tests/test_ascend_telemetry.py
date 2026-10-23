"""Offline Ascend probe execution: discovery, safe errors, and vendor isolation."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from fl_dashboard.resources.ascend import adapt_ascend_probe
from fl_dashboard.resources.telemetry import COMPLETE, parse_telemetry, probe_script
from fl_dashboard.vendor.jms_top.probe import ProbeLimits, build_script


TABLE = """| 0     Ascend910           | OK            | 168.7       53                0 / 0 |
| 0     0                   | 0000:01:00.0  | 0           0 / 0            4 / 65536 |
| 0     Ascend910           | OK            | -           52                0 / 0 |
| 1     1                   | 0000:01:00.0  | 0           0 / 0            1024 / 65536 |
Processes:
| NPU Chip | Process id | Process name | Memory |
| 0 1 | 4242 | python | 1024 |
"""
NVIDIA = "##NVIDIA\n0, NVIDIA A100, 0, 4, 40960, 30, 60, 00000000:01:00.0\nfl-dashboard-process-ok:nvidia\n"


def host(body):
    return parse_telemetry("fixture", "jms-top-ok\n" + body + "\n" + COMPLETE + "\n")[0]


class AscendTelemetryTests(unittest.TestCase):
    def execute(self, *, available=("path",), stdout=TABLE, stderr="", code=0):
        bash = (str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
                if os.name == "nt" else shutil.which("bash"))
        if not bash or not Path(bash).is_file():
            self.skipTest("bash unavailable for fixed-probe verification")
        scratch = Path(tempfile.gettempdir())
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ascend-probe-", dir=scratch) as folder:
            root = Path(folder)
            tools = root / "path tools"
            tools.mkdir()
            for command in ("timeout", "head", "awk"):
                path = tools / command
                path.write_text('#!/bin/sh\nexec /usr/bin/' + command + ' "$@"\n', encoding="utf-8")
                path.chmod(0o700)
            (root / "stdout").write_text(stdout, encoding="utf-8")
            (root / "stderr").write_text(stderr, encoding="utf-8")
            directories = {"path": tools, "bin": root / "fallback bin", "sbin": root / "fallback sbin"}
            for source in available:
                directory = directories[source]
                directory.mkdir(exist_ok=True)
                utility = directory / "npu-smi"
                utility.write_text(
                    '#!/bin/sh\nprintf "%s %s\\n" "' + source + '" "$*" >> "$PWD/calls"\n'
                    '/usr/bin/cat "$PWD/stdout"\n/usr/bin/cat "$PWD/stderr" >&2\n'
                    'exit ' + str(code) + '\n', encoding="utf-8")
                utility.chmod(0o700)
            # Execute the production Ascend section, including its final bounds,
            # while excluding every other vendor and all real installation paths.
            generated = probe_script().splitlines()
            bounded = next(line for line in generated if line.startswith("bounded_head()"))
            query = next(line for line in generated if line.startswith("A=$(command -v npu-smi"))
            query = query.replace("/usr/local/bin/npu-smi", '"$PWD/fallback bin/npu-smi"')
            query = query.replace("/usr/local/sbin/npu-smi", '"$PWD/fallback sbin/npu-smi"')
            self.assertNotIn("/usr/local/", query)
            script = 'export PATH="$PWD/path tools"; ' + bounded + "\n" + query
            result = subprocess.run([bash, "--noprofile", "--norc", "-c", script], cwd=root,
                                    capture_output=True, timeout=12,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(b"", result.stderr)
            calls = (root / "calls").read_text(encoding="utf-8") if (root / "calls").exists() else ""
            return result.stdout.decode("utf-8"), calls

    def test_adapter_changes_only_ascend_and_preserves_bound(self):
        original = build_script(limits=ProbeLimits(tile_n=37))
        adapted = adapt_ascend_probe(original)
        changed = [(before, after) for before, after in zip(original.splitlines(), adapted.splitlines())
                   if before != after]
        self.assertEqual(1, len(changed))
        self.assertIn("command -v npu-smi", changed[0][0])
        self.assertIn("head -37", changed[0][1])
        self.assertIn('timeout -k 1s 6s "$A" info', probe_script())
        for invalid in ("no probe", original + original, original.replace("head -37", "head missing")):
            with self.subTest(invalid=invalid[:20]), self.assertRaises(RuntimeError):
                adapt_ascend_probe(invalid)

    def test_path_wins_and_success_preserves_chips_and_processes(self):
        raw, calls = self.execute(available=("path", "bin", "sbin"))
        self.assertEqual("path info\n", calls)
        result = host(raw)
        self.assertEqual("ascend", result["backend"])
        self.assertTrue(result["collection_complete"])
        self.assertEqual((2, 1, 1, 1), tuple(result["accel"][key]
                                          for key in ("total", "free", "busy", "physical_cards")))
        self.assertEqual([0, 0], [card["card"] for card in result["accel"]["cards"]])
        self.assertEqual(4242, result["accel"]["cards"][1]["procs"][0]["pid"])

    def test_both_fallback_paths_work_without_path_entry(self):
        for available, expected in ((("bin", "sbin"), "bin"), (("sbin",), "sbin")):
            with self.subTest(source=expected):
                raw, calls = self.execute(available=available)
                self.assertEqual(expected + " info\n", calls)
                self.assertEqual(2, host(raw)["accel"]["total"])

    def test_stderr_initialization_failure_is_sanitized(self):
        error = "DrvMngGetConsoleLogLevel failed. (ret=4)\ndcmi module initialize failed. ret is -8005\nfixture-private-value"
        raw, _ = self.execute(stdout=TABLE, stderr=error, code=187)
        self.assertEqual("##ASCEND\n##STALLED:ascend:initialization-failed\n", raw)
        result = host(raw)
        self.assertIn("npu-smi 初始化失败", result["error"])
        self.assertFalse(result["collection_complete"])
        self.assertEqual(0, result["accel"]["total"])
        self.assertEqual(0, result["accel"]["free"])

    def test_timeout_exit_codes_are_distinguished(self):
        for code in (124, 137):
            with self.subTest(code=code):
                raw, _ = self.execute(stdout=TABLE, stderr="fixture-private-value", code=code)
                self.assertEqual("##ASCEND\n##STALLED:ascend:tool-timeout\n", raw)
                self.assertIn("npu-smi 查询超时", host(raw)["error"])
                self.assertEqual(0, host(raw)["accel"]["free"])

    def test_other_nonzero_query_discards_residual_cards_and_secrets(self):
        raw, _ = self.execute(stdout=TABLE + "fixture-private-stdout", stderr="fixture-private-stderr", code=7)
        self.assertEqual("##ASCEND\n##STALLED:ascend:query-failed\n", raw)
        result = host(raw)
        self.assertIn("npu-smi info 查询失败", result["error"])
        self.assertEqual([], result["accel"]["cards"])
        self.assertFalse(result["collection_complete"])

    def test_failures_are_isolated_to_the_actual_vendor(self):
        failure, _ = self.execute(stdout="", stderr="dcmi module initialize failed", code=187)
        result = host(failure + NVIDIA)
        self.assertEqual("cuda", result["backend"])
        self.assertTrue(result["collection_complete"])
        self.assertIsNone(result["error"])
        self.assertEqual(1, result["accel"]["free"])
        result = host("##NVIDIA\n##STALLED:nvidia:query-failed\n##ASCEND\n" + TABLE)
        self.assertEqual("ascend", result["backend"])
        self.assertTrue(result["collection_complete"])
        self.assertIsNone(result["error"])
        self.assertEqual(1, result["accel"]["free"])


if __name__ == "__main__":
    unittest.main()
