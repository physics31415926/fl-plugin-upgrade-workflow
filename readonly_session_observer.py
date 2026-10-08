"""Safe persisted dashboard task states for a read-only share.

This observer never imports an executor, connects to its API, reads credentials,
or returns controller identities. Cached files are re-read only after a change.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import threading


class ReadonlySessionObserver:
    MAX_FILE_BYTES = 8 * 1024 * 1024
    MAX_FILES = 256
    MAX_ENTRIES_PER_STORE = 1024
    MAX_READ_BYTES = 32 * 1024 * 1024
    STATUSES = {"prepared", "starting", "running", "waiting_approval", "waiting_input", "interrupting",
                "completed", "interrupted", "failed", "disconnected"}
    ACTIVE = {"starting", "running", "waiting_approval", "waiting_input", "interrupting"}
    PUBLIC_FIELDS = {"id", "backend", "branch", "title", "source", "source_label", "provider", "status", "stale",
                     "observed_at", "updated_at", "stage", "controllable", "historical"}

    def __init__(self, stores, *, stale_seconds=1800, clock=None):
        if not isinstance(stores, list) or not 1 <= len(stores) <= 4:
            raise ValueError("Read-only session stores must be a bounded array")
        self.stores = []
        for store in stores:
            if not isinstance(store, dict) or store.get("provider") not in ("codex", "claude"):
                raise ValueError("Read-only session provider must be codex or claude")
            directory = store.get("directory")
            if not isinstance(directory, (str, Path)) or not str(directory):
                raise ValueError("Read-only session store requires a directory")
            self.stores.append((store["provider"], Path(directory).expanduser()))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stale_seconds, self._cache, self._lock = stale_seconds, {}, threading.RLock()

    @staticmethod
    def _time(value):
        if not isinstance(value, str) or len(value) > 80:
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result.astimezone(timezone.utc) if result.tzinfo is not None else None
        except ValueError:
            return None

    def _safe(self, value, provider, directory, controller_id):
        if (not isinstance(value, dict) or value.get("id") != controller_id
                or value.get("provider", provider) != provider or value.get("status") not in self.STATUSES):
            return None
        backend, branch = value.get("backend"), value.get("branch")
        if (not isinstance(backend, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", backend)
                or not isinstance(branch, str) or not 1 <= len(branch) <= 200
                or branch.startswith("-") or any(ord(char) <= 32 for char in branch)):
            return None
        updated = self._time(value.get("updated_at"))
        if updated is None:
            return None
        fingerprint = hashlib.sha256((provider + "\0" + str(directory.absolute()) + "\0" + controller_id).encode("utf8")).hexdigest()[:20]
        label = "NVIDIA" if backend == "cuda" else "iluvatar" if backend in ("corex", "iluvatar") else backend.upper()
        return {"id": "task-" + fingerprint, "backend": backend, "branch": branch,
                "title": label + " · " + ("Claude" if provider == "claude" else "Codex") + " 适配",
                "source": "dashboard", "source_label": "看板持久化状态", "provider": provider,
                "status": value["status"], "updated_at": updated.isoformat(), "stale": False,
                "stage": None, "controllable": False, "historical": False}

    def snapshot(self):
        now, rows, seen, remaining = self._clock().astimezone(timezone.utc), [], set(), self.MAX_READ_BYTES
        with self._lock:
            for provider, directory in self.stores:
                if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents):
                    continue
                try:
                    children = sorted(islice(directory.iterdir(), self.MAX_ENTRIES_PER_STORE), key=lambda child: child.name)
                except OSError:
                    continue
                for child in children:
                    if len(seen) >= self.MAX_FILES:
                        break
                    if not re.fullmatch(r"[0-9a-f]{32}", child.name) or child.is_symlink():
                        continue
                    path, cache_key = child / "session.json", (provider, str(child))
                    try:
                        if path.is_symlink():
                            continue
                        stat = path.stat()
                        if not 0 < stat.st_size <= self.MAX_FILE_BYTES:
                            continue
                        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                        seen.add(cache_key)
                        cached = self._cache.get(cache_key)
                        if cached is not None and cached[0] == signature:
                            safe = cached[1]
                        else:
                            if stat.st_size > remaining:
                                continue
                            with path.open("rb") as stream:
                                opened = os.fstat(stream.fileno())
                                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != signature:
                                    continue
                                content = stream.read(self.MAX_FILE_BYTES + 1)
                            remaining -= len(content)
                            if len(content) > self.MAX_FILE_BYTES:
                                continue
                            safe = self._safe(json.loads(content), provider, directory, child.name)
                            self._cache[cache_key] = (signature, safe)
                        if safe is not None:
                            row = {**safe, "observed_at": now.isoformat()}
                            changed = self._time(row["updated_at"])
                            row["stale"] = row["status"] in self.ACTIVE and (now - changed).total_seconds() > self._stale_seconds
                            rows.append(row)
                    except (OSError, ValueError, TypeError):
                        continue
            self._cache = {key: value for key, value in self._cache.items() if key in seen}
        return sorted(rows, key=lambda row: row["updated_at"], reverse=True)
