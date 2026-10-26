"""Identity-bound hardware hints for task selection, never connection readiness."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from fl_dashboard.agents.codex_sessions import CodexSessionManager
from fl_dashboard.resources.jumpserver_profiles import JumpServerProfiles, _server, profile_id
from fl_dashboard.resources.telemetry import DEVICE_LABELS, FAMILY_BACKENDS, _text


def profile_context(profile, source, *, roots, import_path, fingerprint, sync_status, max_output):
    """The same complete connection fingerprint for collection and hint reuse."""
    private = copy.deepcopy(profile)
    source_root = roots[0] if roots else Path(".").resolve()
    config_hash = ""
    configuration_error = profile.get("configuration_error") or (
        "SSH 配置不可用，请同步或修复别名配置后重试。"
        if sync_status not in (None, "synced") and not profile.get("ssh_connection") else None)
    if source["enabled"]:
        if not source["valid"]:
            configuration_error = "JumpServer 授权或资产列表已失效，请在本机重新登录或刷新。"
        elif not profile.get("ssh_connection"):
            configuration_error = configuration_error or "JumpServer 设备尚未生成有效的独立连接配置。"
    if profile.get("ssh_connection") and not configuration_error:
        try:
            if source["enabled"]:
                if not roots:
                    raise ValueError("Device monitor has no owner directory")
                validator = object.__new__(CodexSessionManager)
                private["ssh_connection"] = validator._ssh_connection(profile["ssh_connection"])
                private["ssh_config_path"] = None
            else:
                path = Path(profile.get("ssh_config_path") or "").resolve()
                owner = next((root for root in roots if path.is_relative_to(root)), None)
                if owner is None or not path.is_file():
                    raise ValueError("Invalid managed configuration")
                with path.open("rb") as stream:
                    raw_config = stream.read(max_output + 1)
                if len(raw_config) > max_output:
                    raise ValueError("Managed configuration is too large")
                source_root = owner
                config_hash = hashlib.sha256(raw_config).hexdigest()
        except (OSError, ValueError, TypeError):
            configuration_error = "设备的已保存连接配置无效，请重新保存连接。"
            config_hash = "invalid-managed-configuration"
    effective = {"alias": profile.get("ssh_alias"), "connection": profile.get("ssh_connection"),
                 "managed_config": profile.get("ssh_config_path"), "managed_hash": config_hash,
                 "import_path": str(import_path) if import_path else None,
                 "includes_fingerprint": fingerprint, "sync_status": sync_status,
                 "inventory_source": ({"enabled": True, "scope": source["scope"]}
                                      if source["enabled"] else {"enabled": False}),
                 "configuration_error": configuration_error}
    key = hashlib.sha256(json.dumps(effective, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"key": key, "profile": private, "source_root": source_root,
            "configuration_error": configuration_error,
            "import_path": Path(import_path).resolve() if import_path else None}


def monitor_profile(profile, catalog):
    """Rebuild only the current asset's monitor connection; no manager writes."""
    if profile.get("source") != "jumpserver" or not isinstance(catalog, dict) or not catalog.get("enabled"):
        return None
    canonical, host = _server(catalog.get("base_url"))
    assets = catalog.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if (not isinstance(asset, dict) or asset.get("ssh_supported") is not True
                or asset.get("is_active", True) is not True):
            continue
        try:
            identity = profile_id(canonical, asset.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if identity != profile.get("id") or identity != profile.get("ssh_alias"):
            continue
        asset_id = str(asset["id"])
        supplied_asset = profile.get("jumpserver_asset_id")
        if supplied_asset is not None and supplied_asset != asset_id:
            return None
        task, task_error = JumpServerProfiles._connection(catalog, asset, asset_id, host, purpose="task")
        connection, error = JumpServerProfiles._connection(catalog, asset, asset_id, host, purpose="monitor")
        if error:
            return None
        validator = object.__new__(CodexSessionManager)
        connection = validator._ssh_connection(connection)
        supplied = profile.get("ssh_connection")
        if supplied:
            if task_error or validator._ssh_connection(supplied) != validator._ssh_connection(task):
                return None
        elif not profile.get("configuration_error"):
            return None
        return {**profile, "ssh_connection": connection, "ssh_config_path": None,
                "configuration_error": None}
    return None


def supported_backends(project):
    config = getattr(project.monitor, "config", {})
    configured = config.get("session_backends") if isinstance(config, dict) else None
    if configured is None:
        configured = getattr(project.sessions, "backends", None)
    if configured is None:
        configured = getattr(project.sessions, "config", {}).get("session_backends", CodexSessionManager.BACKENDS)
    if not isinstance(configured, (list, tuple, set, frozenset)):
        return set()
    return {value for value in configured if isinstance(value, str)} - {"rocm"}


def observation(entry, *, stale, verified):
    view = entry.get("view") or {}
    accel = view.get("accel") or {}
    total = accel.get("total")
    backend = FAMILY_BACKENDS.get(accel.get("family")) if type(total) is int and total > 0 else None
    # Do not promote the profile fallback backend from a generic/empty probe.
    if view.get("backend") not in (None, "", backend):
        backend = None
    hardware = _text(accel.get("card_name"), 200) if backend else ""
    if backend and not hardware:
        names = list(dict.fromkeys(_text(card.get("name"), 100)
                                   for card in accel.get("cards", []) if isinstance(card, dict)))
        hardware = " / ".join(name for name in names if name)[:200]
    return {"backend": backend or "", "backend_label": DEVICE_LABELS.get(backend, "无法识别"),
            "hardware": hardware, "updated_at": entry["updated_at"],
            "stale": bool(stale), "verified": bool(verified and backend)}
