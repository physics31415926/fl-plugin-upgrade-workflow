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


FLAGSCALE_DEFAULTS = {
    "llm_provider": "anthropic",
    "base_url": "https://api.anthropic.com",
    "model": "claude-sonnet-4-20250514",
    "max_iterations": 200,
    "max_continuations": 20,
    "max_context_tokens": 0,
    "max_output_tokens": 8192,
    "thinking_budget": 0,
    "time_budget_sec": 0,
}


def validate_flagscale_settings(value):
    settings = {**FLAGSCALE_DEFAULTS, **value}
    if settings["llm_provider"] not in {"anthropic", "openai"}:
        raise SessionError("FlagScale LLM provider must be anthropic or openai")
    settings["base_url"] = validate_base_url(settings["base_url"])
    if not isinstance(settings["model"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", settings["model"]):
        raise SessionError("Invalid FlagScale model")
    ranges = {"max_iterations": (1, 5000), "max_continuations": (0, 1000),
              "max_context_tokens": (0, 2_000_000), "max_output_tokens": (1, 131072),
              "thinking_budget": (0, 131071), "time_budget_sec": (0, 604800)}
    for name, (minimum, maximum) in ranges.items():
        number = settings[name]
        if type(number) is not int or not minimum <= number <= maximum:
            raise SessionError(f"FlagScale {name} must be an integer from {minimum} to {maximum}")
    thinking = settings["thinking_budget"]
    if thinking and (settings["llm_provider"] != "anthropic" or thinking < 1024 or thinking >= settings["max_output_tokens"]):
        raise SessionError("FlagScale thinking_budget requires Anthropic and 1024 <= budget < max_output_tokens")
    return {key: settings[key] for key in FLAGSCALE_DEFAULTS}


class AgentSettingsStore:
    """Only snapshot/update are public API responses; runtime is private CLI input."""

    def __init__(self, directory, *, encrypt=None, decrypt=None, provider="claude"):
        if provider not in {"claude", "flagscale"}:
            raise SessionError("Unknown local agent settings provider")
        self.provider = provider
        self.defaults = DEFAULTS if provider == "claude" else FLAGSCALE_DEFAULTS
        self.validate = validate_public_settings if provider == "claude" else validate_flagscale_settings
        self.label = "Claude" if provider == "claude" else "FlagScale"
        self.path = Path(directory) / (provider + "-settings.json")
        self._encrypt = encrypt or _dpapi
        self._decrypt = decrypt or (lambda value: _dpapi(value, decrypt=True))
        self._lock = threading.RLock()

    def _load(self):
        if not self.path.exists():
            return {**self.defaults, "api_key": ""}
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if record.get("schema_version") != 1 or record.get("protection") != "windows-dpapi-current-user":
                raise ValueError()
            raw = self._decrypt(base64.b64decode(record["encrypted"], validate=True))
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            public = self.validate(value)
            key = value.get("api_key", "")
            if not isinstance(key, str):
                raise ValueError()
            return {**public, "api_key": key}
        except SessionError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise SessionError(f"Cannot read protected local {self.label} settings") from None

    def _public(self, value):
        return {"provider": self.provider, **{key: value[key] for key in self.defaults},
                "configured": bool(value.get("api_key")),
                "key_storage": "Windows DPAPI (current user)",
                "api_protocol": value.get("llm_provider", "anthropic")}

    def snapshot(self):
        with self._lock:
            return self._public(self._load())

    def update(self, payload):
        if not isinstance(payload, dict) or set(payload) - (set(self.defaults) | {"api_key", "delete_api_key", "provider"}):
            raise SessionError(f"Unknown {self.label} setting")
        if payload.get("provider", self.provider) != self.provider:
            raise SessionError(f"Settings support the {self.label} provider only")
        if not isinstance(payload.get("delete_api_key", False), bool):
            raise SessionError("delete_api_key must be a boolean")
        with self._lock:
            current = self._load()
            public = self.validate({**current, **{key: payload[key] for key in self.defaults if key in payload}})
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
            public_json = json.dumps(public, ensure_ascii=False)
            # Rotation/deletion must not expose the previous credential through
            # another field in this same public settings update.
            secrets = (current["api_key"], supplied, key)
            if any(isinstance(secret, str) and secret and secret in public_json for secret in secrets):
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
                raise SessionError(f"Configure a local {self.label} API key before starting a {self.label} task")
            return value


class FlagScaleSettingsStore(AgentSettingsStore):
    """Independent FlagScale credentials; existing Claude files are unchanged."""
    def __init__(self, directory, *, encrypt=None, decrypt=None):
        super().__init__(directory, encrypt=encrypt, decrypt=decrypt, provider="flagscale")
