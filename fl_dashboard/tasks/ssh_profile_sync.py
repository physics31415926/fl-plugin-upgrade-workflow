"""Refresh imported machine aliases without touching SSH keys or running SSH."""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
from pathlib import Path
import re
import shlex
import threading
from datetime import datetime, timezone

from fl_dashboard.tasks.device_recognition import ALIAS_BACKEND_SOURCE, alias_backend

SOURCE = "本机 SSH config"
ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
FIELDS = {"hostname": "host", "user": "user", "port": "port", "identityfile": "identity_file", "proxyjump": "jump_alias"}


def _arguments(text):
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return list(lexer)


def read_config(path):
    """Read explicit aliases and local connection previews; never read key bytes."""
    directives, files, visited = [], [], set()
    def read(file, stack):
        file = file.expanduser().resolve()
        if file in stack:
            raise ValueError("SSH Include contains a cycle")
        if len(files) >= 64:
            raise ValueError("Too many SSH Include files")
        raw = file.read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError("SSH configuration is too large")
        files.append((str(file), hashlib.sha256(raw).hexdigest()))
        visited.add(file)
        for line in raw.decode("utf-8-sig").splitlines():
            match = re.match(r"^\s*([^#\s=]+)(?:\s*=\s*|\s+)(.*)$", line)
            if not match:
                continue
            key, values = match[1].lower(), _arguments(match[2])
            if not values:
                continue
            if key == "include":
                for pattern in values:
                    target = Path(pattern).expanduser()
                    if not target.is_absolute():
                        target = path.parent / target
                    for included in sorted(glob.glob(str(target))):
                        read(Path(included), stack | {file})
            else:
                directives.append((key, values))
    read(Path(path), set())
    aliases = sorted({alias for key, values in directives if key == "host"
                      for alias in values if ALIAS.fullmatch(alias)}, key=str.casefold)
    if len(aliases) > 200:
        raise ValueError("Too many SSH aliases")
    resolved = {}
    for alias in aliases:
        active, options = True, {}
        for key, values in directives:
            if key == "host":
                negatives = [value[1:] for value in values if value.startswith("!")]
                active = any(fnmatch.fnmatchcase(alias.lower(), value.lower()) for value in values if not value.startswith("!"))
                active = active and not any(fnmatch.fnmatchcase(alias.lower(), value.lower()) for value in negatives)
            elif key == "match":
                # Match exec is intentionally not evaluated by the importer.
                active = False
            elif active and key not in options:
                options[key] = " ".join(values)
        preview = {name: options[key] for key, name in FIELDS.items() if key in options}
        preview.setdefault("host", alias)
        try:
            preview["port"] = int(preview.get("port", 22))
        except ValueError:
            preview["port"] = None
        preview["uses_proxy_command"] = bool(options.get("proxycommand"))
        resolved[alias] = {"connection": preview, "fingerprint": hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()}
    return resolved, hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


class SSHProfileSync:
    def __init__(self, config_path=None, state_path=None):
        self.path = Path(config_path) if config_path else Path.home() / ".ssh" / "config"
        self.state_path = Path(state_path) if state_path else None
        self.lock = threading.RLock()
        self.state = {"fingerprints": {}, "stale": []}
        if self.state_path and self.state_path.is_file():
            try:
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(value.get("fingerprints"), dict) and isinstance(value.get("stale"), list):
                    self.state = value
            except (OSError, ValueError, AttributeError):
                pass

    def _save(self):
        if self.state_path:
            from fl_dashboard.agents.codex_sessions import atomic_json
            atomic_json(self.state_path, self.state)

    def checked(self, alias):
        with self.lock:
            if alias in self.state["stale"]:
                self.state["stale"].remove(alias)
                self._save()

    def synchronize(self, manager, snapshot):
        result = dict(snapshot)
        profiles = snapshot.get("machine_profiles")
        if not isinstance(profiles, list) or not callable(getattr(manager, "save_profile", None)):
            return result
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            try:
                aliases, file_fingerprint = read_config(self.path)
            except FileNotFoundError:
                result["ssh_config_sync"] = {"status": "missing", "checked_at": now, "changed": False,
                                             "error": "本机 SSH 配置文件不存在，保留已保存机器。"}
                return result
            except (OSError, ValueError, UnicodeError):
                result["ssh_config_sync"] = {"status": "error", "checked_at": now, "changed": False,
                                             "error": "SSH 配置读取失败，保留已保存机器；请检查文件格式与 Include。"}
                return result
            def imported_profile(profile):
                return (isinstance(profile, dict) and profile.get("source") == SOURCE
                        and not profile.get("ssh_connection"))

            def profile_priority(profile):
                configured = bool(profile.get("backend") and profile.get("backend_source") != ALIAS_BACKEND_SOURCE)
                checked = bool((profile.get("last_check") or {}).get("ok"))
                # A saved non-compute role must never become selectable merely
                # because an older duplicate was imported as a compute device.
                restricted = profile.get("role") in {"gateway", "development"}
                return (restricted, configured, checked, bool(profile.get("hardware")),
                        bool(profile.get("remote_workspace")), bool(profile.get("model_paths")),
                        bool(profile.get("label") and profile.get("label") != profile.get("ssh_alias")))

            # Keep every owner record for task references, but display one imported
            # profile per alias. Prefer established metadata over empty imports.
            imported = {}
            manual_aliases = {profile.get("ssh_alias") for profile in profiles
                              if isinstance(profile, dict) and not imported_profile(profile)}
            for profile in profiles:
                if imported_profile(profile):
                    alias = profile.get("ssh_alias")
                    if alias not in imported or profile_priority(profile) > profile_priority(imported[alias]):
                        imported[alias] = profile
            occupied = {profile.get("ssh_alias") for profile in profiles if isinstance(profile, dict)}
            previous = self.state["fingerprints"]
            added = set(aliases) - set(previous)
            removed = set(previous) - set(aliases)
            updated = {alias for alias in set(previous) & set(aliases)
                       if previous[alias] != aliases[alias]["fingerprint"]}
            # Existing checks without a prior fingerprint cannot establish that
            # the currently loaded SSH endpoint is still the checked endpoint.
            unverified = set(imported) & set(aliases) - set(previous)
            stale = (set(self.state["stale"]) | updated | unverified) & set(aliases)
            classified = 0
            try:
                for alias in sorted(set(aliases) - occupied, key=str.casefold):
                    role = "gateway" if re.search(r"(?i)(?:jump|bastion|gateway)", alias) else "compute"
                    backend = alias_backend(alias) if role == "compute" else None
                    imported[alias] = manager.save_profile({
                        "id": "ssh-" + hashlib.sha256(alias.encode()).hexdigest()[:24],
                        "label": alias, "ssh_alias": alias, "backend": backend or "", "remote_workspace": "",
                        "model_paths": {}, "source": SOURCE, "role": role,
                        "backend_source": ALIAS_BACKEND_SOURCE if backend else ""})
                for alias, profile in list(imported.items()):
                    if alias not in aliases or alias in manual_aliases or profile.get("role", "compute") != "compute":
                        continue
                    if profile.get("backend") and profile.get("backend_source") != ALIAS_BACKEND_SOURCE:
                        continue
                    backend = alias_backend(alias) or ""
                    source = ALIAS_BACKEND_SOURCE if backend else ""
                    if profile.get("backend", "") != backend or profile.get("backend_source", "") != source:
                        imported[alias] = manager.save_profile({**profile, "backend": backend, "backend_source": source})
                        classified += 1
            except Exception:
                result["ssh_config_sync"] = {"status": "error", "checked_at": now, "changed": False,
                                             "error": "机器配置同步失败，未修改 SSH 文件；请稍后重试。"}
                return result
            current = [profile for profile in profiles if isinstance(profile, dict) and not imported_profile(profile)]
            for alias, profile in imported.items():
                if alias not in aliases or alias in manual_aliases:
                    continue
                item = dict(profile)
                item["ssh_config_connection"] = aliases[alias]["connection"]
                item["ssh_config_changed"] = alias in stale
                if item.get("role", "compute") == "compute":
                    item["backend_recognition"] = ("inferred" if item.get("backend_source") == ALIAS_BACKEND_SOURCE
                                                   else "configured" if item.get("backend") else "unknown")
                if alias in stale:
                    item["last_check"] = None
                    item["last_connection_check"] = None
                current.append(item)
            changed = bool(added or removed or updated or classified)
            self.state = {"fingerprints": {alias: value["fingerprint"] for alias, value in aliases.items()},
                          "stale": sorted(stale), "file_fingerprint": file_fingerprint}
            self._save()
            result["machine_profiles"] = current
            result["ssh_config_sync"] = {"status": "synced", "checked_at": now, "changed": changed,
                                         "added": len(added), "removed": len(removed), "updated": len(updated),
                                         "classified": classified, "aliases": len(aliases), "stale_checks": len(stale),
                                         "recognized": sum(bool(profile.get("backend")) for profile in current
                                                           if profile.get("role", "compute") == "compute"),
                                         "unrecognized": sum(not profile.get("backend") for profile in current
                                                             if profile.get("role", "compute") == "compute")}
            return result
