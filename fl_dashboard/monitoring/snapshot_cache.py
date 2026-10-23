"""Nonblocking, persisted last-good monitor snapshots, isolated by configuration."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time


MAX_BRANCHES = 12
MAX_CACHE_BYTES = 16 * 1024 * 1024
COLLECTION_ERROR = "状态采集未完成，请稍后重试；已有结果将继续保留。"


def _clone(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


@dataclass
class _Entry:
    snapshot: dict | None = None
    good: bool = False
    error: str | None = None
    last_attempt: float | None = None
    last_success: float | None = None
    last_manual: float | None = None
    flight: threading.Event | None = None


class MonitorSnapshotCache:
    """GET serves saved observations; only a daemon collector touches the monitor.

    ``refresh`` retains the explicit synchronous refresh API and joins any current
    collection for the same branch. All monitor calls are serialized because
    custom monitor adapters need not be thread safe. No task/session data is read.
    """

    def __init__(self, monitor, refresh_interval=60, namespace="local", transform=None):
        if (isinstance(refresh_interval, bool) or not isinstance(refresh_interval, (int, float))
                or not math.isfinite(refresh_interval) or refresh_interval < 0):
            raise ValueError("refresh_interval must be a finite nonnegative number")
        if not isinstance(namespace, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", namespace):
            raise ValueError("Invalid snapshot namespace")
        self.monitor = monitor
        self.refresh_interval = refresh_interval
        self.transform = transform or (lambda value: value)
        config = getattr(monitor, "config", {})
        config = config if isinstance(config, dict) else {}
        self.default_branch = getattr(monitor, "branch", None) or config.get("branch", "main")
        self._validate_branch(self.default_branch)
        self._lock = threading.RLock()
        self._collect_lock = threading.Lock()
        self._disk_lock = threading.Lock()
        self._entries = {}
        self._known_branches = {self.default_branch}
        self._threads = set()
        self._closed = False
        identity = {"type": type(monitor).__module__ + "." + type(monitor).__qualname__,
                    "config": config, "branch": self.default_branch,
                    "repo": str(getattr(monitor, "repo", "")),
                    "remote": getattr(monitor, "remote", ""),
                    "github_repo": getattr(monitor, "github_repo", "")}
        self.fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str,
                                                     ensure_ascii=False).encode("utf-8")).hexdigest()
        self.path = self._cache_path(config, namespace)
        self._load()

    def _cache_path(self, config, namespace):
        root = config.get("resource_state_dir") or config.get("sessions_dir")
        if not root:
            evidence = getattr(self.monitor, "evidence_dir", None)
            root = Path(evidence).parent if evidence else None
        if not isinstance(root, (str, Path)) or not str(root):
            return None
        root = Path(root).expanduser()
        # Registry resolves paths; standalone relative runtime paths use the
        # monitor config's directory, just as its evidence directory does.
        if not root.is_absolute():
            base = getattr(self.monitor, "base", Path.cwd())
            root = Path(base) / root
        return root.resolve() / "monitor-snapshots" / namespace / (self.fingerprint + ".json")

    @staticmethod
    def _validate_branch(branch):
        if (not isinstance(branch, str) or len(branch) > 256
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
                or ".." in branch or "//" in branch or branch.endswith("/")):
            raise ValueError("Invalid branch name")
        return branch

    def _validate_snapshot(self, value, branch):
        if (not isinstance(value, dict) or value.get("branch") != branch
                or not isinstance(value.get("backends"), list)
                or not all(isinstance(row, dict) for row in value["backends"])):
            raise ValueError("Invalid monitor snapshot")
        for key in ("branches", "models", "modes", "cases", "errors"):
            if key in value and not isinstance(value[key], list):
                raise ValueError("Invalid monitor snapshot list")
        for key in ("head", "observed_at"):
            if key in value and value[key] is not None and not isinstance(value[key], str):
                raise ValueError("Invalid monitor observation")
        for item in value.get("branches", []):
            self._validate_branch(item)
        # Reject invalid numeric values even when JSON was syntactically valid.
        return _clone(value)

    @property
    def known_branches(self):
        with self._lock:
            return set(self._known_branches)

    def _remember_branches(self, snapshot):
        for branch in snapshot.get("branches", []):
            try:
                self._known_branches.add(self._validate_branch(branch))
            except ValueError:
                pass

    def _entry(self, branch):
        branch = self._validate_branch(self.default_branch if branch is None else branch)
        if branch not in self._entries:
            if len(self._entries) >= MAX_BRANCHES:
                raise ValueError("Snapshot branch limit reached")
            self._entries[branch] = _Entry()
        return branch, self._entries[branch]

    def _placeholder(self, branch):
        config = getattr(self.monitor, "config", {})
        return {"branch": branch, "branches": sorted(self._known_branches),
                "head": None, "observed_at": None, "backends": [], "errors": [],
                "engine": getattr(self.monitor, "engine", None) or
                          (config.get("engine", "vllm") if isinstance(config, dict) else "vllm")}

    def _view(self, branch, entry, *, cached=True):
        value = _clone(entry.snapshot) if entry.snapshot is not None else self._placeholder(branch)
        old = entry.last_success is None or (self.refresh_interval > 0 and
              time.monotonic() - entry.last_success >= self.refresh_interval)
        value["collection"] = {"has_snapshot": entry.snapshot is not None,
                               "cached": cached and entry.snapshot is not None,
                               "refreshing": entry.flight is not None,
                               "stale": not entry.good or bool(entry.error) or old,
                               "error": entry.error}
        return value

    def get(self, branch=None):
        with self._lock:
            branch, entry = self._entry(branch)
            now = time.monotonic()
            due = entry.last_attempt is None or (self.refresh_interval > 0 and
                  now - entry.last_attempt >= self.refresh_interval)
            if entry.error and entry.last_attempt is not None:
                due = due and now - entry.last_attempt >= 30
            if due and entry.flight is None and not self._closed:
                self._begin(branch, entry, manual=False)
            return self._view(branch, entry)

    def refresh(self, branch=None):
        with self._lock:
            branch, entry = self._entry(branch)
            if self._closed:
                return self._view(branch, entry)
            if entry.flight is not None:
                flight = entry.flight
            elif entry.last_manual is not None and time.monotonic() - entry.last_manual < 30:
                return self._view(branch, entry)
            else:
                flight = self._begin(branch, entry, manual=True)
        flight.wait()
        with self._lock:
            return self._view(branch, entry, cached=False)

    def _begin(self, branch, entry, *, manual):
        flight = entry.flight = threading.Event()
        entry.last_attempt = time.monotonic()
        if manual:
            entry.last_manual = entry.last_attempt
        worker = threading.Thread(target=self._collect, args=(branch, entry, flight, manual),
                                  name="monitor-snapshot", daemon=True)
        self._threads.add(worker)
        worker.start()
        return flight

    def _collect(self, branch, entry, flight, manual):
        good = False
        try:
            with self._collect_lock:
                if self._closed:
                    return
                method = self.monitor.refresh_branch if manual else self.monitor.status
                raw = method(branch=branch)
                raw = self._validate_snapshot(raw, branch)
                good = not bool(raw.get("errors"))
                value = self._validate_snapshot(self.transform(raw), branch)
                value.pop("collection", None)
                with self._lock:
                    if self._closed:
                        return
                    if good or not entry.good:
                        entry.snapshot = value
                        entry.good = good
                        self._remember_branches(value)
                    entry.error = None if good else COLLECTION_ERROR
                    if good:
                        entry.last_success = time.monotonic()
                if good:
                    self._persist()
        except Exception:
            with self._lock:
                entry.error = COLLECTION_ERROR
        finally:
            with self._lock:
                entry.flight = None
                self._threads.discard(threading.current_thread())
                flight.set()

    def _load(self):
        if self.path is None:
            return
        try:
            if self.path.stat().st_size > MAX_CACHE_BYTES:
                return
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("version") != 1
                    or value.get("fingerprint") != self.fingerprint):
                return
            snapshots = value.get("snapshots")
            if not isinstance(snapshots, dict) or len(snapshots) > MAX_BRANCHES:
                return
            entries = {}
            for branch, snapshot in snapshots.items():
                self._validate_branch(branch)
                snapshot = self._validate_snapshot(snapshot, branch)
                if snapshot.get("errors") or "collection" in snapshot:
                    return
                entries[branch] = _Entry(snapshot=snapshot, good=True)
            self._entries = entries
            for entry in entries.values():
                self._remember_branches(entry.snapshot)
        except (OSError, ValueError, TypeError, RecursionError):
            # A missing, truncated or incompatible cache only causes a cold load.
            pass

    def _persist(self):
        if self.path is None:
            return
        temp_path = None
        try:
            with self._disk_lock:
                with self._lock:
                    if self._closed:
                        return
                    snapshots = {branch: entry.snapshot for branch, entry in self._entries.items()
                                 if entry.good and entry.snapshot is not None}
                    data = json.dumps({"version": 1, "fingerprint": self.fingerprint,
                                       "snapshots": snapshots}, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if len(data) > MAX_CACHE_BYTES:
                    return
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_path = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=self.path.parent)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                with self._lock:
                    if not self._closed:
                        os.replace(temp_path, self.path)
                        temp_path = None
        except (OSError, ValueError, TypeError):
            # Read-only/full runtime storage must not hide an in-memory snapshot.
            pass
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def close(self):
        with self._lock:
            self._closed = True
            threads = list(self._threads)
        # HTTP shutdown must not wait for slow network collection. Workers check
        # closed before publishing or writing; the daemon exits with the process.
        deadline = time.monotonic() + 1
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
