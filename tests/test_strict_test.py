"""Contract tests for strict_test.py; all subprocesses use a fake runner, no GPU."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "strict_test.py"
SPEC = importlib.util.spec_from_file_location("strict_test", SCRIPT)
assert SPEC and SPEC.loader
strict_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(strict_test)


FAKE_RUNNER = textwrap.dedent(
    """\
    import argparse
    import os
    import subprocess
    import sys
    import xml.etree.ElementTree as ET
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', required=True)
    parser.add_argument('--device', required=True)
    parser.add_argument('--scope', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--task')
    parser.add_argument('--model')
    parser.add_argument('--case')
    args = parser.parse_args()
    if args.scope == 'e2e':
        assert (args.task, args.model, args.case) == ('inference', 'qwen3', 'smoke')

    mode = os.environ.get('FAKE_STATUS', 'pass')
    if mode == 'no_case':
        sys.exit(0)
    modes = ['skip', 'pass'] if mode == 'overwrite_skip' else [mode]
    print(f'[run] Cases:    {len(modes)}', flush=True)
    suite = ET.Element('testsuite', tests=str(len(modes)))
    exit_code = 0
    for index, item in enumerate(modes):
        command = [sys.executable, '-m', 'pytest', item]
        print(f"[run] Command: {' '.join(command)}", flush=True)
        result = subprocess.run(command, check=False)
        exit_code = max(exit_code, result.returncode)
        case = ET.SubElement(suite, 'testcase', name=f'runner-{index}')
        if result.returncode:
            ET.SubElement(case, 'failure')
    report = Path(args.output_dir) / f'test-results-{args.platform}.xml'
    ET.ElementTree(suite).write(report, encoding='utf-8', xml_declaration=True)
    sys.exit(exit_code)
    """
)


FAKE_PYTEST = textwrap.dedent(
    """\
    import os
    import shlex
    import sys
    import xml.etree.ElementTree as ET
    from pathlib import Path

    token = next(
        part for part in shlex.split(os.environ['PYTEST_ADDOPTS'])
        if part.startswith('--junitxml=')
    )
    junit = Path(token.split('=', 1)[1])
    suite = ET.Element('testsuite', tests='1')
    case = ET.SubElement(suite, 'testcase', name=sys.argv[1])
    if sys.argv[1] == 'skip':
        ET.SubElement(case, 'skipped')
    elif sys.argv[1] == 'fail':
        ET.SubElement(case, 'failure')
    ET.ElementTree(suite).write(junit, encoding='utf-8', xml_declaration=True)
    sys.exit(1 if sys.argv[1] == 'fail' else 0)
    """
)


class StrictTestContract(unittest.TestCase):
    def test_junit_atomic_counts_and_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "junit.xml"
            path.write_text(
                "<testsuite tests='5'>"
                "<testcase name='p1'/><testcase name='p2'/>"
                "<testcase name='s'><skipped/></testcase>"
                "<testcase name='f'><failure/></testcase>"
                "<testcase name='e'><error/></testcase>"
                "</testsuite>",
                encoding="utf-8",
            )
            self.assertEqual(
                strict_test.count_junit(path),
                {
                    "collected": 5,
                    "executed": 4,
                    "passed": 2,
                    "failed": 2,
                    "errors": 1,
                    "skipped": 1,
                },
            )
            path.write_text("<testsuite tests='0'/>", encoding="utf-8")
            self.assertEqual(strict_test.count_junit(path)["executed"], 0)

    def test_e2e_case_list_must_be_explicit_nonempty_and_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            with self.assertRaisesRegex(ValueError, "requires --cases-json"):
                strict_test.load_cases("e2e", None)
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nonempty JSON array"):
                strict_test.load_cases("e2e", str(path))
            one = {"task": "inference", "model": "qwen3", "case": "smoke"}
            path.write_text(json.dumps([one, one]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate e2e case"):
                strict_test.load_cases("e2e", str(path))
            path.write_text(json.dumps([one]), encoding="utf-8")
            self.assertEqual(strict_test.load_cases("e2e", str(path)), [one])
            with self.assertRaisesRegex(ValueError, "only valid"):
                strict_test.load_cases("unit", str(path))

    def _fake_repo(self, root: Path) -> tuple[Path, str]:
        repo = root / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "vllm").mkdir()
        (repo / "vllm" / "__init__.py").write_text(
            "__version__ = '0.24.0'\n", encoding="utf-8"
        )
        (repo / "tests" / "run.py").write_text(FAKE_RUNNER, encoding="utf-8")
        (repo / "pytest.py").write_text(FAKE_PYTEST, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            [
                "git", "-c", "user.email=test@example.invalid",
                "-c", "user.name=Test", "commit", "-qm", "init",
            ],
            cwd=repo, capture_output=True, check=True,
        )
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        return repo, head

    def _invoke(
        self, root: Path, repo: Path, head: str, scope: str, mode: str
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        evidence = root / f"{scope}-{mode}.json"
        command = [
            sys.executable, str(SCRIPT),
            "--platform", "cuda", "--device", "a100",
            "--scope", scope,
            "--evidence-file", str(evidence),
            "--target-vllm-commit", "a" * 40,
            "--plugin-commit", head,
        ]
        if scope == "e2e":
            case_file = root / "cases.json"
            case_file.write_text(
                json.dumps([{"task": "inference", "model": "qwen3", "case": "smoke"}]),
                encoding="utf-8",
            )
            command += ["--cases-json", str(case_file)]
        env = {
            **os.environ,
            "FLFLOW_TARGET_VLLM_REF": "v0.24.0",
            "FAKE_STATUS": mode,
        }
        process = subprocess.run(
            command, cwd=repo, env=env, capture_output=True, text=True, check=False
        )
        return process, json.loads(evidence.read_text(encoding="utf-8"))

    def test_fake_runner_accepts_e2e_and_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = self._fake_repo(root)
            for scope in ("e2e", "benchmark"):
                with self.subTest(scope=scope):
                    process, evidence = self._invoke(root, repo, head, scope, "pass")
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertEqual(evidence["status"], "passed")
                    self.assertEqual(
                        (evidence["executed"], evidence["passed"],
                         evidence["failed"], evidence["skipped"]),
                        (1, 1, 0, 0),
                    )
                    self.assertTrue(evidence["plugin_commit_verified"])
                    self.assertEqual(evidence["plugin_head"], head)
                    self.assertEqual(evidence["installed_vllm_version"], "0.24.0")
                    self.assertFalse(evidence["vllm_commit_verified"])
                    self.assertTrue(evidence["cases"][0]["pytest_invocations_verified"])

    def test_fake_runner_rejects_skip_and_no_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = self._fake_repo(root)
            for mode, expected_skipped in (("skip", 1), ("no_case", 0)):
                with self.subTest(mode=mode):
                    process, evidence = self._invoke(root, repo, head, "e2e", mode)
                    self.assertEqual(process.returncode, 1)
                    self.assertEqual(evidence["status"], "failed")
                    self.assertEqual(evidence["skipped"], expected_skipped)
                    self.assertEqual(evidence["executed"], 0)
                    self.assertTrue(evidence["issues"])

    def test_overwritten_earlier_skip_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, head = self._fake_repo(root)
            process, evidence = self._invoke(root, repo, head, "benchmark", "overwrite_skip")
            self.assertEqual(process.returncode, 1)
            self.assertEqual(evidence["status"], "failed")
            # The second fake pytest process overwrites the first process's
            # skipped JUnit testcase with a passing one.
            self.assertEqual(evidence["passed"], 1)
            self.assertEqual(evidence["skipped"], 0)
            self.assertFalse(evidence["cases"][0]["pytest_invocations_verified"])
            self.assertIn("multiple pytest subprocesses", " ".join(evidence["issues"]))


if __name__ == "__main__":
    unittest.main()
