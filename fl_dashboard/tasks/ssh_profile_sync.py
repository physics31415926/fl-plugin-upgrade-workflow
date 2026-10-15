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
            imported = {profile.get("ssh_alias"): profile for profile in profiles
                        if isinstance(profile, dict) and profile.get("source") == SOURCE and not profile.get("ssh_connection")}
            occupied = {profile.get("ssh_alias") for profile in profiles if isinstance(profile, dict)}
            removed = set(imported) - set(aliases)
            previous = self.state["fingerprints"]
            updated = {alias for alias in set(imported) & set(aliases)
                       if previous.get(alias) != aliases[alias]["fingerprint"]}
            added = set(aliases) - occupied
            added_profiles = []
            try:
                for alias in sorted(added, key=str.casefold):
                    added_profiles.append(manager.save_profile({
                        "id": "ssh-" + hashlib.sha256(alias.encode()).hexdigest()[:24],
                        "label": alias, "ssh_alias": alias, "backend": "", "remote_workspace": "",
                        "model_paths": {}, "source": SOURCE,
                        "role": "gateway" if re.search(r"(?i)(?:jump|bastion|gateway)", alias) else "compute"}))
            except Exception:
                result["ssh_config_sync"] = {"status": "error", "checked_at": now, "changed": False,
                                             "error": "机器配置同步失败，未修改 SSH 文件；请稍后重试。"}
                return result
            stale = (set(self.state["stale"]) | updated) & set(aliases)
            current = []
            for profile in profiles + added_profiles:
                if not isinstance(profile, dict):
                    continue
                alias = profile.get("ssh_alias")
                owned = profile.get("source") == SOURCE and not profile.get("ssh_connection")
                if owned and alias in removed:
                    continue
                item = dict(profile)
                if owned and alias in aliases:
                    item["ssh_config_connection"] = aliases[alias]["connection"]
                    item["ssh_config_changed"] = alias in stale
                    if alias in stale:
                        item["last_check"] = None
                        item["last_connection_check"] = None
                current.append(item)
            changed = bool(added or removed or updated)
            self.state = {"fingerprints": {alias: value["fingerprint"] for alias, value in aliases.items()},
                          "stale": sorted(stale), "file_fingerprint": file_fingerprint}
            self._save()
            result["machine_profiles"] = current
            result["ssh_config_sync"] = {"status": "synced", "checked_at": now, "changed": changed,
                                         "added": len(added), "removed": len(removed), "updated": len(updated),
                                         "aliases": len(aliases), "stale_checks": len(stale)}
            return result
