#!/usr/bin/env python3
"""Read-only branch, CI, and adaptation-gate monitor.

Gate evidence is intentionally separate from GitHub CI. Fresh results bind the
exact branch HEAD to a manifest and validate every request. Audited historical
acceptance is labeled separately, requires an identical Git tree and case set,
and retains the original raw execution provenance. Neither an integrated PR
nor a successful CI job implies that the manual adaptation gate ran.

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
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from fl_dashboard.monitoring.pr_acceptance import acceptance_summary
from fl_dashboard.monitoring.pr_discovery import adaptation_backend


SHA = re.compile(r"^[0-9a-f]{40}$")
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PLATFORM = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
CASE_DIR = "tools/adaptation-gate-cases"
LABELS = {
    "cuda": "NVIDIA / 英伟达",
    "amd": "AMD / 超威半导体",
    "ascend": "Ascend / 昇腾",
    "enflame": "Enflame / 燧原",
    "hygon": "Hygon / 海光",
    "corex": "iluvatar / 天数智芯",
    "kunlunxin": "Kunlunxin / 昆仑芯",
    "metax": "MetaX / 沐曦",
    "musa": "Moore Threads / 摩尔线程",
    "sunrise": "Sunrise / 曦望",
    "thead": "T-Head / 平头哥",
    "tsingmicro": "Tsingmicro / 清微"
}


class MonitorError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


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
                 transport: Callable[[str], dict[str, Any] | list[Any]] | None = None):
        if isinstance(config, (str, Path)):
            config_path = Path(config).expanduser().resolve()
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            base = config_path.parent
        else:
            raw = config
            base = Path.cwd()
        self.base = base
        if not isinstance(raw, dict):
            raise MonitorError("Monitor config must be an object")
        self.config = raw.get("monitor", raw)
        if not isinstance(self.config, dict):
            raise MonitorError("monitor must be an object")
        self.engine = self.config.get("engine", "vllm")
        if self.engine not in ("vllm", "sglang"):
            raise MonitorError("engine must be vllm or sglang")
        self.case_dir = self.config.get("case_dir", "examples" if self.engine == "sglang" else CASE_DIR)
        if (not isinstance(self.case_dir, str) or len(self.case_dir) > 512 or
                not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."}
                        for part in self.case_dir.split("/"))):
            raise MonitorError("case_dir must be a relative repository directory without path escapes")
        default_sources = (["examples", "tests/e2e_tests/concurrent"] if self.engine == "sglang" else [self.case_dir])
        self.case_sources = self.config.get("case_sources", [self.case_dir] if "case_dir" in self.config else default_sources)
        if (not isinstance(self.case_sources, list) or not self.case_sources or len(self.case_sources) > 20 or
                not all(isinstance(source, str) and len(source) <= 512 and
                        all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."}
                            for part in source.split("/")) for source in self.case_sources)):
            raise MonitorError("case_sources must be nonempty relative repository paths without escapes")
        self._sglang_case_cache = {}
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
        # Dashboard backend IDs may differ from the workflow platform names.
        # CoreX is registered as iluvatar in the plugin's CI workflow.
        self.backend_ci_platforms = self.config.get("backend_ci_platforms", {"corex": "iluvatar"})
        self.accepted_ci_runs = self.config.get("accepted_ci_runs", {})
        self.backend_pr_discovery = self.config.get("backend_pr_discovery", {})
        if not isinstance(self.backend_prs, dict) or not isinstance(self.merged_commits, dict):
            raise MonitorError("backend_prs and merged_commits must be objects")
        if not isinstance(self.backend_labels, dict):
            raise MonitorError("backend_labels must be an object")
        if (not isinstance(self.backend_ci_platforms, dict) or
                not all(isinstance(backend, str) and PLATFORM.fullmatch(backend) and
                        isinstance(platform, str) and PLATFORM.fullmatch(platform)
                        for backend, platform in self.backend_ci_platforms.items())):
            raise MonitorError("backend_ci_platforms must map backend IDs to workflow platform names")
        if len(set(self.backend_ci_platforms.values())) != len(self.backend_ci_platforms):
            raise MonitorError("backend_ci_platforms must use distinct workflow platform names")
        self._ci_backend_ids = {platform: backend for backend, platform in self.backend_ci_platforms.items()}
        if not isinstance(self.accepted_ci_runs, dict):
            raise MonitorError("accepted_ci_runs must be an object")
        if not isinstance(self.backend_pr_discovery, dict):
            raise MonitorError("backend_pr_discovery must be an object")
        for selector in self.backend_pr_discovery.values():
            if not isinstance(selector, dict):
                raise MonitorError("PR discovery selectors must be objects")
            prefixes = selector.get("head_prefixes")
            terms = selector.get("title_terms", [])
            for values, required in ((prefixes, True), (terms, False)):
                if (not isinstance(values, list) or len(values) > 10 or (required and not values) or
                        not all(isinstance(value, str) and value.strip() and len(value) <= 200 and
                                not any(ord(char) < 32 for char in value) for value in values)):
                    raise MonitorError("PR discovery requires explicit head_prefixes and optional title_terms")
        self.transport = transport or self._http_json
        self._api_cache: dict[str, tuple[float, dict[str, Any] | list[Any]]] = {}
        self._cache_lock = threading.RLock()
        self._auto_refresh_lock = threading.Lock()
        self._auto_refresh_times = {self.branch: time.monotonic()}

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

    @staticmethod
    def _github_remote_repo(url: str) -> str | None:
        """Identify ordinary GitHub clone URLs without exposing embedded credentials."""
        if not isinstance(url, str) or any(char.isspace() for char in url):
            return None
        scp = re.fullmatch(r"git@github\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", url, re.I)
        if scp:
            return scp.group(1).casefold()
        try:
            parsed = urlsplit(url)
            if (parsed.hostname != "github.com" or parsed.query or parsed.fragment or
                    parsed.scheme not in {"https", "ssh"} or
                    parsed.port not in (None, 443 if parsed.scheme == "https" else 22) or
                    (parsed.scheme == "ssh" and parsed.username != "git")):
                return None
        except ValueError:
            return None
        path = re.fullmatch(r"/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", parsed.path)
        return path.group(1).casefold() if path else None

    def _resolved_remote(self, *, allow_tracking_only: bool = True) -> str:
        """Recover an alias rename only when the target repository is unambiguous.

        Resolve each operation so a running monitor survives later Git remote
        renames. Never rewrite the user's remotes or assume origin is upstream.
        """
        names = self._git("remote").stdout.splitlines()
        if self.remote in names:
            return self.remote
        candidates = []
        if self.github_repo:
            for name in names:
                if not REF.fullmatch(name):
                    continue
                url = self._git("remote", "get-url", name, check=False)
                if url.returncode == 0 and self._github_remote_repo(url.stdout.strip()) == self.github_repo.casefold():
                    candidates.append(name)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise MonitorError("Multiple remotes match the monitored repository; configure its remote alias",
                               code="remote_ambiguous")
        # Keep read-only inspection of deliberately offline repositories. Once
        # any remote exists, an unrelated clone must never supply fallback refs.
        if allow_tracking_only and not names:
            refs = self._git("for-each-ref", "--format=%(refname)", f"refs/remotes/{self.remote}/")
            if refs.stdout.strip():
                return self.remote
        raise MonitorError("Configured remote is unavailable and no remote matches the monitored repository",
                           code="remote_missing")

    def _head(self, branch: str) -> tuple[str, str]:
        ref = f"refs/remotes/{self._resolved_remote()}/{branch}"
        sha = self._git("rev-parse", "--verify", ref).stdout.strip().lower()
        if not SHA.fullmatch(sha):
            raise MonitorError(f"Invalid SHA for {ref}")
        return ref, sha

    def _show(self, sha: str, path: str) -> str:
        return self._git("show", f"{sha}:{path}").stdout

    def _branches(self) -> list[str]:
        prefix = f"refs/remotes/{self._resolved_remote()}/"
        done = self._git("for-each-ref", "--format=%(refname)", prefix)
        return sorted(ref[len(prefix):] for ref in done.stdout.splitlines()
                      if ref.startswith(prefix) and not ref.endswith("/HEAD"))

    def branch_options(self) -> dict[str, Any]:
        """List local remote-tracking refs without collecting PRs, CI or evidence."""
        return {"branch": self.branch, "branches": self._branches()}

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

    def _ci_platform(self, backend: str) -> str:
        return self.backend_ci_platforms.get(backend, backend)

    def _ci_test_jobs(self, backend: str, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prefix = f"test-{self._ci_platform(backend)} / "
        return [job for job in jobs if job.get("name", "").startswith(prefix) and
                not any(part in job.get("name", "") for part in (" / notify / ", " / Setup ("))]

    def _version(self, sha: str) -> str | None:
        try:
            data = tomllib.loads(self._show(sha, "pyproject.toml"))
            project = data.get("project", {})
            dependencies = list(project.get("dependencies", []))
            for optional in project.get("optional-dependencies", {}).values():
                dependencies.extend(optional)
            for dep in dependencies:
                match = re.match(re.escape(self.engine) + r"(?:\[[^]]+\])?\s*==\s*([^\s;,]+)", dep, re.I)
                if match:
                    return match.group(1)
        except (MonitorError, tomllib.TOMLDecodeError, TypeError):
            pass
        return None

    def _cases(self, sha: str) -> list[dict[str, Any]]:
        if self.engine == "sglang":
            from fl_dashboard.monitoring.sglang_evidence import discover_cases
            if sha not in self._sglang_case_cache:
                self._sglang_case_cache[sha] = discover_cases(
                    self._show,
                    lambda commit, prefix: self._git("ls-tree", "-r", "--name-only", commit, "--", prefix).stdout.splitlines(),
                    lambda commit, source: self._git("rev-parse", f"{commit}:{source}").stdout.strip(),
                    sha, self.models, self.case_sources)
            return self._sglang_case_cache[sha][0]
        paths = self._git("ls-tree", "-r", "--name-only", sha, "--", self.case_dir).stdout.splitlines()
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

    def _http_json(self, url: str) -> dict[str, Any] | list[Any]:
        gh = shutil.which("gh")
        if gh:
            try:
                done = subprocess.run([gh, "api", url], capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=10)
                if done.returncode == 0:
                    data = json.loads(done.stdout)
                    if isinstance(data, (dict, list)):
                        return data
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "fl-branch-monitor"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with urlopen(Request(url, headers=headers), timeout=6) as response:
            data = json.load(response)
        if not isinstance(data, (dict, list)):
            raise MonitorError("GitHub API response must be an object or array")
        return data

    def _api_json(self, path: str, force: bool) -> dict[str, Any] | list[Any]:
        url = f"https://api.github.com/repos/{self.github_repo}/{path}"
        with self._cache_lock:
            cached = self._api_cache.get(url)
            if cached and not force and time.monotonic() - cached[0] < 300:
                return cached[1]
        value = self.transport(url)
        if not isinstance(value, (dict, list)):
            raise MonitorError("GitHub transport must return an object or array")
        with self._cache_lock:
            self._api_cache[url] = (time.monotonic(), value)
        return value

    def _api(self, path: str, force: bool) -> dict[str, Any]:
        value = self._api_json(path, force)
        if not isinstance(value, dict):
            raise MonitorError("GitHub API response must be an object")
        return value

    def _candidate_prs(self, branch: str, force: bool,
                       errors: list[str] | None = None) -> list[dict[str, Any]]:
        # Include merged adaptations so discovery survives a merge or restart.
        # If history exceeds the bound, still collect the complete open set;
        # an incomplete historical page must not choose an arbitrary old merge.
        for state in ("all", "open"):
            pulls = []
            for page in range(1, 4):
                query = urlencode({"state": state, "base": branch, "sort": "updated",
                                   "direction": "desc", "per_page": 100, "page": page})
                values = self._api_json(f"pulls?{query}", force)
                if not isinstance(values, list):
                    raise MonitorError("GitHub PR list must be an array")
                pulls.extend(value for value in values if isinstance(value, dict) and
                             (value.get("state") == "open" or
                              (state == "all" and value.get("state") == "closed" and value.get("merged_at"))) and
                             isinstance(value.get("base"), dict) and value["base"].get("ref") == branch)
                if len(values) < 100:
                    return pulls
            if state == "all" and errors is not None:
                errors.append("Merged PR history exceeds the discovery limit; configure an explicit backend PR for older merges")
        raise MonitorError("Too many open PRs; configure an explicit backend PR")

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

    @staticmethod
    def _ci_check_url(checks: list[dict[str, Any]], status: str) -> str | None:
        if status == "failed":
            relevant = [check for check in checks if check.get("conclusion") in
                        ("failure", "timed_out", "action_required", "startup_failure")]
        elif status == "running":
            # Prefer a job actually executing over an earlier queued check.
            relevant = [check for check in checks if check.get("status") == "in_progress"]
            relevant += [check for check in checks if check.get("status") not in ("completed", "in_progress")]
        elif status in ("cancelled", "skipped", "partial", "passed"):
            conclusion = {"cancelled": "cancelled", "skipped": "skipped",
                          "partial": "skipped", "passed": "success"}[status]
            relevant = [check for check in checks if check.get("conclusion") == conclusion]
        else:
            relevant = checks
        return next((check.get("details_url") or check.get("html_url") for check in relevant
                     if check.get("details_url") or check.get("html_url")), None)

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

    def _pr_selector(self, backend: str) -> dict[str, Any] | None:
        if backend in self.backend_pr_discovery:
            return self.backend_pr_discovery[backend]
        if not isinstance(backend, str) or not PLATFORM.fullmatch(backend):
            return None
        return {"content_backend": backend}

    @staticmethod
    def _matches_pr_selector(pr: dict[str, Any], branch: str, selector: dict[str, Any]) -> bool:
        base, candidate_head = pr.get("base"), pr.get("head")
        eligible = pr.get("state") == "open" or (pr.get("state") == "closed" and
                    (pr.get("merged") is True or bool(pr.get("merged_at"))))
        if (not eligible or type(pr.get("number")) is not int or pr["number"] <= 0 or
                not isinstance(base, dict) or base.get("ref") != branch or
                not isinstance(candidate_head, dict) or not isinstance(candidate_head.get("ref"), str)):
            return False
        if selector.get("content_backend"):
            return adaptation_backend(pr) == selector["content_backend"]
        return (any(candidate_head["ref"].startswith(prefix) for prefix in selector["head_prefixes"]) and
                (not selector.get("title_terms") or
                 any(term.casefold() in str(pr.get("title", "")).casefold() for term in selector["title_terms"])))

    def _pr(self, backend: str, branch: str, force: bool, errors: list[str],
            candidate_prs: list[dict[str, Any]] | None = None, head: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        value = self.backend_prs.get(backend)
        number = value.get("number") if isinstance(value, dict) else value
        number = number if type(number) is int and number > 0 else None
        data = None
        selected_by = "configured"
        if number and self.github_repo:
            try:
                data = self._api(f"pulls/{number}", force)
            except (OSError, ValueError, MonitorError) as exc:
                errors.append(f"GitHub PR #{number} unavailable: {exc}")
        selector = self._pr_selector(backend)
        accepted = (data and data.get("merged") and data.get("base", {}).get("ref") == branch and
                    head and self._ancestor(data.get("merge_commit_sha"), head))
        # An explicitly associated active PR remains authoritative under defaults,
        # even when its maintainer chose a non-canonical source branch name.
        configured_open = (backend not in self.backend_pr_discovery and data and
                           data.get("state") == "open" and data.get("base", {}).get("ref") == branch)
        mapped_commit = self.merged_commits.get(backend)
        mapped_merged = (number is None and backend not in self.backend_pr_discovery and
                         branch == self.branch and head and isinstance(mapped_commit, str) and
                         self._ancestor(mapped_commit.lower(), head))
        if selector and candidate_prs is not None and not accepted and not configured_open and not mapped_merged:
            candidates = list({pr["number"]: pr for pr in candidate_prs
                               if self._matches_pr_selector(pr, branch, selector)}.values())
            active = [pr for pr in candidates if pr.get("state") == "open"]
            merged = [pr for pr in candidates if pr.get("state") == "closed" and head and
                      self._ancestor(pr.get("merge_commit_sha"), head)]
            configured = next((pr for pr in active if pr.get("number") == number), None)
            # Keep tracking the current adaptation while open; after merging,
            # recover it from GitHub history, including after a service restart.
            candidate = configured or (active[0] if len(active) == 1 else None)
            if not active and merged:
                candidate = max(merged, key=lambda pr: (str(pr.get("merged_at") or ""), pr["number"]))
            if candidate and candidate.get("number") != number:
                try:
                    replacement = self._api(f"pulls/{candidate['number']}", force)
                    if (replacement.get("number") == candidate["number"] and
                            self._matches_pr_selector(replacement, branch, selector) and
                            (replacement.get("state") == "open" or
                             (replacement.get("merged") is True and head and
                              self._ancestor(replacement.get("merge_commit_sha"), head)))):
                        number, data, selected_by = candidate["number"], replacement, "discovered"
                    else:
                        errors.append(f"Discovered {backend} PR no longer matches the target branch or selector")
                except (KeyError, OSError, ValueError, MonitorError) as exc:
                    errors.append(f"Discovered {backend} PR unavailable: {exc}")
            elif not configured and len(active) > 1:
                errors.append(f"Multiple open {backend} adaptation PRs target this branch; configure the intended PR number")
        # Configured numbers are hints, not proof of branch membership. Apply
        # the same boundary after every discovery/fallback path so stale pins
        # cannot leak PR links, CI, or acceptance references into another branch.
        if (number is None or not data or data.get("number") != number or
                not isinstance(data.get("base"), dict) or data["base"].get("ref") != branch):
            return None, None
        public = {"number": number, "url": f"https://github.com/{self.github_repo}/pull/{number}" if self.github_repo else None,
                  "title": None, "draft": None, "state": None, "merged": None, "selected_by": selected_by}
        if data:
            public.update(url=data.get("html_url", public["url"]), title=data.get("title"), draft=data.get("draft"),
                          state=data.get("state"), merged=data.get("merged"))
        return public, data

    def _ancestor(self, ancestor: str, head: str) -> bool:
        if not isinstance(ancestor, str) or not SHA.fullmatch(ancestor.lower()):
            return False
        return self._git("merge-base", "--is-ancestor", ancestor, head, check=False).returncode == 0

    def _tree(self, commit: str) -> str | None:
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            return None
        done = self._git("rev-parse", "--verify", f"{commit}^{{tree}}", check=False)
        value = done.stdout.strip()
        return value if done.returncode == 0 and SHA.fullmatch(value) else None

    def _accepted_ci(self, backend: str, branch: str, sha: str, force: bool,
                     errors: list[str]) -> dict[str, Any] | None:
        record = self.accepted_ci_runs.get(backend)
        if not isinstance(record, dict) or record.get("branch") != branch or not self.github_repo:
            return None
        commit = record.get("commit")
        tree = self._tree(commit)
        if not tree or tree != self._tree(sha):
            return None
        run_id, attempt = record.get("run_id"), record.get("attempt")
        if type(run_id) is not int or type(attempt) is not int or run_id < 1 or attempt < 1:
            errors.append(f"Invalid accepted CI run for {backend}")
            return None
        try:
            endpoint = f"actions/runs/{run_id}/attempts/{attempt}"
            run = self._api(endpoint, force)
            if run.get("head_sha") != commit or run.get("run_attempt") != attempt:
                raise MonitorError("Accepted CI run commit or attempt does not match its record")
            data = self._api(endpoint + "/jobs?per_page=100", force)
            jobs = self._ci_test_jobs(backend, data.get("jobs", []))
            return {"status": self._ci_status(jobs), "scope": "accepted-tree", "commit": commit,
                    "target_commit": sha, "tree": tree, "tree_matches_head": True,
                    "url": run.get("html_url"), "run_id": run_id, "attempt": attempt,
                    "detail": f"适配 CI 第 {attempt} 次运行，{len(jobs)} 个 backend 测试；验收代码树与当前分支相同"}
        except (OSError, ValueError, MonitorError) as exc:
            errors.append(f"Accepted {backend} CI unavailable: {exc}")
            return None

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

    def _branch_backend_ci(self, backend: str, branch_jobs: list[dict[str, Any]],
                           branch_ci: dict[str, Any]) -> dict[str, Any]:
        jobs = self._ci_test_jobs(backend, branch_jobs)
        if not jobs:
            jobs = [job for job in branch_jobs if job.get("name") == f"test-{self._ci_platform(backend)}"]
        status = self._ci_status(jobs)
        return {"status": status, "scope": "branch", "detail": f"{len(jobs)} backend test jobs at branch HEAD",
                "url": self._ci_check_url(jobs, status) or branch_ci.get("url"), "commit": branch_ci.get("commit")}

    def _backend_ci(self, backend: str, integration: dict[str, Any], branch_jobs: list[dict[str, Any]],
                    branch_ci: dict[str, Any], pr_data: dict[str, Any] | None, force: bool,
                    errors: list[str], branch: str | None = None) -> dict[str, Any]:
        pr = pr_data or {}
        head_sha = pr.get("head", {}).get("sha")
        merge_sha = pr.get("merge_commit_sha")
        merged_pr = (integration["status"] == "merged" and pr.get("merged") is True and
                     pr.get("base", {}).get("ref") == (branch or self.branch) and
                     isinstance(merge_sha, str) and integration.get("commit") == merge_sha.lower() and
                     isinstance(head_sha, str) and SHA.fullmatch(head_sha.lower()) and
                     self._ancestor(merge_sha.lower(), branch_ci.get("commit", "")))
        # Keep the adaptation's tested PR commit after a verified merge. A later
        # branch run describes a different revision and belongs in current_ci.
        if integration["status"] == "merged" and not merged_pr:
            return self._branch_backend_ci(backend, branch_jobs, branch_ci)
        scope = "merged-pr" if merged_pr else "pr"
        if integration["status"] not in ("open", "draft") and not merged_pr:
            return {"status": "unknown", "scope": scope, "detail": "No adaptation PR targeting this branch",
                    "url": None, "commit": None}
        if not self.github_repo or not isinstance(head_sha, str) or not SHA.fullmatch(head_sha.lower()):
            return {"status": "unknown", "scope": scope, "detail": "No candidate branch CI data", "url": None, "commit": head_sha}
        head_sha = head_sha.lower()
        try:
            checks = []
            for page in range(1, 11):
                suffix = "" if page == 1 else f"&page={page}"
                data = self._api(f"commits/{head_sha}/check-runs?per_page=100{suffix}", force)
                batch = data.get("check_runs", [])
                checks.extend(batch)
                if len(batch) < 100:
                    break
            else:
                raise MonitorError("Too many PR checks to determine backend CI completely")
            checks = self._ci_test_jobs(backend, checks)
            status = self._ci_status(checks)
            detail = ("已合入适配 PR" if merged_pr else "适配 PR") + f" 提交上的 {len(checks)} 个 backend 测试"
            return {"status": status, "scope": scope, "detail": detail,
                    "url": self._ci_check_url(checks, status), "commit": head_sha}
        except (OSError, ValueError, MonitorError) as exc:
            errors.append(f"GitHub {backend} checks unavailable: {exc}")
            return {"status": "unknown", "scope": scope, "detail": "Adaptation PR backend CI unavailable", "url": None, "commit": head_sha}

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

    def _acceptance_records(self, backend: str, branch: str, sha: str, pr: dict | None) -> list[dict]:
        from fl_dashboard.monitoring.acceptance_records import AcceptanceRecords
        store = AcceptanceRecords(self.evidence_dir, self.engine, self.github_repo)
        records = []
        current_tree = None
        for entry in store.entries(backend, branch, pr):
            record = store.public(entry)
            if record.get("tested_commit") and record.get("tested_tree"):
                tested_tree = self._tree(record["tested_commit"])
                if current_tree is None:
                    current_tree = self._tree(sha)
                record["tree_matches_head"] = bool(tested_tree and tested_tree == record["tested_tree"] == current_tree)
            else:
                record["tree_matches_head"] = False
            if record.get("archive_available"):
                record["archive_url"] = "/api/acceptance-record?" + urlencode({"branch": branch, "backend": backend, "record": record["id"]})
            records.append(record)
        return records

    def acceptance_record(self, backend: str, record_id: str, branch: str | None = None) -> dict:
        from fl_dashboard.monitoring.acceptance_records import AcceptanceRecords
        branch = branch or self.branch
        self._validate_branch(branch)
        try:
            return AcceptanceRecords(self.evidence_dir, self.engine, self.github_repo).read_archive(backend, branch, record_id)
        except (ValueError, UnicodeError) as exc:
            raise MonitorError(str(exc)) from None

    def _audit_record(self, backend: str, branch: str) -> dict[str, Any] | None:
        path = self.evidence_dir / backend / "acceptance.json"
        if not _inside(path, self.evidence_dir) or not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (not isinstance(record, dict) or record.get("schema_version") != 1 or
                record.get("evidence_type") != "audited_record" or record.get("backend") != backend or
                record.get("branch") != branch or not record.get("sources")):
            return None
        return record

    def _audit_matches(self, record: dict[str, Any], sha: str, cases: list[dict[str, Any]]) -> bool:
        tree = self._tree(record.get("tested_commit"))
        if not tree or tree != record.get("tree") or tree != self._tree(sha) or not cases:
            return False
        gate = record.get("gate", {})
        if not isinstance(gate, dict) or gate.get("status") != "passed":
            return False
        definitions = gate.get("scenario_definitions", [])
        if not isinstance(definitions, list) or len(definitions) != len(cases):
            return False
        expected = {(case["scenario"], case["requests"]) for case in cases}
        if any(not isinstance(row, dict) for row in definitions):
            return False
        if {(row.get("scenario"), row.get("requests")) for row in definitions} != expected:
            return False
        groups = gate.get("groups", [])
        matrix = {(model, mode) for model in self.models for mode in self.modes}
        if not isinstance(groups, list) or len(groups) != len(matrix) or any(not isinstance(row, dict) for row in groups):
            return False
        if {(row.get("model"), row.get("mode")) for row in groups} != matrix:
            return False
        requests = sum(case["requests"] for case in cases)
        return (all(row.get("scenarios_passed") == len(cases) and row.get("scenarios_total") == len(cases) and
                    row.get("requests_total") == requests for row in groups) and
                gate.get("scenarios_passed") == len(cases) * len(matrix) and
                gate.get("scenarios_total") == len(cases) * len(matrix) and
                gate.get("requests_total") == requests * len(matrix))

    def _audit_case(self, record: dict[str, Any], model: str, mode: str, scenario: str,
                    requests: int) -> tuple[str, str, Path | None, int]:
        group = next((row for row in record["gate"]["groups"] if row["model"] == model and row["mode"] == mode), {})
        if not group.get("raw_results_dir"):
            return "missing", "原始请求 JSON 未归档", None, 0
        manifest = {"schema_version": 1, "run_id": "historical-acceptance", "model": model,
                    "port": group.get("port"), "results_dir": group["raw_results_dir"]}
        return self._result(self.evidence_dir / record["backend"] / "acceptance.json", manifest, scenario, requests)

    def _sglang_result(self, manifest_path: Path, manifest: dict[str, Any], case: dict[str, Any] | None) -> tuple[str, str, Path | None, int]:
        from fl_dashboard.monitoring.sglang_evidence import validate_result
        if (manifest.get("schema_version") != 1 or manifest.get("engine") != "sglang" or
                not isinstance(manifest.get("run_id"), str) or not manifest["run_id"] or not case):
            return "failed", "Invalid SGLang manifest schema, engine, or source case", None, 0
        directory = manifest.get("results_dir")
        if not isinstance(directory, str) or not directory:
            return "failed", "Missing SGLang results_dir", None, 0
        results_dir = (manifest_path.parent / directory).resolve()
        if not _inside(results_dir, self.evidence_dir):
            return "failed", "results_dir escapes evidence_dir", None, 0
        path = results_dir / (case["scenario"] + ".json")
        if not path.is_file() or not _inside(path, self.evidence_dir):
            return "missing", "SGLang raw-request JSON is missing", None, 0
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "failed", "SGLang raw-request JSON is invalid", path, 0
        status, reason, passed = validate_result(value, manifest, case)
        return status, reason, path, passed

    def _result(self, manifest_path: Path, manifest: dict[str, Any], scenario: str, requests: int,
                case: dict[str, Any] | None = None) -> tuple[str, str, Path | None, int]:
        if self.engine == "sglang":
            return self._sglang_result(manifest_path, manifest, case)
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
        if not cases:
            return {"status": "unknown", "passed": 0, "total": 0, "failed": 0, "missing": 0,
                    "requests_passed": 0, "requests_total": 0, "latest_commit": None, "run_id": None,
                    "detail": f"所选分支的 {self.case_dir} 未发现可验证的 Gate case；不能判定通过。"}, []
        rows: list[dict[str, Any]] = []
        used: list[tuple[Path, dict[str, Any]]] = []
        audit = self._audit_record(backend, branch) if self.engine == "vllm" else None
        audit_matches = bool(audit and self._audit_matches(audit, sha, cases))
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
                    if (self.engine == "sglang" and
                            (model not in case.get("models", []) or mode not in case.get("modes", []))):
                        continue
                    scenario, requests = case["scenario"], case["requests"]
                    row = {"model": model, "mode": mode, "scenario": scenario, "requests": requests,
                           "status": "missing", "reason": "No evidence manifest", "evidence_url": None}
                    if self.engine == "sglang":
                        row.update(label=case.get("label"), source=case["source"], source_blob=case["source_blob"],
                                   evidence_format="sglang_raw_requests")
                    if selected:
                        path, manifest = selected
                        if manifest.get("commit") != sha:
                            row.update(status="stale", reason=f"Evidence belongs to {manifest.get('commit')}")
                        else:
                            status, reason, evidence_path, passed_count = self._result(path, manifest, scenario, requests, case)
                            evidence_url = ("/api/branch-evidence?" + urlencode({"branch": branch, "backend": backend,
                                            "model": model, "mode": mode, "scenario": scenario})) if evidence_path else None
                            row.update(status=status, reason=reason, evidence_url=evidence_url,
                                       requests_passed=passed_count)
                    if row["status"] in ("missing", "stale") and audit_matches:
                        row.update(status="recorded_pass", evidence_type="audited_record",
                                   reason=f"该模型和模式的 {len(cases)}/{len(cases)} 验收记录；原始请求 JSON 未归档",
                                   evidence_url="/api/branch-evidence?" + urlencode({"branch": branch, "backend": backend,
                                       "model": model, "mode": mode, "scenario": scenario}), requests_passed=None)
                        raw_status, raw_reason, raw_path, raw_passed = self._audit_case(audit, model, mode, scenario, requests)
                        if raw_status == "passed":
                            row.update(reason="历史验收记录；原始请求 JSON 已核验，运行来源见证据", requests_passed=raw_passed,
                                       raw_results_available=True)
                        elif raw_status == "failed":
                            row.update(status="failed", reason="验收记录与原始结果冲突：" + raw_reason, requests_passed=raw_passed)
                    elif row["status"] == "missing" and audit and not audit_matches:
                        row.update(status="stale", reason="历史验收记录与当前代码树或用例清单不一致")
                    rows.append(row)
        totals = {status: sum(row["status"] == status for row in rows) for status in ("passed", "recorded_pass", "failed", "missing", "stale")}
        if not rows:
            gate_status = "missing"
        elif totals["failed"]:
            gate_status = "failed"
        elif totals["stale"]:
            gate_status = "stale"
        elif totals["missing"]:
            gate_status = "missing" if not totals["passed"] else "partial"
        elif totals["recorded_pass"]:
            gate_status = "recorded_pass"
        else:
            gate_status = "passed"
        latest = max(used, key=lambda item: (str(item[1].get("created_at", "")), item[0].stat().st_mtime_ns))[1] if used else None
        discovery_warnings = self._sglang_case_cache.get(sha, ([], []))[1] if self.engine == "sglang" else []
        if gate_status == "passed" and discovery_warnings:
            gate_status = "partial"
        gate = {"status": gate_status, "passed": totals["passed"] + totals["recorded_pass"], "total": len(rows),
                "failed": totals["failed"], "missing": totals["missing"] + totals["stale"],
                "requests_passed": None if any(row.get("requests_passed") is None and row["status"] == "recorded_pass" for row in rows)
                    else sum(row.get("requests_passed", 0) or 0 for row in rows),
                "requests_total": sum(row["requests"] for row in rows),
                "latest_commit": latest.get("commit") if latest else None,
                "run_id": latest.get("run_id") if latest else None}
        if self.engine == "sglang":
            gate.update(evidence_format="sglang_raw_requests", case_sources=self.case_sources,
                        detail="原始脚本没有结果 JSON；归档后逐请求重核输入、输出断言与所选提交源码。" +
                               ("部分用例来源无法读取，不能判定完整验收通过。" if discovery_warnings else ""))
        if audit:
            gate.update(audit_sources=audit.get("sources"), source_thread=audit.get("source_thread"),
                        evidence_type="audited_record" if totals["recorded_pass"] else "raw_json",
                        tree_matches_head=audit_matches, recorded_cases=totals["recorded_pass"])
            if totals["recorded_pass"]:
                raw_available = all(row.get("raw_results_available") for row in rows if row["status"] == "recorded_pass")
                gate.update(latest_commit=audit.get("tested_commit"), run_id="historical-acceptance",
                            raw_results_available=raw_available,
                            detail=f"历史验收 {gate['passed']}/{len(rows)}；已核对验收代码树相同。" +
                                ("原始请求 JSON 已归档，原始运行时间与提交见证据。" if raw_available else "原始请求 JSON 未归档。"))
        return gate, rows

    def status(self, branch: str | None = None, force: bool = False) -> dict[str, Any]:
        # Polling must refresh remote refs as well as cached GitHub metadata.
        selected = branch or self.branch
        offline_refresh_error = None
        if not force and time.monotonic() - self._auto_refresh_times.get(selected, float("-inf")) >= 60:
            if self._auto_refresh_lock.acquire(blocking=False):
                try:
                    if time.monotonic() - self._auto_refresh_times.get(selected, float("-inf")) >= 60:
                        try:
                            return self.refresh_branch(branch=branch)
                        except MonitorError as exc:
                            if exc.code != "remote_missing":
                                raise
                            # Existing refs may be intentionally inspected in an
                            # offline clone. Preserve that data, but never claim
                            # an automatic refresh fetched a current revision.
                            self._resolved_remote()
                            offline_refresh_error = "Git remote is unavailable; showing local tracking refs"
                finally:
                    self._auto_refresh_times[selected] = time.monotonic()
                    self._auto_refresh_lock.release()
        branch = branch or self.branch
        self._validate_branch(branch)
        errors: list[str] = [offline_refresh_error] if offline_refresh_error else []
        ref, sha = self._head(branch)
        cases = self._cases(sha)
        registry = self._registry(sha)
        branch_ci, jobs = self._branch_ci(branch, sha, force, errors)
        manifests = self._manifests()
        registry_backends = [self._ci_backend_ids.get(platform, platform) for platform in registry]
        backend_ids = list(dict.fromkeys([*registry_backends, *self.backend_labels, *self.backend_prs,
                                          *self.merged_commits, *self.backend_pr_discovery]))
        candidate_prs = None
        if backend_ids and self.github_repo:
            try:
                candidate_prs = self._candidate_prs(branch, force, errors)
            except (OSError, ValueError, MonitorError) as exc:
                errors.append(f"Backend PR discovery unavailable: {exc}")
        # New backend adaptations can precede their platforms.yml entry. Use
        # the selected branch's verified PR purpose, without changing the
        # configured backend set or leaking rows into other release branches.
        discovered_backends = set()
        for candidate in candidate_prs or []:
            backend = adaptation_backend(candidate)
            if (backend and backend not in backend_ids and
                    self._matches_pr_selector(candidate, branch, {"content_backend": backend}) and
                    (candidate.get("state") == "open" or
                     self._ancestor(candidate.get("merge_commit_sha"), sha))):
                backend_ids.append(backend)
                discovered_backends.add(backend)
        backends = []
        for backend in backend_ids:
            pr, pr_data = self._pr(backend, branch, force, errors, candidate_prs, sha)
            if backend in discovered_backends and pr is None:
                continue
            integration = self._integration(backend, branch, sha, pr_data)
            ci = self._backend_ci(backend, integration, jobs, branch_ci, pr_data, force, errors, branch=branch)
            current_ci = self._branch_backend_ci(backend, jobs, branch_ci) if integration["status"] == "merged" else ci
            accepted_ci = self._accepted_ci(backend, branch, sha, force, errors)
            if accepted_ci:
                ci = accepted_ci
            gate, results = self._gate(backend, branch, sha, cases, manifests)
            records = self._acceptance_records(backend, branch, sha, pr)
            acceptance = acceptance_summary(pr, pr_data, records, gate, branch=branch, head=sha)
            backends.append({"id": backend, "label": self.backend_labels.get(backend, LABELS.get(backend, backend)),
                             "ci_enabled": registry.get(self._ci_platform(backend), False), "integration": integration,
                             "ci": ci, "current_ci": current_ci, "pr": pr, "gate": gate, "results": results,
                             "acceptance_records": records, "acceptance": acceptance})
        backends.sort(key=lambda item: (item["id"] != "cuda", str(item["label"]).casefold(), item["id"]))
        upstream_version = self._version(sha)
        return {"branch": branch, "branch_ref": ref, "head": sha,
                "head_url": f"https://github.com/{self.github_repo}/commit/{sha}" if self.github_repo else None,
                "observed_at": _utc_now(), "engine": self.engine, "upstream_version": upstream_version,
                "vllm_version": upstream_version if self.engine == "vllm" else None,
                "case_dir": self.case_dir, "case_sources": self.case_sources,
                "case_discovery": {"warnings": self._sglang_case_cache.get(sha, ([], []))[1]} if self.engine == "sglang" else {},
                "branches": self._branches(), "models": self.models, "modes": self.modes,
                "cases": cases, "branch_ci": branch_ci, "backends": backends, "errors": errors}

    def refresh_branch(self, branch: str | None = None) -> dict[str, Any]:
        branch = branch or self.branch
        self._validate_branch(branch)
        fetch_error = None
        remote = self._resolved_remote(allow_tracking_only=False)
        try:
            self._git("fetch", "--no-tags", remote,
                      f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}", timeout=30)
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
        if (self._ci_platform(backend) not in registry and backend not in self.backend_labels and
                backend not in self.backend_prs and backend not in self.merged_commits and
                backend not in self.backend_pr_discovery):
            raise MonitorError("Unknown backend")
        cases = {case["scenario"]: case for case in self._cases(sha)}
        if scenario not in cases:
            raise MonitorError("Unknown gate scenario")
        if self.engine == "sglang" and (model not in cases[scenario].get("models", []) or mode not in cases[scenario].get("modes", [])):
            raise MonitorError("SGLang scenario does not apply to this model or runtime mode")
        def historical_evidence() -> dict[str, Any]:
            if self.engine == "sglang":
                raise MonitorError("No current-HEAD SGLang raw-request evidence")
            audit = self._audit_record(backend, branch)
            if not audit or not self._audit_matches(audit, sha, list(cases.values())):
                raise MonitorError("No current-HEAD evidence manifest or matching acceptance record")
            status, reason, path, _ = self._audit_case(audit, model, mode, scenario, cases[scenario]["requests"])
            raw = json.loads(path.read_text(encoding="utf-8")) if path and _inside(path, self.evidence_dir) else None
            return {"evidence_type": "audited_record", "record": audit, "raw_result": raw,
                    "raw_validation": status, "note": reason,
                    "provenance": next(row for row in audit["gate"]["groups"] if row["model"] == model and row["mode"] == mode)}
        candidates = [(path, data) for path, data in self._manifests() if
                      data.get("backend") == backend and data.get("branch") == branch and
                      data.get("commit") == sha and data.get("model") == model and data.get("mode") == mode]
        if not candidates:
            return historical_evidence()
        path, manifest = max(candidates, key=lambda item: (str(item[1].get("created_at", "")), item[0].stat().st_mtime_ns))
        _, _, result_path, _ = self._result(path, manifest, scenario, cases[scenario]["requests"], cases[scenario])
        if result_path is None or not _inside(result_path, self.evidence_dir):
            return historical_evidence()
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MonitorError(f"Cannot read case result: {exc}") from exc
        if not isinstance(result, dict):
            raise MonitorError("Case result is not an object")
        return {"manifest": manifest, "result": result}
