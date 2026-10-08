"""Local provider settings; secrets are encrypted with current-user Windows DPAPI."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import ipaddress
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit
from codex_sessions import SessionError, atomic_json

DEFAULTS = {
    "base_url": "https://api.anthropic.com",
    "model": "sonnet",
    "permission_mode": "acceptEdits",
    "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep"],
    "max_turns": 80,
}
ALLOWED_MODES = {"acceptEdits", "dontAsk"}
ALLOWED_TOOL_NAMES = {"Read", "Edit", "Write", "Glob", "Grep", "Bash"}


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(value: bytes, *, decrypt=False) -> bytes:
    if os.name != "nt":
        raise SessionError("API key storage requires Windows current-user DPAPI")
    buffer = ctypes.create_string_buffer(value)
    source = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    library = ctypes.windll.crypt32
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; no machine-wide protection flag.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise SessionError("Windows could not decrypt the local API settings" if decrypt
                           else "Windows could not encrypt the local API settings")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(target.data)


def validate_base_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 1000 or any(ord(c) <= 32 for c in value):
        raise SessionError("API base URL must be an HTTPS Anthropic-compatible endpoint")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise SessionError("Invalid API base URL") from None
    try:
        loopback = host == "localhost" or bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        loopback = False
    if (not host or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "\\" in value
            or port is not None and not 1 <= port <= 65535
            or parsed.scheme not in {"http", "https"}
            or parsed.scheme == "http" and not loopback):
        raise SessionError("Use HTTPS for API endpoints; HTTP is allowed only for loopback")
    return value.rstrip("/")


def validate_public_settings(value):
    settings = {**DEFAULTS, **value}
    settings["base_url"] = validate_base_url(settings["base_url"])
    model = settings["model"]
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
        raise SessionError("Invalid Claude model")
    if settings["permission_mode"] not in ALLOWED_MODES:
        raise SessionError("Claude permission mode must be acceptEdits or dontAsk")
    tools = settings["allowed_tools"]
    if (not isinstance(tools, list) or len(tools) > 32
            or any(not isinstance(tool, str) or len(tool) > 300
                   or any(ord(c) < 32 for c in tool)
                   or not re.fullmatch(r"[A-Za-z]+(?:\([^\r\n]*\))?", tool)
                   or tool.split("(", 1)[0] not in ALLOWED_TOOL_NAMES for tool in tools)):
        raise SessionError("allowed_tools must list explicit Claude Read/Edit/Write/Glob/Grep/Bash rules")
    settings["allowed_tools"] = list(dict.fromkeys(tools))
    turns = settings["max_turns"]
    if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= 500:
        raise SessionError("Claude max_turns must be an integer from 1 to 500")
    return {key: settings[key] for key in DEFAULTS}


class AgentSettingsStore:
    """Only snapshot/update are public API responses; runtime is private CLI input."""

    def __init__(self, directory, *, encrypt=None, decrypt=None):
        self.path = Path(directory) / "claude-settings.json"
        self._encrypt = encrypt or _dpapi
        self._decrypt = decrypt or (lambda value: _dpapi(value, decrypt=True))
        self._lock = threading.RLock()

    def _load(self):
        if not self.path.exists():
            return {**DEFAULTS, "api_key": ""}
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if record.get("schema_version") != 1 or record.get("protection") != "windows-dpapi-current-user":
                raise ValueError()
            raw = self._decrypt(base64.b64decode(record["encrypted"], validate=True))
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            public = validate_public_settings(value)
            key = value.get("api_key", "")
            if not isinstance(key, str):
                raise ValueError()
            return {**public, "api_key": key}
        except SessionError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise SessionError("Cannot read protected local Claude settings") from None

    @staticmethod
    def _public(value):
        return {"provider": "claude", **{key: value[key] for key in DEFAULTS},
                "configured": bool(value.get("api_key")),
                "key_storage": "Windows DPAPI (current user)",
                "api_protocol": "anthropic"}

    def snapshot(self):
        with self._lock:
            return self._public(self._load())

    def update(self, payload):
        if not isinstance(payload, dict) or set(payload) - (set(DEFAULTS) | {"api_key", "delete_api_key", "provider"}):
            raise SessionError("Unknown Claude setting")
        if payload.get("provider", "claude") != "claude":
            raise SessionError("Settings support the Claude provider only")
        if not isinstance(payload.get("delete_api_key", False), bool):
            raise SessionError("delete_api_key must be a boolean")
        with self._lock:
            current = self._load()
            public = validate_public_settings({**current, **{key: payload[key] for key in DEFAULTS if key in payload}})
            key = current["api_key"]
            supplied = payload.get("api_key")
            if supplied is not None:
                if (not isinstance(supplied, str) or len(supplied) > 4096
                        or any(ord(c) <= 32 for c in supplied)):
                    raise SessionError("API key must be a single nonempty token")
                if supplied:
                    if len(supplied) < 12:
                        raise SessionError("API key is too short")
                    key = supplied
            if payload.get("delete_api_key"):
                key = ""
            if key and key in json.dumps(public, ensure_ascii=False):
                raise SessionError("API key must not be included in public provider settings")
            value = {**public, "api_key": key}
            encrypted = self._encrypt(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            atomic_json(self.path, {"schema_version": 1, "protection": "windows-dpapi-current-user",
                                    "encrypted": base64.b64encode(encrypted).decode("ascii")})
            if os.name != "nt":
                self.path.chmod(0o600)
            return self._public(value)

    def runtime(self):
        with self._lock:
            value = self._load()
            if not value["api_key"]:
                raise SessionError("Configure a local Claude API key before starting a Claude task")
            return value
