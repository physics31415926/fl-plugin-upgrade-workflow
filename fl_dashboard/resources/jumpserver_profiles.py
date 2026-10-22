"""Project machine profiles derived only from the current JumpServer catalog.

No network, credentials, or SSH calls live here. API discovery owns authorization;
the existing session manager owns validated, isolated SSH configuration files.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit, urlunsplit
import uuid

from fl_dashboard.tasks.device_recognition import alias_backend

SOURCE = "jumpserver"
BACKEND_SOURCE = "JumpServer 名称推断（待硬件核验）"
_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@+-]{0,99}\Z")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}\Z")
_FIELDS = ("id", "label", "ssh_alias", "ssh_connection", "backend", "backend_source",
           "remote_workspace", "model_paths", "role", "hardware", "source", "notes")


def _text(value, limit=200):
    return "".join(c for c in value if c.isprintable()).strip()[:limit] if isinstance(value, str) else ""


def _server(base_url):
    """Canonical server identity excludes credentials and uses URL hostname only."""
    if not isinstance(base_url, str) or len(base_url) > 2000:
        raise ValueError("Invalid server")
    parts = urlsplit(base_url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise ValueError("Invalid server")
    hostname = parts.hostname.lower()
    port = parts.port
    authority = "[" + hostname + "]" if ":" in hostname else hostname
    if port and port != (443 if parts.scheme == "https" else 80):
        authority += ":" + str(port)
    canonical = urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "", ""))
    return canonical, hostname


def profile_id(base_url, asset_id):
    """Stable across refreshes and projects, distinct across JumpServer instances."""
    server, _ = _server(base_url)
    asset = str(uuid.UUID(str(asset_id)))
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return "jms-" + digest(server) + "-" + digest(asset)


class JumpServerProfiles:
    def __init__(self, inventory):
        self.inventory = inventory
        self._guard = threading.Lock()
        self._projects = {}

    def profiles(self, project_id, manager, *, purpose="task"):
        if purpose not in {"task", "monitor"}:
            raise ValueError("Unknown JumpServer connection purpose")
        # Workbench and resource polling may import the same newly discovered
        # assets simultaneously. Serialize snapshot+comparison+saves per project.
        with self._guard:
            lock = self._projects.setdefault(str(project_id), threading.RLock())
        with lock:
            catalog = self.inventory.catalog()
            if not isinstance(catalog, dict) or not catalog.get("enabled"):
                return []
            try:
                canonical, default_host = _server(catalog.get("base_url"))
            except (TypeError, ValueError):
                return []
            error = None
            if not catalog.get("authenticated"):
                error = "JumpServer 授权已失效，请重新登录并刷新资产"
            elif catalog.get("error"):
                error = "JumpServer 资产同步失败，请重新刷新；暂不允许连接缓存资产"
            try:
                snapshot = manager.snapshot(compact=True)
                saved = {item["id"]: item for item in snapshot.get("machine_profiles", [])
                         if isinstance(item, dict) and isinstance(item.get("id"), str)}
            except Exception:
                snapshot, saved = {}, {}
                error = error or "本机任务服务暂不可用，无法准备设备连接配置"
            supported = snapshot.get("backends")
            current, seen = [], set()
            assets = catalog.get("assets")
            if not isinstance(assets, list):
                return []
            for asset in assets:
                if (not isinstance(asset, dict) or asset.get("ssh_supported") is not True
                        or asset.get("is_active", True) is not True):
                    continue
                try:
                    asset_id = str(uuid.UUID(str(asset.get("id"))))
                except (TypeError, ValueError, AttributeError):
                    continue
                identity = profile_id(canonical, asset_id)
                if identity in seen:
                    continue
                seen.add(identity)
                previous = saved.get(identity, {})
                name = _text(asset.get("name")) or "JumpServer 设备"
                inferred = alias_backend(name) or ""
                backend_source = BACKEND_SOURCE if inferred else ""
                backend = inferred
                # A verified or manually configured backend takes priority over
                # a name hint. User workspace/model paths are never rediscovered.
                if previous.get("backend") and previous.get("backend_source") != BACKEND_SOURCE:
                    backend, backend_source = previous["backend"], previous.get("backend_source", "")
                if isinstance(supported, (list, set, tuple)) and backend not in supported:
                    backend, backend_source = "", ""
                row = {"id": identity, "label": name, "ssh_alias": identity, "source": SOURCE,
                       "backend": backend, "backend_source": backend_source,
                       "role": previous.get("role", "compute"), "hardware": previous.get("hardware", ""),
                       "remote_workspace": previous.get("remote_workspace", ""),
                       "model_paths": previous.get("model_paths", {}), "notes": previous.get("notes", "")}
                connection, connection_error = self._connection(catalog, asset, asset_id, default_host, purpose=purpose)
                failure = error or connection_error
                row["ssh_connection"] = connection
                if not failure:
                    try:
                        # Local managers canonicalize key paths and optional
                        # connection defaults. Remote managers validate on save.
                        validator = getattr(manager, "_ssh_connection", None)
                        if callable(validator):
                            row["ssh_connection"] = validator(connection)
                        if purpose == "monitor":
                            # Telemetry owns an isolated, short-lived SSH config;
                            # it must never rewrite the task manager's profile.
                            row["ssh_config_path"] = None
                        elif all(previous.get(field) == row.get(field) for field in _FIELDS):
                            row = dict(previous)
                        else:
                            row = manager.save_profile(row)
                            saved[identity] = row
                    except Exception:
                        failure = "设备连接配置保存失败，请检查本机任务服务、JumpServer 网关及 SSH 私钥路径"
                if failure:
                    # Retain an informational row without a usable config path.
                    # The owner keeps prior records for existing task history.
                    row = {**row, "ssh_connection": None, "ssh_config_path": None,
                           "last_check": None, "last_connection_check": None,
                           "configuration_error": failure}
                else:
                    row = {**row, "configuration_error": None}
                row.update(jumpserver_asset_id=asset_id, jumpserver_generation=catalog.get("generation"),
                           backend_recognition="inferred" if row.get("backend_source") == BACKEND_SOURCE
                           else "configured" if row.get("backend") else "unknown")
                current.append(row)
            return current

    @staticmethod
    def _connection(catalog, asset, asset_id, default_host, *, purpose="task"):
        accounts = asset.get("accounts")
        accounts = list(dict.fromkeys(a for a in accounts if isinstance(a, str) and _USER.fullmatch(a))) if isinstance(accounts, list) else []
        if not accounts:
            return None, "平台未返回可自动连接的 SSH 账号，请检查该资产的账号配置"
        preferred = (catalog.get("task_account") or catalog.get("default_account")) if purpose == "task" else catalog.get("default_account")
        label = "任务执行账号" if purpose == "task" else "设备采集账号"
        if preferred:
            if preferred not in accounts:
                return None, "平台未返回所选" + label + "（" + _text(preferred, 100) + "），请检查资产账号配置"
            account = preferred
        elif len(accounts) == 1:
            account = accounts[0]
        else:
            return None, "资产有多个授权账号，请在 JumpServer 设置中选择已授权的" + label
        username = catalog.get("username")
        host = catalog.get("ssh_host") or default_host
        port = catalog.get("ssh_port", 2222)
        if port is None or port == "":
            port = 2222
        identity = catalog.get("identity_file") or ""
        if (not isinstance(username, str) or not _USER.fullmatch(username)
                or not isinstance(host, str) or not _HOST.fullmatch(host)
                or type(port) is not int or not 1 <= port <= 65535
                or not isinstance(identity, str)):
            return None, "JumpServer SSH 网关或登录用户配置无效，请在本机设置中检查"
        if identity:
            try:
                path = Path(identity).expanduser()
                if not path.is_absolute():
                    return None, "SSH 私钥路径必须为本机绝对路径"
                identity = str(path.resolve())
            except (OSError, ValueError):
                return None, "SSH 私钥路径无效，请在本机设置中检查"
        return {"host": host, "user": username + "#" + account + "#" + asset_id,
                "port": port, "identity_file": identity, "jump_alias": "", "inherit_ssh_config": False}, None
