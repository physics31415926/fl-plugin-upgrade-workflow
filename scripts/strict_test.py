#!/usr/bin/env python3
"""Run the FL test runner with an atomic pytest/JUnit acceptance gate.

Invoke this script from a vllm-plugin-FL worktree. ``tests/run.py`` may spawn
several pytest subprocesses per invocation, and their shared JUnit path would
be overwritten. This adapter accepts only a single pytest subprocess per
invocation and fails closed when the runner discovers more than one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def count_junit(path: Path) -> dict[str, int]:
    """Count pytest testcases, classifying errors as failed tests."""
    root = ET.parse(path).getroot()
    if _local_name(root.tag) not in {"testsuite", "testsuites"}:
        raise ValueError(f"unexpected JUnit root: {root.tag}")

    cases = [node for node in root.iter() if _local_name(node.tag) == "testcase"]
    passed = failures = errors = skipped = 0
    for case in cases:
        outcomes = [
            _local_name(child.tag)
            for child in case
            if _local_name(child.tag) in {"failure", "error", "skipped"}
        ]
        if len(outcomes) > 1:
            raise ValueError(f"multiple outcomes for JUnit testcase {case.get('name')!r}")
        if not outcomes:
            passed += 1
        elif outcomes[0] == "failure":
            failures += 1
        elif outcomes[0] == "error":
            errors += 1
        else:
            skipped += 1

    collected = len(cases)
    return {
        "collected": collected,
        "executed": collected - skipped,
        "passed": passed,
        "failed": failures + errors,
        "errors": errors,
        "skipped": skipped,
    }


def runner_invocation_count(log_path: Path, report_path: Path) -> int:
    """Verify the runner launched exactly one pytest subprocess.

    The runner prints one ``[run] Command:`` line before each subprocess and
    writes one report testcase per discovered TestCase. Both independent
    records must agree before a pytest JUnit file can be treated as complete.
    """
    log = log_path.read_text(encoding="utf-8", errors="replace")
    declared = re.findall(r"^\[run\] Cases:\s*(\d+)\s*$", log, flags=re.MULTILINE)
    commands = re.findall(r"^\[run\] Command:\s*(.+)$", log, flags=re.MULTILINE)
    if len(declared) != 1:
        raise ValueError("runner did not declare exactly one case count")
    if any(" -m pytest " not in f" {line} " for line in commands):
        raise ValueError("runner command record is not a pytest subprocess")
    if not report_path.is_file():
        raise ValueError("runner summary JUnit XML is missing")
    reported = count_junit(report_path)["collected"]
    counts = (int(declared[0]), len(commands), reported)
    if counts != (1, 1, 1):
        raise ValueError(
            "runner may have launched multiple pytest subprocesses or omitted "
            f"one: declared/commands/reported={counts}; shared JUnit XML "
            "cannot prove all outcomes"
        )
    return 1


def load_cases(scope: str, cases_json: str | None) -> list[dict[str, str]]:
    if scope != "e2e":
        if cases_json is not None:
            raise ValueError("--cases-json is only valid with --scope e2e")
        return [{"task": scope, "model": "", "case": ""}]

    if not cases_json:
        raise ValueError("--scope e2e requires --cases-json")
    raw = json.loads(Path(cases_json).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("--cases-json must contain a nonempty JSON array")

    cases: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"case {index} must be a JSON object")
        values: dict[str, str] = {}
        for key in ("task", "model", "case"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"case {index} needs a nonempty string {key!r}")
            values[key] = value.strip()
        identity = (values["task"], values["model"], values["case"])
        if identity in seen:
            raise ValueError(f"duplicate e2e case: {'/'.join(identity)}")
        seen.add(identity)
        cases.append(values)
    return cases


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")[:60] or "case"


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot read plugin worktree HEAD: {result.stderr.strip()}")
    return result.stdout.strip().lower()


def _expected_vllm_version(ref: str, explicit: str | None) -> str:
    # Normal vLLM release tags have a three-component version. A branch,
    # arbitrary ref, or commit cannot safely be converted to a package version.
    standard = re.fullmatch(
        r"v?[0-9]+\.[0-9]+\.[0-9]+(?:a[0-9]+|b[0-9]+|rc[0-9]+|\.post[0-9]+|\.dev[0-9]+)?",
        ref,
    )
    if standard:
        expected = ref.removeprefix("v")
        if explicit and explicit.removeprefix("v").strip() != expected:
            raise ValueError(
                "--expected-installed-version cannot override the release version "
                f"in FLFLOW_TARGET_VLLM_REF ({ref})"
            )
        return expected
    if not explicit or not explicit.strip():
        raise ValueError(
            "FLFLOW_TARGET_VLLM_REF is not a release tag; "
            "pass --expected-installed-version explicitly"
        )
    return explicit.removeprefix("v").strip()


def _installed_vllm_version(repo: Path) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import vllm; print('FLFLOW_VLLM_VERSION=' + str(vllm.__version__))",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise ValueError(f"cannot import installed vllm: {detail}")
    matches = [
        line.partition("=")[2].strip()
        for line in result.stdout.splitlines()
        if line.startswith("FLFLOW_VLLM_VERSION=")
    ]
    if len(matches) != 1 or not matches[0]:
        raise ValueError("installed vllm did not report one version")
    return matches[0]


def run_one(
    repo: Path,
    artifacts_dir: Path,
    platform: str,
    device: str,
    scope: str,
    index: int,
    case: dict[str, str],
) -> dict[str, Any]:
    label = "-".join(filter(None, (case["task"], case["model"], case["case"])))
    case_dir = artifacts_dir / f"{index:03d}-{_slug(label)}"
    case_dir.mkdir(parents=True, exist_ok=False)
    junit_path = case_dir / "pytest-junit.xml"
    runner_report_path = case_dir / f"test-results-{platform}.xml"
    log_path = case_dir / "console.log"

    command = [
        sys.executable,
        str(repo / "tests" / "run.py"),
        "--platform", platform,
        "--device", device,
        "--scope", scope,
        "--output-dir", str(case_dir),
    ]
    if scope == "e2e":
        command.extend([
            "--task", case["task"],
            "--model", case["model"],
            "--case", case["case"],
        ])

    env = os.environ.copy()
    # Replace inherited pytest addopts: a caller's -k, --collect-only, etc.
    # must not silently reduce the acceptance test set.
    env["PYTEST_ADDOPTS"] = f"--junitxml={shlex.quote(str(junit_path))}"
    result: dict[str, Any] = {
        **case,
        "collected": 0,
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "exit_code": None,
        "junit": str(junit_path),
        "runner_report": str(runner_report_path),
        "pytest_invocations_verified": False,
        "log": str(log_path),
        "issues": [],
    }

    print(f"[strict] Running {label} on {platform}/{device}", flush=True)
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                command,
                cwd=repo,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                log.write(line)
            result["exit_code"] = process.wait()
    except OSError as exc:
        result["issues"].append(f"could not start test runner: {exc}")

    try:
        runner_invocation_count(log_path, runner_report_path)
        result["pytest_invocations_verified"] = True
    except (ET.ParseError, OSError, ValueError) as exc:
        result["issues"].append(f"cannot verify one pytest subprocess: {exc}")

    if junit_path.is_file():
        try:
            result.update(count_junit(junit_path))
        except (ET.ParseError, OSError, ValueError) as exc:
            result["issues"].append(f"invalid pytest JUnit XML: {exc}")
    else:
        result["issues"].append("pytest JUnit XML is missing (possibly no discovered case)")

    if result["exit_code"] != 0:
        result["issues"].append(f"test runner exited with {result['exit_code']}")
    if result["executed"] == 0:
        result["issues"].append("zero tests executed")
    if result["skipped"]:
        result["issues"].append(f"{result['skipped']} pytest tests skipped")
    if result["failed"]:
        result["issues"].append(f"{result['failed']} pytest tests failed or errored")
    return result


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--scope", choices=("unit", "functional", "e2e", "benchmark"), required=True)
    parser.add_argument("--cases-json", help="Required for e2e: nonempty array of task/model/case objects")
    parser.add_argument("--evidence-file", required=True)
    parser.add_argument("--target-vllm-commit", required=True)
    parser.add_argument("--plugin-commit", required=True)
    parser.add_argument(
        "--expected-installed-version",
        help="Required when FLFLOW_TARGET_VLLM_REF is not a release tag",
    )
    args = parser.parse_args(argv)

    evidence_path = Path(args.evidence_file).resolve()
    if evidence_path.suffix.lower() != ".json":
        parser.error("--evidence-file must end in .json")
    if evidence_path == Path(__file__).resolve():
        parser.error("--evidence-file cannot name this script")
    if args.cases_json and Path(args.cases_json).resolve() == evidence_path:
        parser.error("--cases-json and --evidence-file must be different files")
    # A previous successful report must never survive a new failed invocation.
    evidence_path.unlink(missing_ok=True)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "platform": args.platform,
        "device": args.device,
        "scope": args.scope,
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "target_vllm_commit": args.target_vllm_commit,
        "plugin_commit": args.plugin_commit,
        "plugin_head": None,
        "plugin_commit_verified": False,
        "target_vllm_ref": os.environ.get("FLFLOW_TARGET_VLLM_REF", "").strip(),
        "expected_installed_version": None,
        "installed_vllm_version": None,
        "vllm_commit_verified": False,
        "vllm_commit_verification": "Installed vLLM source commit is not established by package version",
        "status": "failed",
        "cases": [],
        "issues": [],
    }

    try:
        for name, value in (
            ("--target-vllm-commit", args.target_vllm_commit),
            ("--plugin-commit", args.plugin_commit),
        ):
            if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value):
                raise ValueError(f"{name} must be a full commit SHA (40 or 64 hex characters)")
        cases = load_cases(args.scope, args.cases_json)
        repo = Path.cwd().resolve()
        if not (repo / "tests" / "run.py").is_file():
            raise ValueError(f"tests/run.py not found in worktree {repo}")
        evidence["plugin_head"] = _git_head(repo)
        if evidence["plugin_head"] != args.plugin_commit.lower():
            raise ValueError(
                f"plugin worktree HEAD {evidence['plugin_head']} "
                f"does not equal --plugin-commit {args.plugin_commit.lower()}"
            )
        evidence["plugin_commit_verified"] = True
        expected_version = _expected_vllm_version(
            evidence["target_vllm_ref"], args.expected_installed_version
        )
        evidence["expected_installed_version"] = expected_version
        installed_version = _installed_vllm_version(repo)
        evidence["installed_vllm_version"] = installed_version
        if installed_version.split("+", 1)[0] != expected_version:
            raise ValueError(
                f"installed vllm {installed_version!r} does not match "
                f"target version {expected_version!r}"
            )

        artifacts_dir = evidence_path.parent / f"{evidence_path.stem}-artifacts-{uuid.uuid4().hex[:12]}"
        artifacts_dir.mkdir(parents=True, exist_ok=False)
        evidence["artifacts_dir"] = str(artifacts_dir)
        for index, case in enumerate(cases, start=1):
            case_result = run_one(
                repo, artifacts_dir, args.platform, args.device, args.scope, index, case
            )
            evidence["cases"].append(case_result)
            for key in ("executed", "passed", "failed", "skipped"):
                evidence[key] += case_result[key]
            if case_result["issues"]:
                identity = "/".join(filter(None, (case["task"], case["model"], case["case"])))
                evidence["issues"].append(f"{identity}: {'; '.join(case_result['issues'])}")
        if evidence["executed"] == 0:
            evidence["issues"].append("no atomic pytest tests executed")
        if not evidence["issues"]:
            evidence["status"] = "passed"
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        evidence["issues"].append(str(exc))

    _write_json(evidence_path, evidence)
    print(f"[strict] {evidence['status']}: {evidence_path}", flush=True)
    for issue in evidence["issues"]:
        print(f"[strict] {issue}", file=sys.stderr, flush=True)
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
