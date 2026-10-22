"""Cached, bounded, read-only device telemetry using existing SSH profiles."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace
from tempfile import TemporaryDirectory

from fl_dashboard.agents.codex_sessions import CodexSessionManager
from fl_dashboard.monitoring.branch_monitor import LABELS
from fl_dashboard.tasks.automatic_tasks import _ssh_argv
from fl_dashboard.resources.telemetry import parse_telemetry, probe_script

MAX_HOSTS = 512
MAX_OUTPUT = 128 * 1024
PROBE_TIMEOUT = 35
STALE_SECONDS = 600
REFRESH_INTERVAL = 300
MIN_REFRESH_SECONDS = 5
_ACTIVE = {"starting", "running", "waiting_input", "waiting_approval", "interrupting"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


class _InventoryLimit(ValueError):
    pass


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def _blank_accel():
    return {"family": "unknown", "label": "无法识别", "card_name": "", "total": 0,
            "free": 0, "busy": 0, "unknown": 0, "residual": 0, "cards": []}


def _unknown_counts(accel):
    result = copy.deepcopy(accel)
    result.update(free=0, busy=0, residual=0, unknown=max(0, int(result.get("total") or 0)))
    for card in result.get("cards", []):
        if isinstance(card, dict):
            card.update(state="unknown", busy=None, residual=False)
    return result


class DeviceFleet:
    """One collector per control server; no network activity in snapshot()."""

    def __init__(self, registry, ssh_syncs, *, runner=None, ssh_executable=None,
                 clock=time.time, monotonic=time.monotonic, max_workers=4,
                 inventory=None, profile_source=None, cache_path=None):
        self.registry = registry
        self.ssh_syncs = ssh_syncs
        self.inventory = inventory
        self.profile_source = profile_source
        self._source_signature = None
        self._source_epoch = 0
        self._runner = runner or self._run_probe
        self._ssh_executable = ssh_executable
        self._clock, self._monotonic = clock, monotonic
        self._lock = threading.RLock()
        self._closed = False
        self._processes = set()
        self._cache = {}
        self._inflight = {}
        self._states = {project.id: {"profiles": [], "refreshing": False, "pending": set(),
                                    "total": 0, "done": 0, "updated_at": None, "error": None,
                                    "inventory_error": False, "generation": 0, "metadata_future": None}
                        for project in registry.projects}
        self._projects = {project.id: project for project in registry.projects}
        self._metadata = ThreadPoolExecutor(max_workers=1, thread_name_prefix="device-inventory")
        self._workers = ThreadPoolExecutor(max_workers=max(1, min(4, max_workers)), thread_name_prefix="device-probe")
        self._disk_cache = None
        self._cache_writer = None
        self._cache_write_future = None
        self._cache_dirty = False
        self._cache_guard = threading.Lock()
        self._cache_stop = threading.Event()
        if cache_path is not None:
            from fl_dashboard.resources.device_cache import DeviceTelemetryCache
            self._disk_cache = DeviceTelemetryCache(cache_path)
            self._cache_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="device-cache")
            self._restore_cache()

    def _restore_cache(self):
        source = self._reconcile_source()
        source_type = "jumpserver" if source["enabled"] else "ssh"
        try:
            records = self._disk_cache.load()
        except Exception:
            return
        seen = set()
        for record in records.values():
            state = self._states.get(record["project_id"])
            identity = record["project_id"], record["id"]
            if state is None or record["source_type"] != source_type or identity in seen:
                continue
            seen.add(identity)
            view = copy.deepcopy(record["view"])
            view["accel"] = _unknown_counts(view.get("accel") or _blank_accel())
            view.update(verdict="unknown", reachable=None, stale=True)
            age = STALE_SECONDS + 1
            updated_at = record.get("updated_at")
            try:
                observed = datetime.fromisoformat(updated_at)
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                age = max(0, self._clock() - observed.timestamp())
            except (ValueError, TypeError):
                pass
            key = record["fingerprint"]
            self._cache[key] = {"view": view, "sample": None, "observed": self._monotonic() - age,
                                "attempted": float("-inf"), "updated_at": updated_at,
                                "error": True, "retained": True}
            state["profiles"].append({"id": record["id"], "alias": record["ssh_alias"],
                                       "label": record["label"], "backend": record["backend"],
                                       "backend_label": record.get("backend_label") or record["backend"],
                                       "task_count": 0, "key": key, "source_epoch": source["epoch"]})
            state["inventory_error"] = True
            state["updated_at"] = updated_at or state["updated_at"]
        for state in self._states.values():
            if len(state["profiles"]) > MAX_HOSTS:
                state.update(profiles=[], error="历史设备快照超过监控上限，请重新刷新设备列表。")

    def _cache_records(self):
        records = {}
        source_type = "jumpserver" if self._source_signature and self._source_signature[0] else "ssh"
        for project_id, state in self._states.items():
            for profile in state["profiles"]:
                entry = self._cache.get(profile["key"])
                if entry is None:
                    continue
                identity = hashlib.sha256((project_id + ":" + profile["key"]).encode("utf-8")).hexdigest()
                records[identity] = {"fingerprint": profile["key"], "project_id": project_id,
                                     "source_type": source_type, "id": profile["id"],
                                     "ssh_alias": profile["alias"], "label": profile["label"],
                                     "backend": profile["backend"], "backend_label": profile["backend_label"],
                                     "updated_at": entry.get("updated_at"), "view": copy.deepcopy(entry["view"])}
        return records

    def _queue_cache(self):
        if self._cache_writer is None or self._closed:
            return
        self._cache_dirty = True
        if self._cache_write_future is None or self._cache_write_future.done():
            self._cache_write_future = self._cache_writer.submit(self._write_cache)

    def _write_cache(self):
        # Coalesce a completed batch so a large fleet does not rewrite the snapshot
        # once per device. Only display metadata and readings reach the cache helper.
        while not self._cache_stop.wait(.15):
            with self._cache_guard:
                with self._lock:
                    if self._closed:
                        return
                    records = self._cache_records()
                    self._cache_dirty = False
                try:
                    self._disk_cache.save(records)
                except Exception:
                    pass  # Persistence is optional; live telemetry stays available.
            with self._lock:
                if not self._cache_dirty:
                    self._cache_write_future = None
                    return

    def _source_state(self):
        # catalog is a pure cached view; never discover, read SSH config, or probe here.
        if self.inventory is None:
            return {"enabled": False, "generation": 0, "valid": True}
        try:
            catalog = self.inventory.catalog()
            enabled = bool(catalog.get("enabled"))
            generation = catalog.get("generation", 0)
            if not isinstance(generation, int):
                raise ValueError("Invalid inventory generation")
            valid = not enabled or bool(catalog.get("authenticated") and not catalog.get("error"))
            return {"enabled": enabled, "generation": generation, "valid": valid}
        except Exception:
            # An unreadable configured source must never fall back to local aliases.
            return {"enabled": True, "generation": -1, "valid": False}

    def _reconcile_source(self):
        source = self._source_state()
        signature = source["enabled"], source["generation"], source["valid"]
        previous = self._source_signature
        if signature != previous:
            self._source_signature = signature
            if previous is not None:
                self._source_epoch += 1
                switched = previous[0] != signature[0]
                for state in self._states.values():
                    state["generation"] += 1
                    state.update(refreshing=False, pending=set(), total=0, done=0,
                                 inventory_error=True, error=None)
                    if switched:
                        state.update(profiles=[], updated_at=None)
                    pending = state.get("metadata_future")
                    if pending is not None:
                        pending.cancel()
                # Running read-only probes finish naturally. Canceled queued jobs do
                # not hold up a newly selected inventory behind an old large fleet.
                for future in list(self._inflight.values()):
                    future.cancel()
            if not source["valid"]:
                for state in self._states.values():
                    state.update(inventory_error=True,
                                 error="JumpServer 授权或资产列表已失效，请在本机重新登录或刷新。")
        return {**source, "epoch": self._source_epoch}

    def snapshot(self, project_id):
        with self._lock:
            if project_id not in self._states:
                raise ValueError("Unknown device-monitor project")
            self._reconcile_source()
            state = self._states[project_id]
            now = self._monotonic()
            hosts = []
            for profile in state["profiles"]:
                entry = self._cache.get(profile["key"])
                view = copy.deepcopy(entry["view"]) if entry else {
                    "reachable": None, "verdict": "unknown", "error": None, "accel": _blank_accel(), "node": {}}
                age = max(0, now - entry["observed"]) if entry else None
                stale = bool(entry and (age > STALE_SECONDS or state["inventory_error"] or entry.get("retained")))
                view["accel"] = view.get("accel") or _blank_accel()
                if stale or entry and entry.get("error"):
                    view["last_known_accel"] = copy.deepcopy(view["accel"])
                    view["accel"] = _unknown_counts(view["accel"])
                    view["verdict"] = "down" if view.get("reachable") is False else "unknown"
                view.update(id=profile["id"], label=profile["label"], ssh_alias=profile["alias"],
                            backend=view.get("backend") or profile["backend"],
                            backend_label=view.get("backend_label") if view.get("backend") else profile["backend_label"],
                            updated_at=entry.get("updated_at") if entry else None,
                            age_seconds=round(age, 1) if age is not None else None,
                            stale=stale, task_count=profile["task_count"],
                            refreshing=profile["key"] in state["pending"])
                # Only telemetry and intentional display identifiers leave this module.
                view.pop("host", None)
                hosts.append(view)
            summary = {"hosts": len(hosts), "online": 0, "free_hosts": 0,
                       "total_cards": 0, "free_cards": 0, "busy_cards": 0,
                       "unknown_cards": 0, "residual_cards": 0}
            for host in hosts:
                if host.get("reachable") is True and not host["stale"]:
                    summary["online"] += 1
                if host.get("verdict") == "free" and not host["stale"]:
                    summary["free_hosts"] += 1
                for source, target in (("total", "total_cards"), ("free", "free_cards"),
                                       ("busy", "busy_cards"), ("unknown", "unknown_cards"), ("residual", "residual_cards")):
                    summary[target] += max(0, int(host["accel"].get(source) or 0))
            return {"project_id": project_id, "refreshing": state["refreshing"],
                    "progress": {"done": state["done"], "total": state["total"]},
                    "updated_at": state["updated_at"], "refresh_interval": REFRESH_INTERVAL,
                    "error": state["error"], "summary": summary, "hosts": hosts}

    def refresh(self, project_id, profile_id=None):
        if profile_id is not None and (not isinstance(profile_id, str) or not _ID.fullmatch(profile_id)):
            raise ValueError("Invalid device profile ID")
        with self._lock:
            if project_id not in self._states:
                raise ValueError("Unknown device-monitor project")
            if self._closed:
                raise ValueError("Device monitor has stopped")
            source = self._reconcile_source()
            state = self._states[project_id]
            if not state["refreshing"]:
                state.update(refreshing=True, pending=set(), total=0, done=0, error=None)
                state["generation"] += 1
                state["metadata_future"] = self._metadata.submit(
                    self._schedule, project_id, profile_id, state["generation"], source)
        return self.snapshot(project_id)

    def _inventory(self, project_id, source):
        project = self._projects[project_id]
        manager = project.sessions
        if manager is None:
            raise ValueError("设备列表不可用，请检查本机任务服务。")
        if source["enabled"]:
            if self.profile_source is None:
                raise ValueError("JumpServer profile source unavailable")
            profiles = self.profile_source.profiles(project_id, manager, purpose="monitor")
            snapshot = manager.snapshot(compact=True)
            sync, sync_status, sync_error = None, None, False
        else:
            snapshot = manager.snapshot(compact=True)
            sync = self.ssh_syncs.get(project_id)
            if sync is not None:
                snapshot = sync.synchronize(manager, snapshot)
            sync_status = snapshot.get("ssh_config_sync", {}).get("status")
            sync_error = sync_status not in (None, "synced")
            profiles = snapshot.get("machine_profiles")
        if not isinstance(profiles, list):
            raise ValueError("设备列表格式无效，请检查本机任务服务。")
        managers = getattr(manager, "managers", [manager])
        roots = [Path(item.root).resolve() for item in managers if getattr(item, "root", None)]
        fingerprint = getattr(sync, "state", {}).get("file_fingerprint", "")
        import_path = getattr(sync, "path", None)
        tasks = snapshot.get("sessions", [])
        config = getattr(project.monitor, "config", {}) if project.monitor is not None else {}
        labels = {**LABELS, **config.get("backend_labels", {})}
        result, seen = [], set()
        for profile in profiles:
            if not isinstance(profile, dict) or profile.get("role", "compute") != "compute":
                continue
            # Owner archives retain JumpServer profiles for task history. They
            # are never active local-SSH devices after that source is disabled.
            if not source["enabled"] and profile.get("source") == "jumpserver":
                continue
            identity, alias = profile.get("id"), profile.get("ssh_alias")
            if (not isinstance(identity, str) or not _ID.fullmatch(identity) or identity in seen
                    or not isinstance(alias, str) or not _ID.fullmatch(alias)):
                continue
            seen.add(identity)
            if len(result) >= MAX_HOSTS:
                raise _InventoryLimit("计算设备超过监控上限（512 台），请缩小设备范围。")
            private = copy.deepcopy(profile)
            source_root = roots[0] if roots else Path(".").resolve()
            config_hash = ""
            configuration_error = profile.get("configuration_error") or (
                "SSH 配置不可用，请同步或修复别名配置后重试。"
                if sync_error and not profile.get("ssh_connection") else None)
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
                            raw_config = stream.read(MAX_OUTPUT + 1)
                        if len(raw_config) > MAX_OUTPUT:
                            raise ValueError("Managed configuration is too large")
                        source_root = owner
                        config_hash = hashlib.sha256(raw_config).hexdigest()
                except (OSError, ValueError, TypeError):
                    configuration_error = "设备的已保存连接配置无效，请重新保存连接。"
                    config_hash = "invalid-managed-configuration"
            effective = {"alias": alias, "connection": profile.get("ssh_connection"),
                         "managed_config": profile.get("ssh_config_path"), "managed_hash": config_hash,
                         "import_path": str(import_path) if import_path else None,
                         "includes_fingerprint": fingerprint, "sync_status": sync_status,
                         "inventory_source": source, "configuration_error": configuration_error}
            key = hashlib.sha256(json.dumps(effective, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            result.append({"id": identity, "alias": alias, "label": str(profile.get("label") or alias)[:200],
                           "backend": profile.get("backend") or "", "backend_label": labels.get(profile.get("backend"), profile.get("backend") or "无法识别"),
                           "task_count": sum(1 for task in tasks if isinstance(task, dict) and task.get("ssh_alias") == alias and task.get("status") in _ACTIVE),
                           "key": key, "profile": private, "source_root": source_root,
                           "configuration_error": configuration_error, "source_epoch": source["epoch"],
                           "monitor_connection": source["enabled"],
                           "import_path": Path(import_path).resolve() if import_path else None})
        return sorted(result, key=lambda row: (row["backend"] != "cuda", row["backend_label"].split("/")[0].strip().casefold(), row["label"].casefold()))

    def _schedule(self, project_id, profile_id, generation, source):
        try:
            with self._lock:
                current = self._reconcile_source()
                if self._closed or generation != self._states[project_id]["generation"] or current["epoch"] != source["epoch"]:
                    return
            profiles = self._inventory(project_id, source)
            selected = [profile for profile in profiles if profile_id is None or profile["id"] == profile_id]
            if profile_id is not None and not selected:
                raise ValueError("所选设备已不存在，请同步 SSH 配置后重试。")
            with self._lock:
                current = self._reconcile_source()
                state = self._states[project_id]
                if self._closed or state["generation"] != generation or current["epoch"] != source["epoch"]:
                    return
                state.update(profiles=profiles, inventory_error=not source["valid"],
                             error=None if source["valid"] else "JumpServer 授权或资产列表已失效，请在本机重新登录或刷新。")
                keys = {row["key"] for row in selected}
                state.update(pending=set(keys), total=len(keys), done=0)
                now = self._monotonic()
                for profile in selected:
                    key = profile["key"]
                    if profile["configuration_error"]:
                        self._cache[key] = self._failure(profile["configuration_error"], now, reachable=None)
                        state["pending"].discard(key)
                        continue
                    cached = self._cache.get(key)
                    if cached and now - cached["attempted"] < MIN_REFRESH_SECONDS:
                        state["pending"].discard(key)
                        continue
                    if key in self._inflight:
                        continue
                    future = self._workers.submit(self._collect, profile)
                    self._inflight[key] = future
                    future.add_done_callback(lambda completed, target=key, epoch=source["epoch"]:
                                             self._completed(target, completed, epoch))
                state["done"] = state["total"] - len(state["pending"])
                if not state["pending"]:
                    state.update(refreshing=False, updated_at=_iso(self._clock()))
                self._queue_cache()
        except Exception as exc:
            with self._lock:
                state = self._states[project_id]
                if state["generation"] == generation:
                    message = (str(exc) if isinstance(exc, _InventoryLimit) else
                               "无法更新 JumpServer 设备列表，请检查资产授权和本机任务服务。" if source["enabled"] else
                               "无法更新设备列表，请检查 SSH 配置和本机任务服务。")
                    state.update(refreshing=False, pending=set(), error=message, inventory_error=True)

    def _collect(self, profile):
        attempted = self._monotonic()
        try:
            with self._lock:
                current = self._reconcile_source()
                if self._closed or current["epoch"] != profile["source_epoch"] or not current["valid"]:
                    raise ValueError("closed or outdated inventory")
                previous = self._cache.get(profile["key"], {}).get("sample")
            executable = self._ssh_executable or shutil.which("ssh")
            if not executable:
                return self._failure("未找到 OpenSSH 客户端。", attempted)
            with ExitStack() as stack:
                connection_profile = profile["profile"]
                if profile["monitor_connection"]:
                    # Never share a mutable task config with telemetry. Each
                    # probe gets its own validated config and removes it after use.
                    config_root = (profile["source_root"] / "device-probes").resolve()
                    if not config_root.is_relative_to(profile["source_root"]):
                        raise ValueError("Device configuration is outside its owner")
                    config_root.mkdir(parents=True, exist_ok=True)
                    temporary = Path(stack.enter_context(TemporaryDirectory(prefix="probe-", dir=config_root)))
                    config_path = temporary / "ssh.conf"
                    writer = object.__new__(CodexSessionManager)
                    writer._write_ssh_config(config_path, profile["alias"], connection_profile["ssh_connection"])
                    connection_profile = {**connection_profile, "ssh_config_path": str(config_path)}
                argv = _ssh_argv(SimpleNamespace(root=profile["source_root"]), connection_profile, executable)
                if not connection_profile.get("ssh_connection") and profile["import_path"] is not None:
                    if not profile["import_path"].is_file():
                        return self._failure("SSH 配置已不存在，请重新同步。", attempted)
                    argv[1:1] = ["-F", str(profile["import_path"])]
                argv[1:1] = ["-T", "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "PermitLocalCommand=no"]
                result = self._runner(argv + ["bash -s"], input=probe_script().encode("utf-8"),
                                      timeout=PROBE_TIMEOUT, output_limit=MAX_OUTPUT)
            stdout = result.stdout if isinstance(result.stdout, bytes) else str(result.stdout or "").encode("utf-8")
            stderr = result.stderr if isinstance(result.stderr, bytes) else str(result.stderr or "").encode("utf-8")
            if len(stdout) + len(stderr) > MAX_OUTPUT:
                return self._failure("设备统计输出超过上限，未采用结果。", attempted)
            if result.returncode:
                lowered = stderr.lower()
                message = ("SSH 主机密钥尚未受信任，请在本机终端确认。" if b"host key verification failed" in lowered else
                           "SSH 认证失败，请检查已保存的连接。" if b"permission denied" in lowered else
                           "设备统计连接失败，请检查 SSH 连接。")
                return self._failure(message, attempted)
            try:
                view, sample = parse_telemetry(profile["alias"], stdout.decode("utf-8", "replace"), previous)
            except Exception:
                return self._failure("无法解析设备统计，当前可用性未知。", attempted, reachable=True)
            return {"view": view, "sample": sample, "observed": self._monotonic(),
                    "attempted": attempted, "updated_at": _iso(self._clock()),
                    "error": bool(view.get("error")) or view.get("verdict") in ("unknown", "down")}
        except subprocess.TimeoutExpired:
            return self._failure("设备统计超时，请稍后重试。", attempted)
        except Exception:
            return self._failure("设备统计暂时不可用，请检查本机连接配置。", attempted)

    def _failure(self, message, attempted, reachable=False):
        return {"view": {"reachable": reachable, "verdict": "down" if reachable is False else "unknown", "error": message,
                         "accel": _blank_accel(), "node": {}}, "sample": None, "observed": self._monotonic(),
                "attempted": attempted, "updated_at": _iso(self._clock()), "error": True}

    def _completed(self, key, future, source_epoch):
        with self._lock:
            if self._inflight.get(key) is future:
                self._inflight.pop(key, None)
            self._reconcile_source()
            if self._closed or source_epoch != self._source_epoch or future.cancelled():
                return
            try:
                entry = future.result()
            except Exception:
                entry = self._failure("设备统计已中断，请稍后重试。", self._monotonic())
            previous = self._cache.get(key)
            # A complete probe can still have unknown availability (for example,
            # a vendor omits per-card process ownership). Keep its fresh metrics
            # and timestamp; only failed/incomplete collection retains history.
            if (entry["error"] and not entry["view"].get("collection_complete", False)
                    and previous and previous["view"].get("accel", {}).get("total")):
                entry["view"]["accel"] = copy.deepcopy(previous["view"]["accel"])
                entry["view"]["node"] = copy.deepcopy(previous["view"].get("node", {}))
                entry["observed"] = previous["observed"]
                entry["updated_at"] = previous["updated_at"]
                entry["retained"] = True
            self._cache[key] = entry
            for state in self._states.values():
                if key in state["pending"]:
                    state["pending"].discard(key)
                    state["done"] = state["total"] - len(state["pending"])
                    if not state["pending"]:
                        state.update(refreshing=False, updated_at=_iso(self._clock()))
            if len(self._cache) > MAX_HOSTS * 4:
                retained = {row["key"] for state in self._states.values() for row in state["profiles"]}
                for old in sorted(self._cache, key=lambda item: self._cache[item]["attempted"]):
                    if old not in retained and old not in self._inflight:
                        self._cache.pop(old, None)
                    if len(self._cache) <= MAX_HOSTS * 4:
                        break
            self._queue_cache()

    @staticmethod
    def _stop_process(process):
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            except OSError:
                pass

    def _run_probe(self, argv, *, input, timeout, output_limit):
        """Bound memory while reading stdout/stderr, and stop only our SSH process."""
        with self._lock:
            if self._closed:
                raise ValueError("closed")
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        with self._lock:
            self._processes.add(process)
            closed = self._closed
        if closed:
            self._stop_process(process)
        output, errors = bytearray(), bytearray()
        byte_lock, overflow = threading.Lock(), threading.Event()
        def read(stream, target):
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    with byte_lock:
                        remaining = max(0, output_limit - len(output) - len(errors))
                        target.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            overflow.set()
                            self._stop_process(process)
                            break
            except (OSError, ValueError):
                pass
            finally:
                stream.close()
        readers = [threading.Thread(target=read, args=(process.stdout, output), daemon=True),
                   threading.Thread(target=read, args=(process.stderr, errors), daemon=True)]
        for reader in readers:
            reader.start()
        def write():
            try:
                process.stdin.write(input)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        try:
            process.wait(timeout=timeout)
            for reader in readers:
                reader.join(timeout=2)
            if overflow.is_set():
                raise ValueError("Probe output limit exceeded")
            return subprocess.CompletedProcess(argv, process.returncode, bytes(output), bytes(errors))
        except subprocess.TimeoutExpired:
            self._stop_process(process)
            raise
        finally:
            with self._lock:
                self._processes.discard(process)
            writer.join(timeout=1)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._reconcile_source()
            records = self._cache_records() if self._disk_cache is not None else None
            self._closed = True
            self._cache_stop.set()
            processes = list(self._processes)
            for state in self._states.values():
                state.update(refreshing=False, pending=set())
        for process in processes:
            self._stop_process(process)
        self._metadata.shutdown(wait=False, cancel_futures=True)
        self._workers.shutdown(wait=False, cancel_futures=True)
        if self._cache_writer is not None:
            self._cache_writer.shutdown(wait=False, cancel_futures=True)
        if self._disk_cache is not None:
            with self._cache_guard:
                try:
                    self._disk_cache.save(records)
                except Exception:
                    pass
