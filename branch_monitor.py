#!/usr/bin/env python3
"""Read-only branch, CI, and adaptation-gate monitor.

Gate evidence is intentionally separate from GitHub CI. A case passes only when
the exact branch HEAD is bound to a manifest and every request in its JSON
result passes. Neither an integrated PR nor a successful CI job implies that
the manual adaptation gate ran.

Manifest schema (one per backend/model/mode run):
  {"schema_version": 1, "run_id": "unique-id", "backend": "cuda",
   "branch": "0.4.0-dev", "commit": "<40-hex-sha>",
   "model": "Qwen3.6-27B", "mode": "eager", "port": 8000,
   "results_dir": "results"}
Place it below evidence_dir. results_dir is relative to the manifest and must
remain below evidence_dir. It contains one <scenario>.json for each case from
tools/adaptation-gate-cases/test_*.py at that commit.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


SHA = re.compile(r"^[0-9a-f]{40}$")
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CASE_DIR = "tools/adaptation-gate-cases"
LABELS = {"cuda": "NVIDIA", "ascend": "Ascend", "metax": "MetaX", "corex": "CoreX",
          "hygon": "Hygon", "musa": "MUSA", "enflame": "Enflame", "kunlunxin": "Kunlunxin"}


class MonitorError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _scenario_requests(name: str) -> int | None:
    if name.endswith("_single"):
        return 1
    match = re.search(r"_concurrent_(\d+)$", name)
    return int(match.group(1)) if match else None


class BranchMonitor:
    """Inspect a configured local remote-tracking branch and optional GitHub data."""

    def __init__(self, config: Path | str | dict[str, Any],
                 transport: Callable[[str], dict[str, Any]] | None = None):
        if isinstance(config, (str, Path)):
            config_path = Path(config).expanduser().resolve()
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            base = config_path.parent
        else:
            raw = config
            base = Path.cwd()
        if not isinstance(raw, dict):
            raise MonitorError("Monitor config must be an object")
        self.config = raw.get("monitor", raw)
        if not isinstance(self.config, dict):
            raise MonitorError("monitor must be an object")
        repo_value = self.config.get("repo") or self.config.get("plugin_repo")
        if not isinstance(repo_value, str) or not repo_value:
            raise MonitorError("Monitor repo is required")
        self.repo = (base / repo_value).resolve()
        if not self.repo.is_dir():
            raise MonitorError(f"Monitor repo does not exist: {self.repo}")
        evidence_value = self.config.get("evidence_dir", "runs/gate-evidence")
        if not isinstance(evidence_value, str) or not evidence_value:
            raise MonitorError("evidence_dir must be a path")
        self.evidence_dir = (base / evidence_value).resolve()
        self.remote = self.config.get("remote", "upstream")
        self.branch = self.config.get("branch", "0.4.0-dev")
        if not isinstance(self.remote, str) or not REF.fullmatch(self.remote):
            raise MonitorError("Invalid remote name")
        self._validate_branch(self.branch)
        self.github_repo = self.config.get("github_repo", "")
        if self.github_repo and (not isinstance(self.github_repo, str) or not REPO.fullmatch(self.github_repo)):
            raise MonitorError("github_repo must be owner/repo")
        self.models = self.config.get("models", ["Qwen3.6-27B", "Qwen3.6-35B-A3B"])
        self.modes = self.config.get("modes", ["eager", "graph"])
        if not isinstance(self.models, list) or not self.models or not all(isinstance(x, str) and x for x in self.models):
            raise MonitorError("models must be nonempty strings")
        if not isinstance(self.modes, list) or not self.modes or not all(isinstance(x, str) and x for x in self.modes):
            raise MonitorError("modes must be nonempty strings")
        self.backend_prs = self.config.get("backend_prs", {})
        self.merged_commits = self.config.get("merged_commits", {})
        self.backend_labels = self.config.get("backend_labels", {})
        if not isinstance(self.backend_prs, dict) or not isinstance(self.merged_commits, dict):
            raise MonitorError("backend_prs and merged_commits must be objects")
        if not isinstance(self.backend_labels, dict):
            raise MonitorError("backend_labels must be an object")
        self.transport = transport or self._http_json
        self._api_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache_lock = threading.RLock()

    @staticmethod
    def _validate_branch(branch: str) -> None:
        if not isinstance(branch, str) or not REF.fullmatch(branch) or ".." in branch or "//" in branch or branch.endswith("/"):
            raise MonitorError("Invalid branch name")

    def _git(self, *args: str, timeout: int = 8, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            done = subprocess.run(["git", "-c", f"safe.directory={self.repo}", "-C", str(self.repo), *args], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MonitorError(f"git {args[0]} unavailable: {exc}") from exc
        if check and done.returncode:
            raise MonitorError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()}")
        return done

    def _head(self, branch: str) -> tuple[str, str]:
        ref = f"refs/remotes/{self.remote}/{branch}"
        sha = self._git("rev-parse", "--verify", ref).stdout.strip().lower()
        if not SHA.fullmatch(sha):
            raise MonitorError(f"Invalid SHA for {ref}")
        return ref, sha

    def _show(self, sha: str, path: str) -> str:
        return self._git("show", f"{sha}:{path}").stdout

    def _branches(self) -> list[str]:
        prefix = f"refs/remotes/{self.remote}/"
        done = self._git("for-each-ref", "--format=%(refname)", prefix)
        return sorted(ref[len(prefix):] for ref in done.stdout.splitlines()
                      if ref.startswith(prefix) and not ref.endswith("/HEAD"))

    def _registry(self, sha: str) -> dict[str, bool]:
        text = self._show(sha, ".github/configs/platforms.yml")
        result: dict[str, bool] = {}
        current: str | None = None
        for line in text.splitlines():
            backend = re.match(r"^  ([a-z][a-z0-9_-]*):\s*(?:#.*)?$", line)
            if backend:
                current = backend.group(1)
                result[current] = False
                continue
            enabled = re.match(r"^    enabled:\s*(true|false)\b", line, re.I)
            if enabled and current:
                result[current] = enabled.group(1).lower() == "true"
        if not result:
            raise MonitorError("No backends found in platforms.yml")
        return result

    def _version(self, sha: str) -> str | None:
        try:
            data = tomllib.loads(self._show(sha, "pyproject.toml"))
            project = data.get("project", {})
            dependencies = list(project.get("dependencies", []))
            for optional in project.get("optional-dependencies", {}).values():
                dependencies.extend(optional)
            for dep in dependencies:
                match = re.match(r"vllm(?:\[[^]]+\])?\s*==\s*([^\s;,]+)", dep, re.I)
                if match:
                    return match.group(1)
        except (MonitorError, tomllib.TOMLDecodeError, TypeError):
            pass
        return None

    def _cases(self, sha: str) -> list[dict[str, Any]]:
        paths = self._git("ls-tree", "-r", "--name-only", sha, CASE_DIR).stdout.splitlines()
        cases: dict[str, dict[str, Any]] = {}
        for path in paths:
            name = Path(path).name
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            try:
                tree = ast.parse(self._show(sha, path), filename=path)
            except SyntaxError as exc:
                raise MonitorError(f"Invalid case source {path}: {exc}") from exc
            for test in tree.body:
                if not isinstance(test, (ast.FunctionDef, ast.AsyncFunctionDef)) or not test.name.startswith("test_"):
                    continue
                for node in ast.walk(test):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "run_case":
                        continue
                    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                        continue
                    scenario = node.args[0].value
                    requests = _scenario_requests(scenario)
                    if requests is None:
                        raise MonitorError(f"Unknown request count for gate scenario {scenario}")
                    if scenario in cases:
                        raise MonitorError(f"Duplicate gate scenario {scenario}")
                    cases[scenario] = {"scenario": scenario, "requests": requests, "source": path,
                                       "file": path, "test": test.name}
        return list(cases.values())

    def _http_json(self, url: str) -> dict[str, Any]:
        gh = shutil.which("gh")
        if gh:
            try:
                done = subprocess.run([gh, "api", url], capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=10)
                if done.returncode == 0:
                    data = json.loads(done.stdout)
                    if isinstance(data, dict):
                        return data
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "fl-branch-monitor"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with urlopen(Request(url, headers=headers), timeout=6) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise MonitorError("GitHub API response must be an object")
        return data

    def _api(self, path: str, force: bool) -> dict[str, Any]:
        url = f"https://api.github.com/repos/{self.github_repo}/{path}"
        with self._cache_lock:
            cached = self._api_cache.get(url)
            if cached and not force and time.monotonic() - cached[0] < 300:
                return cached[1]
        value = self.transport(url)
        if not isinstance(value, dict):
            raise MonitorError("GitHub transport must return an object")
        with self._cache_lock:
            self._api_cache[url] = (time.monotonic(), value)
        return value

    @staticmethod
    def _ci_status(checks: list[dict[str, Any]]) -> str:
        if not checks:
            return "unknown"
        conclusions = [x.get("conclusion") for x in checks]
        if any(x in ("failure", "timed_out", "action_required", "startup_failure") for x in conclusions):
            return "failed"
        if any(x.get("status") != "completed" for x in checks):
            return "running"
        if any(x == "cancelled" for x in conclusions):
            return "cancelled"
        if all(x == "skipped" for x in conclusions):
            return "skipped"
        if any(x == "skipped" for x in conclusions) and all(x in ("success", "skipped") for x in conclusions):
            return "partial"
        return "passed" if all(x == "success" for x in conclusions) else "unknown"

    def _branch_ci(self, branch: str, sha: str, force: bool, errors: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        result = {"status": "unknown", "scope": "branch", "detail": "No CI run for current branch HEAD",
                  "url": None, "commit": sha}
        if not self.github_repo:
            return result, []
        try:
            data = self._api(f"actions/runs?branch={quote(branch, safe='')}&per_page=30", force)
            runs = [r for r in data.get("workflow_runs", []) if r.get("head_sha") == sha and
                    r.get("name") == "CI" and r.get("head_branch", branch) == branch]
            if not runs:
                # GitHub can briefly return an empty branch-filtered page after
                # refresh. Recheck the exact commit before showing unknown CI.
                data = self._api(f"actions/runs?head_sha={sha}&per_page=30", force)
                runs = [r for r in data.get("workflow_runs", []) if r.get("head_sha") == sha and
                        r.get("name") == "CI" and r.get("head_branch", branch) == branch]
            if not runs:
                return result, []
            run = max(runs, key=lambda r: r.get("created_at", ""))
            result.update(status=self._ci_status([{"status": run.get("status"), "conclusion": run.get("conclusion")}]),
                          detail=f"CI run #{run['id']}: {run.get('conclusion') or run.get('status')}",
                          url=run.get("html_url"))
            jobs_data = self._api(f"actions/runs/{run['id']}/jobs?per_page=100", force)
            jobs = jobs_data.get("jobs", [])
            return result, jobs if isinstance(jobs, list) else []
        except (KeyError, OSError, ValueError, MonitorError) as exc:
            errors.append(f"GitHub branch CI unavailable: {exc}")
            return result, []

    def _pr(self, backend: str, force: bool, errors: list[str]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        value = self.backend_prs.get(backend)
        number = value.get("number") if isinstance(value, dict) else value
        if not isinstance(number, int) or number <= 0:
            return None, None
        public = {"number": number, "url": f"https://github.com/{self.github_repo}/pull/{number}" if self.github_repo else None,
                  "title": None, "draft": None}
        if not self.github_repo:
            return public, None
        try:
            data = self._api(f"pulls/{number}", force)
            public.update(url=data.get("html_url", public["url"]), title=data.get("title"), draft=data.get("draft"))
            return public, data
        except (OSError, ValueError, MonitorError) as exc:
            errors.append(f"GitHub PR #{number} unavailable: {exc}")
            return public, None

    def _ancestor(self, ancestor: str, head: str) -> bool:
        if not isinstance(ancestor, str) or not SHA.fullmatch(ancestor.lower()):
            return False
        return self._git("merge-base", "--is-ancestor", ancestor, head, check=False).returncode == 0

    def _integration(self, backend: str, branch: str, sha: str, pr_data: dict[str, Any] | None) -> dict[str, Any]:
        fallback = self.merged_commits.get(backend)
        if branch == self.branch and isinstance(fallback, str) and self._ancestor(fallback.lower(), sha):
            result = {"status": "merged", "commit": fallback.lower(), "detail": "Merge commit is in current branch history"}
        else:
            result = {"status": "unknown", "commit": None, "detail": "No verified integration in current branch"}
        if not pr_data:
            return result
        base = pr_data.get("base", {})
        merged_commit = pr_data.get("merge_commit_sha")
        if pr_data.get("merged") and base.get("ref") == branch and isinstance(merged_commit, str) and self._ancestor(merged_commit.lower(), sha):
            return {"status": "merged", "commit": merged_commit.lower(), "detail": "PR merge commit is in current branch history"}
        if pr_data.get("state") == "open" and base.get("ref") == branch:
            return {"status": "draft" if pr_data.get("draft") else "open", "commit": pr_data.get("head", {}).get("sha"),
                    "detail": "Adaptation PR is open"}
        if pr_data.get("state") == "closed" and base.get("ref") == branch:
            return {"status": "closed", "commit": None, "detail": "PR is closed but not verified in this branch"}
        return result

    def _backend_ci(self, backend: str, integration: dict[str, Any], branch_jobs: list[dict[str, Any]],
                    branch_ci: dict[str, Any], pr_data: dict[str, Any] | None, force: bool,
                    errors: list[str]) -> dict[str, Any]:
        if integration["status"] == "merged":
            jobs = [job for job in branch_jobs if job.get("name", "").startswith(f"test-{backend} / ") and
                    not any(part in job.get("name", "") for part in (" / notify / ", " / Setup ("))]
            if not jobs:
                jobs = [job for job in branch_jobs if job.get("name") == f"test-{backend}"]
            return {"status": self._ci_status(jobs), "scope": "branch", "detail": f"{len(jobs)} backend test jobs at branch HEAD",
                    "url": branch_ci.get("url"), "commit": branch_ci.get("commit")}
        head_sha = (pr_data or {}).get("head", {}).get("sha")
        if integration["status"] not in ("open", "draft"):
            return {"status": "unknown", "scope": "pr", "detail": "No adaptation PR targeting this branch",
                    "url": None, "commit": None}
        if not self.github_repo or not isinstance(head_sha, str) or not SHA.fullmatch(head_sha.lower()):
            return {"status": "unknown", "scope": "pr", "detail": "No candidate branch CI data", "url": None, "commit": head_sha}
        try:
            data = self._api(f"commits/{head_sha}/check-runs?per_page=100", force)
            checks = [check for check in data.get("check_runs", []) if
                      check.get("name", "").startswith(f"test-{backend} / ") and
                      not any(part in check.get("name", "") for part in (" / notify / ", " / Setup ("))]
            return {"status": self._ci_status(checks), "scope": "pr", "detail": f"{len(checks)} backend test checks at PR head",
                    "url": checks[0].get("details_url") if checks else None, "commit": head_sha}
        except (OSError, ValueError, MonitorError) as exc:
            errors.append(f"GitHub {backend} checks unavailable: {exc}")
            return {"status": "unknown", "scope": "pr", "detail": "Candidate branch CI unavailable", "url": None, "commit": head_sha}

    def _manifests(self) -> list[tuple[Path, dict[str, Any]]]:
        if not self.evidence_dir.is_dir():
            return []
        found = []
        for path in self.evidence_dir.rglob("manifest.json"):
            if not _inside(path, self.evidence_dir):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    found.append((path, data))
            except (OSError, ValueError):
                continue
        return found

    def _result(self, manifest_path: Path, manifest: dict[str, Any], scenario: str, requests: int) -> tuple[str, str, Path | None, int]:
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]:
            return "failed", "Invalid manifest schema or run_id", None, 0
        port = manifest.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            return "failed", "Invalid manifest port", None, 0
        directory = manifest.get("results_dir")
        if not isinstance(directory, str) or not directory:
            return "failed", "Missing results_dir", None, 0
        results_dir = (manifest_path.parent / directory).resolve()
        if not _inside(results_dir, self.evidence_dir):
            return "failed", "results_dir escapes evidence_dir", None, 0
        path = results_dir / f"{scenario}.json"
        if not path.is_file() or not _inside(path, self.evidence_dir):
            return "missing", "Case JSON is missing", None, 0
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "failed", "Case JSON is invalid", path, 0
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return "failed", "Invalid case result schema", path, 0
        case = value.get("case")
        summary = value.get("summary")
        outputs = value.get("output")
        inputs = value.get("input")
        if not isinstance(case, dict) or not isinstance(summary, dict) or not isinstance(outputs, list) or not isinstance(inputs, list):
            return "failed", "Incomplete case result", path, 0
        model = manifest["model"]
        reported_models = [case.get("model_path"), case.get("served_model_name")]
        if not any(isinstance(x, str) and (x == model or Path(x).name == model) for x in reported_models):
            return "failed", "Case model differs from manifest", path, 0
        try:
            case_port = int(case.get("port"))
        except (ValueError, TypeError):
            case_port = None
        if case.get("scenario") != scenario or case_port != port or case.get("concurrency") != requests:
            return "failed", "Case identity or request count differs from manifest", path, 0
        def output_passes(output: Any) -> bool:
            if not isinstance(output, dict) or output.get("passed") is not True or output.get("error"):
                return False
            checks = output.get("checks")
            return isinstance(checks, dict) and bool(checks) and all(value is True for value in checks.values())

        passed_requests = sum(output_passes(output) for output in outputs)
        if (len(outputs) != requests or len(inputs) != requests or summary.get("request_count") != requests or
                summary.get("passed_count") != requests or summary.get("failed_count") != 0 or
                summary.get("passed") is not True or passed_requests != requests):
            return "failed", "One or more requests or summary checks failed", path, passed_requests
        return "passed", "All requests passed", path, passed_requests

    def _gate(self, backend: str, branch: str, sha: str, cases: list[dict[str, Any]],
              manifests: list[tuple[Path, dict[str, Any]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        used: list[tuple[Path, dict[str, Any]]] = []
        for model in self.models:
            for mode in self.modes:
                options = [(path, data) for path, data in manifests if
                           data.get("backend") == backend and data.get("branch") == branch and
                           data.get("model") == model and data.get("mode") == mode]
                current = [(path, data) for path, data in options if data.get("commit") == sha]
                pool = current or options
                selected = max(pool, key=lambda item: (str(item[1].get("created_at", "")), item[0].stat().st_mtime_ns)) if pool else None
                if selected:
                    used.append(selected)
                for case in cases:
                    scenario, requests = case["scenario"], case["requests"]
                    row = {"model": model, "mode": mode, "scenario": scenario, "requests": requests,
                           "status": "missing", "reason": "No evidence manifest", "evidence_url": None}
                    if selected:
                        path, manifest = selected
                        if manifest.get("commit") != sha:
                            row.update(status="stale", reason=f"Evidence belongs to {manifest.get('commit')}")
                        else:
                            status, reason, evidence_path, passed_count = self._result(path, manifest, scenario, requests)
                            evidence_url = ("/api/branch-evidence?" + urlencode({"branch": branch, "backend": backend,
                                            "model": model, "mode": mode, "scenario": scenario})) if evidence_path else None
                            row.update(status=status, reason=reason, evidence_url=evidence_url,
                                       requests_passed=passed_count)
                    rows.append(row)
        totals = {status: sum(row["status"] == status for row in rows) for status in ("passed", "failed", "missing", "stale")}
        if not rows:
            gate_status = "missing"
        elif totals["failed"]:
            gate_status = "failed"
        elif totals["stale"]:
            gate_status = "stale"
        elif totals["missing"]:
            gate_status = "missing" if not totals["passed"] else "partial"
        else:
            gate_status = "passed"
        latest = max(used, key=lambda item: (str(item[1].get("created_at", "")), item[0].stat().st_mtime_ns))[1] if used else None
        gate = {"status": gate_status, "passed": totals["passed"], "total": len(rows),
                "failed": totals["failed"], "missing": totals["missing"] + totals["stale"],
                "requests_passed": sum(row.get("requests_passed", 0) for row in rows),
                "requests_total": sum(row["requests"] for row in rows),
                "latest_commit": latest.get("commit") if latest else None,
                "run_id": latest.get("run_id") if latest else None}
        return gate, rows

    def status(self, branch: str | None = None, force: bool = False) -> dict[str, Any]:
        branch = branch or self.branch
        self._validate_branch(branch)
        errors: list[str] = []
        ref, sha = self._head(branch)
        cases = self._cases(sha)
        registry = self._registry(sha)
        branch_ci, jobs = self._branch_ci(branch, sha, force, errors)
        manifests = self._manifests()
        backends = []
        for backend in dict.fromkeys([*registry, *self.backend_prs, *self.merged_commits]):
            pr, pr_data = self._pr(backend, force, errors)
            integration = self._integration(backend, branch, sha, pr_data)
            ci = self._backend_ci(backend, integration, jobs, branch_ci, pr_data, force, errors)
            gate, results = self._gate(backend, branch, sha, cases, manifests)
            backends.append({"id": backend, "label": self.backend_labels.get(backend, LABELS.get(backend, backend)),
                             "ci_enabled": registry.get(backend, False), "integration": integration,
                             "ci": ci, "pr": pr, "gate": gate, "results": results})
        return {"branch": branch, "branch_ref": ref, "head": sha,
                "head_url": f"https://github.com/{self.github_repo}/commit/{sha}" if self.github_repo else None,
                "observed_at": _utc_now(), "vllm_version": self._version(sha),
                "branches": self._branches(), "models": self.models, "modes": self.modes,
                "cases": cases, "branch_ci": branch_ci, "backends": backends, "errors": errors}

    def refresh_branch(self, branch: str | None = None) -> dict[str, Any]:
        branch = branch or self.branch
        self._validate_branch(branch)
        fetch_error = None
        try:
            self._git("fetch", "--no-tags", self.remote,
                      f"+refs/heads/{branch}:refs/remotes/{self.remote}/{branch}", timeout=30)
        except MonitorError as exc:
            fetch_error = f"Branch fetch failed: {exc}"
        result = self.status(branch=branch, force=True)
        if fetch_error:
            result["errors"].insert(0, fetch_error)
        return result

    def read_evidence(self, branch: str, backend: str, model: str, mode: str,
                      scenario: str) -> dict[str, Any]:
        """Return a selected current-HEAD case result; never accept a file path."""
        self._validate_branch(branch)
        if model not in self.models or mode not in self.modes:
            raise MonitorError("Unknown gate model or mode")
        _, sha = self._head(branch)
        registry = self._registry(sha)
        if backend not in registry and backend not in self.backend_prs and backend not in self.merged_commits:
            raise MonitorError("Unknown backend")
        cases = {case["scenario"]: case for case in self._cases(sha)}
        if scenario not in cases:
            raise MonitorError("Unknown gate scenario")
        candidates = [(path, data) for path, data in self._manifests() if
                      data.get("backend") == backend and data.get("branch") == branch and
                      data.get("commit") == sha and data.get("model") == model and data.get("mode") == mode]
        if not candidates:
            raise MonitorError("No current-HEAD evidence manifest")
        path, manifest = max(candidates, key=lambda item: (str(item[1].get("created_at", "")), item[0].stat().st_mtime_ns))
        _, _, result_path, _ = self._result(path, manifest, scenario, cases[scenario]["requests"])
        if result_path is None or not _inside(result_path, self.evidence_dir):
            raise MonitorError("No case result in selected manifest")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MonitorError(f"Cannot read case result: {exc}") from exc
        if not isinstance(result, dict):
            raise MonitorError("Case result is not an object")
        return {"manifest": manifest, "result": result}
