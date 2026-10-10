"""Locate a current native Codex CLI without executing it or pinning updates."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil


IS_WINDOWS = os.name == "nt"
_VERSION_DIRECTORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


def _executable_file(path: Path) -> str | None:
    """Windows app-server needs a native executable, rather than a shell shim."""
    try:
        if not path.is_file():
            return None
        if IS_WINDOWS:
            if path.suffix.lower() != ".exe":
                return None
        elif not os.access(path, os.X_OK):
            return None
        return str(path.resolve())
    except (OSError, ValueError):
        return None


def _path_cli() -> str | None:
    for command in ("codex.exe", "codex") if IS_WINDOWS else ("codex",):
        try:
            candidate = shutil.which(command)
        except (OSError, ValueError):
            candidate = None
        if candidate:
            resolved = _executable_file(Path(candidate))
            if resolved:
                return resolved
    return None


def _app_bin(path: Path) -> Path | None:
    """Only recognize the desktop app's Codex/bin/<version>/codex.exe layout."""
    if (not IS_WINDOWS or path.name.lower() != "codex.exe"
            or not _VERSION_DIRECTORY.fullmatch(path.parent.name)
            or path.parent.parent.name.lower() != "bin"
            or path.parent.parent.parent.name.lower() != "codex"):
        return None
    return path.parent.parent


def _redirected(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _latest_app_cli(directory: Path) -> str | None:
    """Inspect only one trusted app bin and its immediate version directories."""
    try:
        if _redirected(directory) or not directory.is_dir():
            return None
        root = directory.resolve()
        candidates = []
        for version in directory.iterdir():
            if (not _VERSION_DIRECTORY.fullmatch(version.name) or _redirected(version)
                    or not version.is_dir()):
                continue
            executable = version / "codex.exe"
            if _redirected(executable):
                continue
            resolved = _executable_file(executable)
            if not resolved or not Path(resolved).is_relative_to(root):
                continue
            candidates.append((executable.stat().st_mtime_ns, version.name, resolved))
        return max(candidates)[2] if candidates else None
    except (OSError, ValueError):
        return None


def _default_app_bin() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if not IS_WINDOWS or not local:
        return None
    path = Path(local)
    if not path.is_absolute():
        return None
    return path / "OpenAI" / "Codex" / "bin"


def resolve_codex_executable(configured: str | None = None, *, base_dir=None) -> str | None:
    """Resolve the current CLI; a missing custom executable never changes tools.

    Desktop-app version paths may disappear during an update. For those paths
    and automatic discovery, prefer the current PATH CLI, then inspect only the
    known app bin. No shell, version command, login, or model request is run.
    """
    previous_bin = None
    if configured:
        if not isinstance(configured, str) or "\x00" in configured:
            return None
        try:
            path = Path(configured).expanduser()
            is_command = path.name == configured and not path.is_absolute()
            if is_command:
                found = shutil.which(configured)
                if found:
                    path = Path(found)
            if not path.is_absolute():
                path = Path(base_dir or Path.cwd()) / path
            explicit = _executable_file(path)
            if explicit:
                return explicit
            previous_bin = _app_bin(path)
            if previous_bin is None and configured.lower() not in {"codex", "codex.exe"}:
                return None
        except (OSError, ValueError, TypeError):
            return None
    current = _path_cli()
    if current:
        return current
    for directory in (previous_bin, _default_app_bin()):
        if directory is not None:
            current = _latest_app_cli(directory)
            if current:
                return current
    return None
