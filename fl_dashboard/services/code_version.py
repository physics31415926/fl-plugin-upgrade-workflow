"""Whether the running dashboard still matches the code on disk, and self-restart.

A long-lived ``flflow serve`` keeps running the modules it imported at start.
After a ``git pull`` or a local edit the process silently serves old logic, and
nothing on screen says so. ``CodeVersion`` records the checkout HEAD and the
start time when the server is built, and later reports whether HEAD moved or
any watched source file changed since then.

``respawn`` starts a detached copy of this process with the exact original
interpreter arguments, so a restart keeps every flag the operator chose
(sharing, ports, project config) without anyone having to retype them.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fl_dashboard.paths import APP_ROOT

# Python sources whose change means the running process is out of date.
# Templates and assets are read per request, so editing them needs no restart.
WATCHED = ("fl_dashboard", "scripts")
CHANGED_LIMIT = 20


def git_head(root: Path = APP_ROOT) -> str | None:
    """The checkout's HEAD commit, or None when git cannot say."""
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    head = result.stdout.strip()
    if result.returncode != 0 or len(head) not in (40, 64) or any(c not in "0123456789abcdef" for c in head):
        return None
    return head


def changed_sources(since: float, root: Path = APP_ROOT) -> list[str]:
    """Watched ``.py`` files modified after ``since``, as checkout-relative paths."""
    changed = []
    for name in WATCHED:
        base = root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                if path.stat().st_mtime > since:
                    changed.append(path.relative_to(root).as_posix())
            except OSError:
                continue
    return sorted(changed)


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


class CodeVersion:
    """Start-time code identity and a cached comparison with the disk."""

    def __init__(self, root: Path = APP_ROOT, *, clock=time.time, head=git_head, ttl: float = 30.0):
        self.root = Path(root)
        self._clock = clock
        self._head = head
        self._ttl = ttl
        self.started_at = clock()
        self.started_head = head(self.root)
        self._lock = threading.Lock()
        self._cached: tuple[str | None, list[str]] | None = None
        self._cached_at = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            if self._cached is None or now - self._cached_at >= self._ttl:
                self._cached = (self._head(self.root), changed_sources(self.started_at, self.root))
                self._cached_at = now
            current, changed = self._cached
        head_moved = bool(self.started_head and current and current != self.started_head)
        return {
            "pid": os.getpid(),
            "started_at": _iso(self.started_at),
            "started_head": self.started_head,
            "current_head": current,
            "head_moved": head_moved,
            "changed_files": changed[:CHANGED_LIMIT],
            "changed_count": len(changed),
            "stale": head_moved or bool(changed),
            "can_restart": True,
        }


def _inherited(stream) -> object:
    try:
        stream.flush()
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL


def respawn(argv: list[str] | None = None) -> subprocess.Popen:
    """Start a detached copy of this process with the original arguments.

    Called only after the listening socket is closed, so the copy can bind the
    same port. Output goes wherever this process's output went (a log file for
    a hidden launch), and the environment, including any GitHub token, is
    inherited unchanged.
    """
    arguments = list(argv) if argv is not None else list(sys.orig_argv[1:])
    kwargs: dict = {"cwd": os.getcwd(), "stdin": subprocess.DEVNULL,
                    "stdout": _inherited(sys.__stdout__), "stderr": _inherited(sys.__stderr__)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([sys.executable, *arguments], **kwargs)
