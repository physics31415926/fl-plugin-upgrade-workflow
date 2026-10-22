"""Explicit same-user credentials for the controller's loopback owner RPC."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import secrets
import stat
import uuid

from fl_dashboard.agents.agent_settings import _dpapi

WINDOWS = os.name == "nt"
MAX_TOKEN_FILE = 16 * 1024
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")


class OwnerRPCError(ValueError):
    """Public messages never include credential bytes or raw filesystem errors."""


def _path(value):
    try:
        path = Path(value)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError()
        return path
    except (TypeError, ValueError, OSError):
        raise OwnerRPCError("Owner RPC credential requires an absolute, non-symlink file path") from None


def _encode(token):
    if WINDOWS:
        encrypted = _dpapi(token.encode("ascii"))
        return {"schema_version": 1, "protection": "windows-dpapi-current-user",
                "encrypted": base64.b64encode(encrypted).decode("ascii")}
    return {"schema_version": 1, "protection": "posix-owner-permissions", "token": token}


def _decode(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError()
    if WINDOWS:
        if set(value) != {"schema_version", "protection", "encrypted"} or value["protection"] != "windows-dpapi-current-user":
            raise ValueError()
        token = _dpapi(base64.b64decode(value["encrypted"], validate=True), decrypt=True).decode("ascii")
    else:
        if set(value) != {"schema_version", "protection", "token"} or value["protection"] != "posix-owner-permissions":
            raise ValueError()
        token = value["token"]
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError()
    return token


def _load(path):
    try:
        if path.is_symlink():
            raise ValueError()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TOKEN_FILE:
                raise ValueError()
            if not WINDOWS:
                parent = path.parent.stat()
                if (info.st_uid != os.getuid() or parent.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or stat.S_IMODE(parent.st_mode) != 0o700):
                    raise ValueError()
            raw = stream.read(MAX_TOKEN_FILE + 1)
        if len(raw) > MAX_TOKEN_FILE:
            raise ValueError()
        return _decode(json.loads(raw))
    except Exception:
        raise OwnerRPCError("Owner RPC credential cannot be loaded safely; repair its private file instead of replacing it") from None


def owner_rpc_token(path):
    """Load or atomically create an opt-in 256-bit token; never overwrite a file.

    Hard-link publication is atomic and fails when another initializer won. Its
    temporary source is fully written before publication, so concurrent readers
    never observe a partially written credential. An unsupported filesystem fails
    closed instead of weakening initialization or encryption.
    """
    path = _path(path)
    if path.exists():
        return _load(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not WINDOWS:
            if path.parent.stat().st_uid != os.getuid():
                raise ValueError()
            path.parent.chmod(0o700)
        token = secrets.token_hex(32)
        encoded = json.dumps(_encode(token), separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_TOKEN_FILE:
            raise ValueError()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(temporary, flags, 0o600), "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # The winner is complete and validated through exactly the same loader.
        return _load(path)
    except OwnerRPCError:
        raise
    except Exception:
        raise OwnerRPCError("Owner RPC credential could not be initialized safely; check its private directory and encryption support") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
