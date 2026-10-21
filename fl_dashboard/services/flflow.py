#!/usr/bin/env python3
"""Local, auditable orchestration for a vllm-plugin-FL upgrade.

This tool executes only the argv commands explicitly supplied in its JSON config.
Each platform gets a separate, managed Git worktree. CUDA establishes the commit
used as the starting point for all other platforms.
"""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fl_dashboard.paths import APP_ROOT, TEMPLATES
from fl_dashboard.monitoring.branch_monitor import MonitorError
from fl_dashboard.projects.project_registry import ProjectRegistry, ProjectError


SCHEMA_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
TOKEN_RE = re.compile(r"\{([a-z_]+)\}")
POLL_SECONDS = 0.2
PROTECTED_CONFIG_SUFFIXES = (".json", ".yaml", ".yml", ".toml")


class WorkflowError(Exception):
    pass


class Cancelled(WorkflowError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"Expected a JSON object in {path}")
    return value


def canonical_config_sha256(config: dict) -> str:
    """Hash config meaning independent of JSON whitespace and key order."""
    try:
        canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"Config contains unsupported JSON values: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_protected_repo_path(path: str) -> bool:
    """Acceptance inputs may be inspected, but never silently rewritten by adaptation."""
    parts = [part.lower() for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts:
        return False
    name = parts[-1]
    if any(part in {"tests", "test", "acceptance", "benchmarks", "benchmark"} for part in parts):
        return True
    if parts[0] == ".github":
        return True
    if name in {"flflow.py", "strict_test.py", "conftest.py", "pytest.ini", "tox.ini", ".coveragerc", ".gitignore", "agents.md"}:
        return True
    if name.startswith("test_") and name.endswith(".py") or name.endswith(("_test.py", ".test.py")):
        return True
    if name.endswith(PROTECTED_CONFIG_SUFFIXES) and (
        name.startswith(("workflow", "cases", "acceptance", "strict_test")) or
        "acceptance" in name
    ):
        return True
    return False


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
    except OSError as exc:
        raise WorkflowError(f"Could not run git: {exc}") from exc
    if check and proc.returncode:
        raise WorkflowError(f"git {' '.join(args)} failed in {repo}: {(proc.stderr or proc.stdout).strip()}")
    return proc


def validate_config(path: Path) -> dict:
    raw = read_json(path)
    required = ("repo", "upstream_repo", "work_dir", "plugin_base_sha", "target_vllm_ref", "target_vllm_commit", "platforms")
    for key in required:
        if key not in raw:
            raise WorkflowError(f"Missing config key: {key}")
    cfg = dict(raw)
    cfg["_config_sha256"] = canonical_config_sha256(raw)
    for key in ("repo", "upstream_repo", "work_dir"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise WorkflowError(f"{key} must be a nonempty path")
        candidate = Path(raw[key]).expanduser()
        cfg[key] = (candidate if candidate.is_absolute() else path.resolve().parent / candidate).resolve()
    for key in ("repo", "upstream_repo"):
        if not cfg[key].is_dir():
            raise WorkflowError(f"{key} does not exist: {cfg[key]}")
        root = git(cfg[key], "rev-parse", "--show-toplevel").stdout.strip()
        if Path(root).resolve() != cfg[key]:
            raise WorkflowError(f"{key} must be a Git repository root: {cfg[key]}")
    if within(cfg["work_dir"], cfg["repo"]) or within(cfg["work_dir"], cfg["upstream_repo"]):
        raise WorkflowError("work_dir must be outside both Git repositories")
    for key in ("plugin_base_sha", "target_vllm_commit"):
        if not isinstance(raw[key], str) or not SHA_RE.fullmatch(raw[key]):
            raise WorkflowError(f"{key} must be a full 40-character Git SHA")
        cfg[key] = raw[key].lower()
    if not isinstance(raw["target_vllm_ref"], str) or not raw["target_vllm_ref"].strip():
        raise WorkflowError("target_vllm_ref must be nonempty")
    if not isinstance(raw["platforms"], list) or not raw["platforms"]:
        raise WorkflowError("platforms must be a nonempty array")
    names: set[str] = set()
    cuda_count = 0
    for platform in raw["platforms"]:
        if not isinstance(platform, dict):
            raise WorkflowError("Each platform must be an object")
        name, kind = platform.get("name"), platform.get("kind")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in names:
            raise WorkflowError(f"Invalid or duplicate platform name: {name!r}")
        names.add(name)
        if kind not in ("cuda", "backend"):
            raise WorkflowError(f"{name}: kind must be cuda or backend")
        cuda_count += kind == "cuda"
        _validate_argv(platform.get("adapt_command"), f"{name}.adapt_command")
        tests = platform.get("test_commands")
        if not isinstance(tests, list) or not tests:
            raise WorkflowError(f"{name}: test_commands must be a nonempty array")
        scopes: set[str] = set()
        for test in tests:
            if not isinstance(test, dict):
                raise WorkflowError(f"{name}: each test command must be an object")
            scope = test.get("scope")
            if not isinstance(scope, str) or not NAME_RE.fullmatch(scope) or scope in scopes:
                raise WorkflowError(f"{name}: invalid or duplicate test scope: {scope!r}")
            scopes.add(scope)
            _validate_argv(test.get("argv"), f"{name}.{scope}.argv")
            if not isinstance(test.get("evidence_file"), str) or not test["evidence_file"].strip():
                raise WorkflowError(f"{name}.{scope}: evidence_file is required")
            _validate_timeout(test.get("timeout_seconds"), f"{name}.{scope}")
        _validate_timeout(platform.get("adapt_timeout_seconds"), name)
        env = platform.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise WorkflowError(f"{name}: env must map strings to strings")
    if cuda_count != 1:
        raise WorkflowError("Exactly one platform must have kind=cuda")
    parallel = raw.get("max_parallel", len(raw["platforms"]))
    if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
        raise WorkflowError("max_parallel must be a positive integer")
    cfg["max_parallel"] = parallel
    return cfg


def _validate_argv(value: object, label: str) -> None:
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
        raise WorkflowError(f"{label} must be a nonempty array of nonempty strings")


def _validate_timeout(value: object, label: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
        raise WorkflowError(f"{label}: timeout_seconds must be positive")


def expand(value: str, context: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise WorkflowError(f"Unknown command placeholder: {{{key}}}")
        return context[key]
    return TOKEN_RE.sub(replace, value)


class Engine:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.cfg = validate_config(self.config_path)
        self.config_sha256 = self.cfg["_config_sha256"]
        tool_dir = APP_ROOT
        protected_tools = [tool_dir / "scripts" / "strict_test.py", *tool_dir.glob("cases*.json"), *(tool_dir / "examples").glob("cases*.json")]
        self.protected_tool_hashes = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected_tools if path.is_file()
        }
        self.protected_repo_paths: set[str] = set()
        if within(self.config_path, self.cfg["repo"]):
            self.protected_repo_paths.add(self.config_path.relative_to(self.cfg["repo"]).as_posix().lower())
        self.work_dir: Path = self.cfg["work_dir"]
        self.state_path = self.work_dir / "state.json"
        self.control_path = self.work_dir / "control.json"
        self.lock_path = self.work_dir / "run.lock"
        self.lock = threading.RLock()
        self.state: dict = {}
        self.active_commands = 0
        self.worker: threading.Thread | None = None

    def status(self) -> dict:
        with self.lock:
            state = read_json(self.state_path) if self.state_path.exists() else self._idle_state()
            if self.control_path.exists():
                try:
                    control = read_json(self.control_path)
                    if control.get("run_id") == state.get("run_id"):
                        state["requested_action"] = control.get("action")
                except WorkflowError:
                    pass
            return state

    def _idle_state(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION, "run_id": None, "status": "idle", "phase": "idle",
            "dry_run": False, "target_vllm_ref": self.cfg["target_vllm_ref"],
            "target_vllm_commit": self.cfg["target_vllm_commit"],
            "plugin_base_sha": self.cfg["plugin_base_sha"], "cuda_baseline_commit": None,
            "started_at": None, "finished_at": None, "error": None,
            "candidate_commit": None, "integration_required": False,
            "config_sha256": self.config_sha256, "latest_config_sha256": self.config_sha256,
            "config_hash_algorithm": "canonical-json-v1",
            "protected_tool_sha256": {str(path): digest for path, digest in self.protected_tool_hashes.items()},
            "platforms": {p["name"]: {"kind": p["kind"], "status": "pending", "attempts": []} for p in self.cfg["platforms"]},
        }

    def _save(self) -> None:
        with self.lock:
            atomic_json(self.state_path, self.state)

    def _set(self, **values: object) -> None:
        with self.lock:
            self.state.update(values)
            self._save()

    def _platform(self, name: str, **values: object) -> None:
        with self.lock:
            self.state["platforms"][name].update(values)
            self._save()

    def _attempt(self, name: str, **values: object) -> None:
        with self.lock:
            self.state["platforms"][name]["attempts"][-1].update(values)
            self._save()

    def _new_attempt(self, platform: dict) -> dict:
        name = platform["name"]
        with self.lock:
            number = len(self.state["platforms"][name]["attempts"]) + 1
            worktree = self.work_dir / "worktrees" / self.state["run_id"] / name / f"attempt-{number}"
            attempt = {
                "number": number, "status": "preparing", "worktree": str(worktree),
                "plugin_commit": None, "commands": [], "tests": {}, "error": None,
                "started_at": now(), "finished_at": None,
                "config_sha256": self.config_sha256,
            }
            self.state["platforms"][name]["attempts"].append(attempt)
            self.state["platforms"][name]["status"] = "preparing"
            self._save()
            return attempt

    def _control(self) -> str:
        if not self.control_path.exists():
            return "run"
        try:
            value = read_json(self.control_path)
        except WorkflowError:
            return "run"
        return value.get("action", "run") if value.get("run_id") == self.state.get("run_id") else "run"

    def request(self, action: str) -> dict:
        if action not in ("pause", "resume", "cancel"):
            raise WorkflowError(f"Unsupported action: {action}")
        state = self.status()
        if state["status"] not in ("running", "pausing", "paused", "cancelling"):
            raise WorkflowError(f"Cannot {action} a run with status {state['status']}")
        if action == "pause" and state["status"] == "cancelling":
            raise WorkflowError("Cannot pause a cancelling run")
        if action == "resume" and state["status"] not in ("pausing", "paused"):
            raise WorkflowError("Run is not paused")
        atomic_json(self.control_path, {"run_id": state["run_id"], "action": "run" if action == "resume" else action, "updated_at": now()})
        return self.status()

    def _gate(self) -> None:
        while True:
            action = self._control()
            if action == "cancel":
                if self.state.get("status") != "cancelling":
                    self._set(status="cancelling")
                raise Cancelled("Cancelled by user")
            if action != "pause":
                if self.state.get("status") in ("paused", "pausing"):
                    self._set(status="running")
                return
            desired = "pausing" if self.active_commands else "paused"
            if self.state.get("status") != desired:
                self._set(status=desired)
            time.sleep(POLL_SECONDS)

    def _preflight(self) -> None:
        plugin_head = git(self.cfg["repo"], "rev-parse", "HEAD").stdout.strip().lower()
        if plugin_head != self.cfg["plugin_base_sha"]:
            raise WorkflowError(f"Plugin HEAD {plugin_head} differs from configured plugin_base_sha {self.cfg['plugin_base_sha']}")
        if git(self.cfg["repo"], "status", "--porcelain").stdout.strip():
            raise WorkflowError("Plugin repo has uncommitted changes; start from a clean baseline")
        target = git(self.cfg["upstream_repo"], "rev-parse", "--verify", f"{self.cfg['target_vllm_ref']}^{{commit}}").stdout.strip().lower()
        if target != self.cfg["target_vllm_commit"]:
            raise WorkflowError(f"Upstream target ref resolves to {target}, expected {self.cfg['target_vllm_commit']}")

    def _acquire_run_lock(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise WorkflowError(f"Another run may be active ({self.lock_path}); inspect before removing a stale lock") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(json.dumps({"pid": os.getpid(), "created_at": now()}) + "\n")

    def _release_run_lock(self) -> None:
        self.lock_path.unlink(missing_ok=True)

    def _init_run(self, dry_run: bool) -> None:
        self.state = self._idle_state()
        self.state.update(run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8],
                          status="running", phase="preflight", dry_run=dry_run, started_at=now())
        self._save()
        atomic_json(self.control_path, {"run_id": self.state["run_id"], "action": "run", "updated_at": now()})

    def start(self, dry_run: bool = False) -> dict:
        self._acquire_run_lock()
        try:
            self._init_run(dry_run)
            try:
                self._preflight()
                if dry_run:
                    for platform in self.cfg["platforms"]:
                        self._platform(platform["name"], status="simulated")
                    self._set(status="simulated", phase="complete", finished_at=now())
                else:
                    self._run_initial()
            except Cancelled as exc:
                self._set(status="cancelled", phase="complete", error=str(exc), finished_at=now())
            except WorkflowError as exc:
                status = "blocked" if self.state["phase"] == "preflight" else "failed"
                self._set(status=status, phase="complete", error=str(exc), finished_at=now())
            return self.status()
        finally:
            self._release_run_lock()

    def _context(self, platform: dict, attempt: dict, scope: str = "", evidence_file: str = "") -> dict[str, str]:
        return {
            "repo": str(self.cfg["repo"]), "upstream_repo": str(self.cfg["upstream_repo"]),
            "tool_dir": str(APP_ROOT),
            "work_dir": str(self.work_dir), "worktree": attempt["worktree"],
            "platform": platform["name"], "scope": scope, "attempt": str(attempt["number"]),
            "target_vllm_ref": self.cfg["target_vllm_ref"],
            "target_vllm_commit": self.cfg["target_vllm_commit"],
            "plugin_base_sha": self.cfg["plugin_base_sha"],
            "cuda_baseline_commit": self.state.get("cuda_baseline_commit") or "",
            "plugin_commit": attempt["plugin_commit"] or "",
            "evidence_file": evidence_file,
        }

    def _env(self, platform: dict, attempt: dict, scope: str = "", evidence_file: str = "") -> dict[str, str]:
        context = self._context(platform, attempt, scope, evidence_file)
        env = os.environ.copy()
        env.update({
            "FLFLOW_TARGET_VLLM_REF": context["target_vllm_ref"],
            "FLFLOW_TARGET_VLLM_COMMIT": context["target_vllm_commit"],
            "FLFLOW_PLUGIN_BASE_SHA": context["plugin_base_sha"],
            "FLFLOW_CUDA_BASELINE_COMMIT": context["cuda_baseline_commit"],
            "FLFLOW_PLUGIN_COMMIT": context["plugin_commit"],
            "FLFLOW_PLATFORM": context["platform"], "FLFLOW_SCOPE": scope,
            "FLFLOW_WORKTREE": context["worktree"], "FLFLOW_WORK_DIR": context["work_dir"],
            "FLFLOW_UPSTREAM_REPO": context["upstream_repo"],
            "FLFLOW_TOOL_DIR": context["tool_dir"],
            "FLFLOW_EVIDENCE_DIR": str(self.work_dir / "evidence" / self.state["run_id"] / platform["name"]),
            "FLFLOW_EVIDENCE_FILE": evidence_file,
        })
        env.update({k: expand(v, context) for k, v in platform.get("env", {}).items()})
        return env

    def _make_worktree(self, platform: dict, attempt: dict, base_sha: str) -> None:
        self._gate()
        path = Path(attempt["worktree"])
        if path.exists():
            raise WorkflowError(f"Refusing to overwrite existing worktree path: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.cfg["repo"], "worktree", "add", "--detach", str(path), base_sha)

    def _snapshot(self, platform: dict, attempt: dict) -> str:
        self._gate()
        path = Path(attempt["worktree"])
        self._guard_acceptance_inputs(path)
        git(path, "add", "-A")
        changed = git(path, "diff", "--cached", "--quiet", check=False).returncode
        if changed not in (0, 1):
            raise WorkflowError(f"Could not inspect staged changes in {path}")
        if changed:
            git(path, "-c", "user.name=Codex FL Workflow", "-c", "user.email=flflow@local.invalid",
                "commit", "-m", f"FL workflow {self.state['run_id']} {platform['name']} attempt {attempt['number']}")
        commit = git(path, "rev-parse", "HEAD").stdout.strip().lower()
        ref = f"refs/flflow/{self.state['run_id']}/{platform['name']}/attempt-{attempt['number']}"
        git(self.cfg["repo"], "update-ref", ref, commit)
        return commit

    def _guard_acceptance_inputs(self, worktree: Path) -> None:
        current_config = canonical_config_sha256(read_json(self.config_path))
        if current_config != self.config_sha256:
            raise WorkflowError("Acceptance config changed during this run")
        for path, original_hash in self.protected_tool_hashes.items():
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != original_hash:
                raise WorkflowError(f"Protected acceptance tool changed during this run: {path}")
        changed: set[str] = set()
        for args in (
            ("diff", "--name-only", "--no-renames", "-z", "HEAD"),
            ("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
        ):
            changed.update(name for name in git(worktree, *args).stdout.split("\0") if name)
        protected = sorted(name for name in changed if is_protected_repo_path(name) or
                           name.replace("\\", "/").lower() in self.protected_repo_paths)
        if protected:
            raise WorkflowError("Adaptation or tests changed protected acceptance files: " + ", ".join(protected))

    def _run_command(self, platform: dict, attempt: dict, label: str, argv_template: list[str],
                     timeout: float | None, scope: str = "", evidence_file: str = "") -> None:
        self._gate()
        context = self._context(platform, attempt, scope, evidence_file)
        argv = [expand(part, context) for part in argv_template]
        log = self.work_dir / "logs" / self.state["run_id"] / platform["name"] / f"attempt-{attempt['number']}" / f"{label}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        record = {"label": label, "status": "running", "log_file": str(log), "started_at": now(), "finished_at": None, "exit_code": None}
        with self.lock:
            attempt["commands"].append(record)
            self._save()
        started = time.monotonic()
        proc: subprocess.Popen[str] | None = None
        try:
            with log.open("w", encoding="utf-8", errors="replace") as output:
                proc = subprocess.Popen(argv, cwd=attempt["worktree"], env=self._env(platform, attempt, scope, evidence_file),
                                        stdout=output, stderr=subprocess.STDOUT, text=True,
                                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                                        start_new_session=os.name != "nt")
                with self.lock:
                    self.active_commands += 1
                while proc.poll() is None:
                    if self._control() == "cancel":
                        self._set(status="cancelling")
                        self._terminate_process_tree(proc)
                        raise Cancelled(f"Cancelled during {platform['name']} {label}")
                    if timeout is not None and time.monotonic() - started > timeout:
                        self._terminate_process_tree(proc)
                        raise WorkflowError(f"{platform['name']} {label} timed out after {timeout} seconds")
                    time.sleep(POLL_SECONDS)
                if proc.returncode:
                    raise WorkflowError(f"{platform['name']} {label} exited {proc.returncode}; see {log}")
            record["status"] = "passed"
        except (OSError, WorkflowError) as exc:
            record["status"] = "cancelled" if isinstance(exc, Cancelled) else "failed"
            if isinstance(exc, OSError):
                raise WorkflowError(f"Could not launch {platform['name']} {label}: {exc}") from exc
            raise
        finally:
            with self.lock:
                if proc is not None:
                    self.active_commands -= 1
                record["exit_code"] = proc.returncode if proc is not None else None
                record["finished_at"] = now()
                self._save()

    @staticmethod
    def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, check=False, timeout=10)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()

    def _evidence_path(self, platform: dict, attempt: dict, test: dict) -> Path:
        context = self._context(platform, attempt, test["scope"])
        value = Path(expand(test["evidence_file"], context))
        if not value.is_absolute():
            value = self.work_dir / value
        value = value.resolve()
        if not within(value, self.work_dir):
            raise WorkflowError(f"Evidence path must stay inside work_dir: {value}")
        return value

    def _verify_evidence(self, path: Path, platform: dict, attempt: dict, scope: str) -> dict:
        evidence = read_json(path)
        expected = {
            "platform": platform["name"], "scope": scope,
            "target_vllm_commit": self.cfg["target_vllm_commit"],
            "plugin_commit": attempt["plugin_commit"],
        }
        for key, value in expected.items():
            if evidence.get(key) != value:
                raise WorkflowError(f"{path}: {key} must equal {value!r}")
        for key in ("executed", "passed", "failed", "skipped"):
            value = evidence.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise WorkflowError(f"{path}: {key} must be a nonnegative integer")
        if not (evidence["executed"] > 0 and evidence["passed"] == evidence["executed"] and
                evidence["failed"] == 0 and evidence["skipped"] == 0):
            raise WorkflowError(f"{path}: acceptance requires executed=passed>0, failed=0, skipped=0")
        if evidence.get("status") != "passed" or evidence.get("issues") != []:
            raise WorkflowError(f"{path}: status must be passed and issues must be empty")
        cases = evidence.get("cases")
        if not isinstance(cases, list) or not cases:
            raise WorkflowError(f"{path}: cases must contain at least one executed test invocation")
        totals = {key: 0 for key in ("executed", "passed", "failed", "skipped")}
        for index, case in enumerate(cases, start=1):
            if not isinstance(case, dict) or case.get("issues") != []:
                raise WorkflowError(f"{path}: case {index} has issues or is malformed")
            for key in totals:
                value = case.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise WorkflowError(f"{path}: case {index} {key} must be a nonnegative integer")
                totals[key] += value
            if not (case["executed"] > 0 and case["passed"] == case["executed"] and
                    case["failed"] == 0 and case["skipped"] == 0 and
                    case.get("errors", 0) == 0 and case.get("exit_code", 0) == 0):
                raise WorkflowError(f"{path}: case {index} did not execute and pass without failures or skips")
        if any(totals[key] != evidence[key] for key in totals):
            raise WorkflowError(f"{path}: aggregate counts disagree with case counts")
        evidence["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return evidence

    def _execute_platform(self, platform: dict, attempt: dict, base_sha: str, prepared: bool = False) -> bool:
        name = platform["name"]
        try:
            if not prepared:
                self._make_worktree(platform, attempt, base_sha)
            self._attempt(name, status="running")
            self._platform(name, status="running")
            if platform["kind"] == "cuda":
                self._set(phase="cuda_adapt")
            self._run_command(platform, attempt, "adapt", platform["adapt_command"], platform.get("adapt_timeout_seconds"))
            commit = self._snapshot(platform, attempt)
            self._attempt(name, plugin_commit=commit)
            if platform["kind"] == "cuda":
                self._set(cuda_baseline_commit=commit, phase="cuda_test")
            for test in platform["test_commands"]:
                scope = test["scope"]
                evidence_path = self._evidence_path(platform, attempt, test)
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence_path.unlink(missing_ok=True)
                with self.lock:
                    attempt["tests"][scope] = {"status": "running", "evidence_file": str(evidence_path), "evidence": None, "error": None}
                    self._save()
                try:
                    self._run_command(platform, attempt, f"test-{scope}", test["argv"], test.get("timeout_seconds"), scope, str(evidence_path))
                    self._guard_acceptance_inputs(Path(attempt["worktree"]))
                    verified = self._verify_evidence(evidence_path, platform, attempt, scope)
                    with self.lock:
                        attempt["tests"][scope].update(status="passed", evidence=verified)
                        self._save()
                except WorkflowError as exc:
                    with self.lock:
                        attempt["tests"][scope].update(status="cancelled" if isinstance(exc, Cancelled) else "failed", error=str(exc))
                        self._save()
                    raise
            self._attempt(name, status="passed", finished_at=now())
            self._platform(name, status="passed")
            return True
        except WorkflowError as exc:
            status = "cancelled" if isinstance(exc, Cancelled) else "failed"
            self._attempt(name, status=status, error=str(exc), finished_at=now())
            self._platform(name, status=status)
            return False

    def _backend_parallel(self, platforms: list[dict]) -> None:
        if not platforms:
            return
        self._set(phase="backend_adapt_test")
        baseline = self.state["cuda_baseline_commit"]
        ready: list[tuple[dict, dict]] = []
        for platform in platforms:
            attempt = self._new_attempt(platform)
            try:
                self._make_worktree(platform, attempt, baseline)
                ready.append((platform, attempt))
            except WorkflowError as exc:
                status = "cancelled" if isinstance(exc, Cancelled) else "failed"
                self._attempt(platform["name"], status=status, error=str(exc), finished_at=now())
                self._platform(platform["name"], status=status)
                if isinstance(exc, Cancelled):
                    break
        if self._control() == "cancel":
            for platform, attempt in ready:
                self._attempt(platform["name"], status="cancelled", error="Cancelled before adaptation", finished_at=now())
                self._platform(platform["name"], status="cancelled")
            return
        with ThreadPoolExecutor(max_workers=min(self.cfg["max_parallel"], len(ready)) or 1) as pool:
            futures = [pool.submit(self._execute_platform, p, a, baseline, True) for p, a in ready]
            for future in as_completed(futures):
                future.result()

    def _finish(self) -> None:
        statuses = [p["status"] for p in self.state["platforms"].values()]
        if self._control() == "cancel" or "cancelled" in statuses:
            result = "cancelled"
        elif statuses and all(status == "passed" for status in statuses):
            commits = {p["attempts"][-1]["plugin_commit"] for p in self.state["platforms"].values()}
            if len(commits) == 1:
                result = "passed"
                self._set(candidate_commit=next(iter(commits)), integration_required=False)
            else:
                result = "ready_for_integration"
                self._set(candidate_commit=None, integration_required=True)
        else:
            result = "failed"
        self._set(status=result, phase="complete", finished_at=now())

    def _run_initial(self) -> None:
        cuda = next(p for p in self.cfg["platforms"] if p["kind"] == "cuda")
        attempt = self._new_attempt(cuda)
        if not self._execute_platform(cuda, attempt, self.cfg["plugin_base_sha"]):
            reason = "CUDA baseline did not pass acceptance"
            for platform in self.cfg["platforms"]:
                if platform["kind"] != "cuda":
                    self._platform(platform["name"], status="blocked", error=reason)
            self._finish()
            return
        backends = [p for p in self.cfg["platforms"] if p["kind"] != "cuda"]
        self._backend_parallel(backends)
        self._finish()

    def retry_failed(self) -> dict:
        self._acquire_run_lock()
        try:
            self.state = read_json(self.state_path)
            if self.state.get("status") != "failed":
                raise WorkflowError("retry-failed requires a failed run; use start for a new run")
            if (self.state.get("config_hash_algorithm") != "canonical-json-v1" or
                    self.state.get("config_sha256") != self.config_sha256):
                raise WorkflowError("Config changed since this run; use start for a fresh full run")
            if self.state.get("protected_tool_sha256") != {
                    str(path): digest for path, digest in self.protected_tool_hashes.items()}:
                raise WorkflowError("Acceptance tools changed since this run; use start for a fresh full run")
            cuda = next(p for p in self.cfg["platforms"] if p["kind"] == "cuda")
            if self.state["platforms"][cuda["name"]]["status"] != "passed" or not self.state.get("cuda_baseline_commit"):
                raise WorkflowError("CUDA baseline failed; use start for a fresh full run")
            if (self.state.get("plugin_base_sha") != self.cfg["plugin_base_sha"] or
                    self.state.get("target_vllm_commit") != self.cfg["target_vllm_commit"]):
                raise WorkflowError("Config changed since this run; use start for a fresh run")
            self._preflight()
            failed = [p for p in self.cfg["platforms"] if p["kind"] != "cuda" and self.state["platforms"][p["name"]]["status"] == "failed"]
            if not failed:
                raise WorkflowError("No failed backend is available to retry")
            atomic_json(self.control_path, {"run_id": self.state["run_id"], "action": "run", "updated_at": now()})
            self._set(status="running", phase="backend_adapt_test", finished_at=None, error=None,
                      latest_config_sha256=self.config_sha256)
            self._backend_parallel(failed)
            self._finish()
            return self.status()
        finally:
            self._release_run_lock()

    def launch(self, retry: bool = False, dry_run: bool = False) -> None:
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                raise WorkflowError("A run is already active in this server")
            if self.lock_path.exists():
                raise WorkflowError(f"A run lock exists: {self.lock_path}")
            def target() -> None:
                try:
                    self.retry_failed() if retry else self.start(dry_run)
                except Exception as exc:
                    with self.lock:
                        self.state = read_json(self.state_path) if self.state_path.exists() else self._idle_state()
                        self.state.update(status="failed", phase="complete", error=f"Internal workflow error: {exc}", finished_at=now())
                        self._save()
            self.worker = threading.Thread(target=target, name="flflow-worker", daemon=True)
            self.worker.start()


def make_server(engine: Engine | None, host: str, port: int, monitor: object | None = None,
                sessions: object | None = None, *, registry: ProjectRegistry | None = None) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise WorkflowError("HTTP control must bind to loopback only")
    registry = registry or ProjectRegistry.single(engine, monitor, sessions)

    from fl_dashboard.tasks.ssh_profile_sync import SSHProfileSync
    ssh_syncs = {}
    for project in registry.projects:
        config = getattr(project.monitor, "config", {})
        state_root = config.get("sessions_dir")
        state_path = Path(state_root) / "ssh-config-sync.json" if state_root else None
        ssh_syncs[project.id] = SSHProfileSync(config.get("ssh_import_config"), state_path)

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, body: dict) -> None:
            data = json.dumps(body, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _request_host(self) -> str | None:
            raw = self.headers.get("Host", "")
            try:
                parsed = urlsplit("//" + raw)
                hostname = (parsed.hostname or "").lower()
                if (hostname not in ("127.0.0.1", "localhost", "::1") or
                        parsed.port != self.server.server_port or parsed.username or parsed.password or
                        parsed.path or parsed.query or parsed.fragment):
                    return None
                return hostname
            except ValueError:
                return None

        def _trusted_request(self, post: bool = False) -> bool:
            hostname = self._request_host()
            if hostname is None:
                self._json(HTTPStatus.FORBIDDEN, {"error": "Host must be the local workflow origin"})
                return False
            if post:
                origin = self.headers.get("Origin")
                if origin is not None:
                    try:
                        parsed = urlsplit(origin)
                        same_origin = (parsed.scheme == "http" and parsed.hostname == hostname and
                                       parsed.port == self.server.server_port and not parsed.username and
                                       not parsed.password and not parsed.path and not parsed.query and
                                       not parsed.fragment)
                    except ValueError:
                        same_origin = False
                    if not same_origin:
                        self._json(HTTPStatus.FORBIDDEN, {"error": "Origin must match the local workflow origin"})
                        return False
                if self.headers.get("Sec-Fetch-Site", "").lower() not in ("", "same-origin", "none"):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Cross-site control requests are not allowed"})
                    return False
            return True

        def do_GET(self) -> None:
            if not self._trusted_request():
                return
            target = urlsplit(self.path)
            try:
                project = registry.select(target.query)
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            engine, monitor, sessions = project.workflow, project.monitor, project.sessions
            if target.path == "/api/projects":
                self._json(200, registry.metadata())
                return
            if target.path == "/api/projects-status":
                self._json(200, registry.statuses())
                return
            if target.path == "/api/agent-settings":
                try:
                    values = parse_qs(target.query, keep_blank_values=True).get("provider", ["claude"])
                    if len(values) != 1 or values[0] not in ("claude", "flagscale"):
                        raise ValueError("Choose Claude or FlagScale Agent settings")
                    store = project.flagscale_settings if values[0] == "flagscale" else project.agent_settings
                    if store is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Local agent settings are not configured"})
                        return
                    from fl_dashboard.agents.composite_sessions import safe_agent_settings
                    self._json(200, safe_agent_settings(store.snapshot()))
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Local agent settings are unavailable"})
                return
            if target.path == "/api/agent-providers":
                if sessions is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Agent session providers are not configured"})
                    return
                try:
                    self._json(200, {"providers": sessions.providers()})
                except Exception:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Local agent providers are unavailable"})
                return
            if target.path == "/api/adaptation-tasks":
                try:
                    self._json(200, {"tasks": project.adaptation_tasks(), "error": project.native_tasks_error})
                except Exception:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Adaptation task observations unavailable"})
                return
            if target.path in ("/api/status", "/api/logs") and engine is None:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": project.workflow_error or "Workflow is not configured for this project"})
                return
            history = re.fullmatch(r"/api/codex-sessions/([0-9a-f]{32})/events", target.path)
            if history:
                if sessions is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Codex sessions are not configured"})
                    return
                try:
                    query = parse_qs(target.query, keep_blank_values=True)
                    if set(query) - {"cursor", "limit", "project"} or any(len(values) != 1 for values in query.values()):
                        raise ValueError("Invalid history query")
                    raw_limit = query.get("limit", ["80"])[0]
                    if not re.fullmatch(r"[0-9]{1,2}", raw_limit):
                        raise ValueError("History limit must be between 1 and 80")
                    self._json(200, sessions.event_history(history[1], cursor=query.get("cursor", [None])[0], limit=int(raw_limit)))
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Session history unavailable"})
                return
            if target.path == "/api/capabilities":
                self._json(200, {"read_only": False, "sessions": sessions is not None,
                                 "refresh": monitor is not None, "workflow": engine is not None,
                                 "project_id": project.id, "label": project.label, "multi_project": registry.multi_project,
                                 "engine": project.engine_name, "default_branch": project.default_branch,
                                 "adaptation_tasks": project.native_tasks is not None or sessions is not None})
                return
            if target.path in ("/api/codex-sessions", "/api/codex-health"):
                if sessions is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Codex sessions are not configured"})
                    return
                try:
                    if target.path.endswith("health"):
                        result = sessions.health()
                    elif parse_qs(target.query).get("view") == ["cards"]:
                        result = sessions.snapshot(compact=True)
                    else:
                        result = sessions.snapshot()
                    if target.path == "/api/codex-sessions":
                        result = ssh_syncs[project.id].synchronize(sessions, result)
                    self._json(200, result)
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception as exc:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Codex sessions unavailable: {exc}"})
                return
            if target.path == "/api/branches":
                if monitor is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Branch monitor is not configured"})
                    return
                try:
                    query = parse_qs(target.query, keep_blank_values=True)
                    if set(query) - {"project"} or any(len(values) != 1 for values in query.values()):
                        raise ValueError("Invalid branch list query")
                    options = getattr(monitor, "branch_options", None)
                    result = options() if callable(options) else {"branch": monitor.branch, "branches": [monitor.branch]}
                    self._json(200, result)
                except (ValueError, MonitorError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Branch list is temporarily unavailable"})
                return
            if target.path == "/api/branch-status":
                if monitor is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Branch monitor is not configured"})
                    return
                branch = parse_qs(target.query).get("branch", [None])[0]
                try:
                    self._json(200, monitor.status(branch=branch))
                except (ValueError, MonitorError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception as exc:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Branch monitor failed: {exc}"})
                return
            if target.path == "/api/acceptance-record":
                if monitor is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Branch monitor is not configured"})
                    return
                try:
                    query = parse_qs(target.query, keep_blank_values=True, max_num_fields=10)
                    if set(query) - {"project", "branch", "backend", "record"} or any(len(values) != 1 for values in query.values()):
                        raise ValueError("Invalid acceptance record query")
                    backend, record_id = query.get("backend", [""])[0], query.get("record", [""])[0]
                    if not backend or not record_id:
                        raise ValueError("backend and record are required")
                    result = monitor.acceptance_record(backend, record_id, branch=query.get("branch", [None])[0])
                    content = result["content"]
                    if not isinstance(content, str):
                        raise ValueError("Invalid acceptance record content")
                    data = content.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                except (ValueError, MonitorError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except FileNotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Acceptance record unavailable"})
                except Exception:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Acceptance record unavailable"})
                return
            if target.path == "/api/branch-evidence":
                if monitor is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Branch monitor is not configured"})
                    return
                query = parse_qs(target.query)
                fields = {key: query.get(key, [""])[0] for key in ("branch", "backend", "model", "mode", "scenario")}
                try:
                    if not all(fields.values()):
                        raise ValueError("branch, backend, model, mode, and scenario are required")
                    self._json(200, monitor.read_evidence(**fields))
                except (ValueError, KeyError, MonitorError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except FileNotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Evidence is not available"})
                except Exception as exc:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Could not read evidence: {exc}"})
                return
            if target.path == "/api/status":
                self._json(200, engine.status())
                return
            if target.path == "/api/logs":
                query = parse_qs(target.query)
                platform = query.get("platform", [""])[0]
                try:
                    attempt_number = int(query.get("attempt", ["0"])[0])
                    tail = max(1, min(int(query.get("tail", ["200"])[0]), 1000))
                    state = engine.status()
                    attempts = state["platforms"][platform]["attempts"]
                    attempt = attempts[attempt_number - 1] if attempt_number > 0 else attempts[-1]
                    logs = []
                    for command in attempt["commands"]:
                        path = Path(command["log_file"])
                        if not within(path, engine.work_dir):
                            continue
                        if path.exists():
                            with path.open("r", encoding="utf-8", errors="replace") as stream:
                                lines = list(deque((line.rstrip("\n\r") for line in stream), maxlen=tail))
                        else:
                            lines = []
                        logs.append({"label": command["label"], "lines": lines})
                    self._json(200, {"platform": platform, "attempt": attempt["number"], "logs": logs})
                except (KeyError, IndexError, ValueError):
                    self._json(400, {"error": "Invalid platform, attempt, or tail"})
                return
            if target.path in ("/", "/index.html", "/monitor", "/monitor.html", "/workflow", "/workflow.html", "/sessions", "/sessions.html"):
                filename = ("sessions.html" if target.path in ("/sessions", "/sessions.html") else
                            "workflow.html" if target.path in ("/workflow", "/workflow.html") else
                            "overview.html" if registry.multi_project and target.path in ("/", "/index.html") else "index.html")
                path = TEMPLATES / filename
                if not path.is_file():
                    self._json(404, {"error": f"{filename} is not installed"})
                    return
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            self._json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            if not self._trusted_request(post=True):
                return
            parsed = urlsplit(self.path)
            target = parsed.path
            try:
                project = registry.select(parsed.query)
                engine, monitor, sessions = project.workflow, project.monitor, project.sessions
                if target in ("/api/start", "/api/retry", "/api/retry-failed", "/api/pause", "/api/resume", "/api/cancel") and engine is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": project.workflow_error or "Workflow is not configured for this project"})
                    return
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise WorkflowError("Content-Type must be application/json")
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 1_000_000:
                    raise WorkflowError("Request body is too large")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise WorkflowError("Request body must be a JSON object")
                if target == "/api/agent-settings":
                    provider = body.get("provider", "claude")
                    if provider not in ("claude", "flagscale"):
                        raise WorkflowError("Choose Claude or FlagScale Agent settings")
                    store = project.flagscale_settings if provider == "flagscale" else project.agent_settings
                    if store is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Local agent settings are not configured"})
                        return
                    from fl_dashboard.agents.composite_sessions import safe_agent_settings
                    self._json(200, safe_agent_settings(store.update(body)))
                    return
                if target == "/api/codex-machines/sync-ssh":
                    if sessions is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Agent sessions are not configured"})
                        return
                    if body:
                        raise WorkflowError("SSH sync does not accept client paths or connection fields")
                    result = ssh_syncs[project.id].synchronize(sessions, sessions.snapshot(compact=True))
                    self._json(200, {"profiles": result.get("machine_profiles", []),
                                     "ssh_config_sync": result.get("ssh_config_sync")})
                    return
                session_action = re.fullmatch(r"/api/codex-sessions/([A-Za-z0-9_-]+)/(start|message|interrupt|respond|desktop|delete|restore)", target)
                if session_action and not re.fullmatch(r"[0-9a-f]{32}", session_action[1]):
                    raise WorkflowError("Invalid dashboard session ID; native tasks are observation-only")
                if target in ("/api/codex-machines", "/api/codex-machines/check", "/api/codex-sessions") or session_action:
                    if sessions is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Codex sessions are not configured"})
                        return
                    if target == "/api/codex-machines":
                        result = sessions.save_profile(body)
                    elif target == "/api/codex-machines/check":
                        result = sessions.check_machine(body)
                        if isinstance(result, dict):
                            alias = result.get("ssh_alias")
                            if isinstance(alias, str):
                                ssh_syncs[project.id].checked(alias)
                    elif target == "/api/codex-sessions":
                        from fl_dashboard.tasks.automatic_tasks import normalize_automatic_task
                        task_body = normalize_automatic_task(project, body)
                        result = sessions.create(task_body)
                        if body.get("start") is True:
                            result = sessions.start(result["id"])
                    else:
                        session_id, action = session_action.groups()
                        if action == "start":
                            result = sessions.start(session_id)
                        elif action == "message":
                            result = sessions.message(session_id, body.get("text", ""))
                        elif action == "interrupt":
                            result = sessions.interrupt(session_id)
                        elif action in ("delete", "restore"):
                            if body:
                                raise WorkflowError("Task deletion and restoration do not accept extra fields")
                            result = getattr(sessions, action)(session_id)
                        elif action == "desktop":
                            if body:
                                raise WorkflowError("Codex client continuation does not accept paths, URLs or task changes")
                            result = sessions.desktop(session_id)
                        else:
                            result = sessions.respond(session_id, body.get("request_id"), body.get("response"))
                    self._json(200, result)
                elif target == "/api/branch-refresh":
                    if monitor is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Branch monitor is not configured"})
                    else:
                        branch = body.get("branch")
                        if branch is not None and not isinstance(branch, str):
                            raise WorkflowError("branch must be a string")
                        self._json(200, monitor.refresh_branch(branch=branch))
                elif target == "/api/start":
                    engine.launch(dry_run=body.get("dry_run", False) is True)
                    self._json(HTTPStatus.ACCEPTED, {"accepted": True})
                elif target in ("/api/retry", "/api/retry-failed"):
                    engine.launch(retry=True)
                    self._json(HTTPStatus.ACCEPTED, {"accepted": True})
                elif target in ("/api/pause", "/api/resume", "/api/cancel"):
                    self._json(200, engine.request(target.removeprefix("/api/")))
                else:
                    self._json(404, {"error": "Not found"})
            except (WorkflowError, MonitorError, json.JSONDecodeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Request failed: {exc}"})

    return ThreadingHTTPServer((host, port), Handler)


def serve(engine: Engine | None, host: str, port: int, monitor: object | None = None,
          sessions: object | None = None, *, registry: ProjectRegistry | None = None) -> None:
    server = make_server(engine, host, port, monitor, sessions, registry=registry)
    print(f"FL workflow dashboard: http://{host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if registry is not None:
            registry.close()
        elif sessions is not None:
            sessions.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("validate", "start", "status", "pause", "resume", "cancel", "retry-failed", "serve"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--config", required=action != "serve", type=Path)
        if action == "start":
            cmd.add_argument("--dry-run", action="store_true")
        if action == "serve":
            cmd.add_argument("--host", default="127.0.0.1")
            cmd.add_argument("--port", type=int, default=8765)
            cmd.add_argument("--monitor-config", type=Path)
            cmd.add_argument("--projects-config", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "serve" and args.projects_config:
            registry = ProjectRegistry.from_config(args.projects_config)
            serve(None, args.host, args.port, registry=registry)
            return 0
        if args.config is None:
            raise WorkflowError("--config or --projects-config is required for serve")
        engine = Engine(args.config)
        if args.action == "validate":
            print(json.dumps({"valid": True, "platforms": [p["name"] for p in engine.cfg["platforms"]]}, indent=2))
            return 0
        if args.action == "serve":
            from fl_dashboard.monitoring.branch_monitor import BranchMonitor
            from fl_dashboard.agents.codex_sessions import CodexSessionManager

            monitor_path = args.monitor_config or args.config.resolve().parent / "branch-monitor.json"
            monitor = BranchMonitor(monitor_path) if monitor_path.is_file() else None
            sessions = CodexSessionManager(monitor.config, monitor_path.resolve().parent) if monitor else None
            serve(engine, args.host, args.port, monitor, sessions)
            return 0
        if args.action == "start":
            result = engine.start(args.dry_run)
        elif args.action == "retry-failed":
            result = engine.retry_failed()
        elif args.action == "status":
            result = engine.status()
        else:
            result = engine.request(args.action)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] in ("passed", "simulated", "idle", "running", "pausing", "paused", "cancelling") else 1
    except (WorkflowError, ProjectError, KeyboardInterrupt) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
