#!/usr/bin/env python3
"""Read-only deployment of the branch dashboard, without the control server.

Non-loopback HTTP requires Basic auth or --trusted-ingress. The latter means
authentication/TLS is enforced by a trusted reverse proxy or private ingress.
Explicit --anonymous-lan permits direct HTTPS on a specific RFC1918 IPv4 address.
Optional access shares forward authenticated requests to the local control server.
Set an authentication password in an environment variable, never a config or URL.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import threading
from urllib.parse import parse_qs, urlsplit

from fl_dashboard.paths import ASSETS, TEMPLATES
from fl_dashboard.monitoring.branch_monitor import BranchMonitor
from fl_dashboard.monitoring.snapshot_cache import MonitorSnapshotCache
from fl_dashboard.projects.project_registry import ProjectRegistry
from fl_dashboard.monitoring.pr_acceptance import public_acceptance_tables, detail_text
from fl_dashboard.services.public_devices import PublicDeviceRefresher, read_public_devices
from fl_dashboard.services.share_gateway import validate_control_url


CAPABILITIES = {"read_only": True, "sessions": False, "refresh": True, "evidence": False}
STATUS_FIELDS = {"status", "scope", "commit", "target_commit", "tree", "tree_matches_head", "run_id", "attempt"}
GATE_FIELDS = {"status", "passed", "total", "failed", "missing", "requests_passed", "requests_total",
               "latest_commit", "evidence_type", "tree_matches_head", "recorded_cases", "raw_results_available"}
RESULT_FIELDS = {"model", "mode", "scenario", "requests", "status", "requests_passed", "evidence_type", "raw_results_available", "label", "source", "source_blob"}
ACCEPTANCE_FIELDS = {"status", "source", "title", "summary", "pr_number", "archive_available"}
ACCEPTANCE_SOURCES = {"pr_description", "local_record", "current_gate", "pr_reference"}
PRIVATE_VALUE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|(?:^|\s)[~/][A-Za-z0-9_.-]+(?:/|$)|\b(?:\d{1,3}\.){3}\d{1,3}\b)")


def _scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and len(value) <= 400 and not PRIVATE_VALUE.search(value) and not any(ord(c) < 32 for c in value):
        return value
    return None


def _fields(value, names):
    return {key: _scalar(value[key]) for key in names if key in value} if isinstance(value, dict) else {}


def _public_url(value):
    if not isinstance(value, str) or len(value) > 1500:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname == "github.com" and not parsed.username and not parsed.password and parsed.port in (None, 443):
            return value
    except ValueError:
        pass
    return None


def _public_pr_url(value):
    """Keep only canonical GitHub PR links, without free-form URL metadata."""
    value = _public_url(value)
    if value is None or any(ord(c) < 32 for c in value):
        return None
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", parsed.path):
        return None
    if _scalar(parsed.path.lstrip("/")) is None:
        return None
    return value


def _status(value):
    result = _fields(value, STATUS_FIELDS)
    result["url"] = _public_url(value.get("url")) if isinstance(value, dict) else None
    scope = {"branch": "当前分支 CI", "pr": "适配 PR CI", "merged-pr": "已合入 PR 适配 CI", "accepted-tree": "已验收代码树 CI"}.get(result.get("scope"), "状态记录")
    result["detail"] = scope + (f" · 第 {result['attempt']} 次运行" if type(result.get("attempt")) is int else "")
    return result


def sanitize_snapshot(snapshot):
    """Build an explicit public projection; unknown/nested provenance is omitted.

Free-form diagnostic strings are not forwarded because they may contain local
paths, machine aliases, private IPs, source task IDs, or commands.
"""
    result = _fields(snapshot, {"branch", "branch_ref", "head", "observed_at", "vllm_version", "engine", "upstream_version"})
    result["head_url"] = _public_url(snapshot.get("head_url"))
    for key in ("branches", "models", "modes"):
        result[key] = [clean for value in snapshot.get(key, []) if (clean := _scalar(value)) is not None][:100]
    result["cases"] = []
    for case in snapshot.get("cases", [])[:100]:
        if not isinstance(case, dict):
            continue
        public_case = _fields(case, {"scenario", "requests", "file", "test", "model", "label", "source", "source_blob"})
        for key in ("models", "modes"):
            values = case.get(key)
            if isinstance(values, list):
                public_case[key] = [clean for value in values[:100]
                                    if isinstance(value, str) and (clean := _scalar(value)) is not None]
        result["cases"].append(public_case)
    result["branch_ci"] = _status(snapshot.get("branch_ci", {}))
    result["backends"] = []
    for backend in snapshot.get("backends", [])[:50]:
        if not isinstance(backend, dict):
            continue
        public = _fields(backend, {"id", "label", "ci_enabled"})
        for key in ("integration", "ci", "current_ci"):
            public[key] = _status(backend.get(key, {}))
        pr = backend.get("pr")
        public["pr"] = {**_fields(pr, {"number", "title", "draft", "state", "merged", "selected_by"}), "url": _public_url(pr.get("url"))} if isinstance(pr, dict) else None
        public["gate"] = _fields(backend.get("gate", {}), GATE_FIELDS)
        public["gate"]["detail"] = "逐项结果；验收概述见验收结果"
        acceptance = backend.get("acceptance")
        public["acceptance"] = None
        if isinstance(acceptance, dict):
            item = _fields(acceptance, ACCEPTANCE_FIELDS)
            if item.get("source") not in ACCEPTANCE_SOURCES:
                item["source"] = None
            item["pr_url"] = _public_pr_url(acceptance.get("pr_url"))
            item["archive_url"] = None
            item["acceptance_tables"] = []
            for table in public_acceptance_tables(acceptance.get("description_blocks")):
                # Redact every public cell at the output boundary as well.
                item["acceptance_tables"].append({"type": "table", "rows": [
                    [detail_text(cell, 1200) for cell in row]
                    for row in table["rows"]]})
            item["rows"] = []
            rows = acceptance.get("rows")
            for row in rows[:100] if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                item["rows"].append({
                    "label": detail_text(row.get("label"), 160) or "测试结果",
                    "configuration": detail_text(row.get("configuration"), 300),
                    "result": detail_text(row.get("result"), 400),
                    "status": row.get("status") if row.get("status") in ("passed", "partial", "failed", "pending") else "unknown"})
            total = acceptance.get("rows_total")
            item["rows_total"] = total if type(total) is int and 0 <= total <= 1_000_000 else len(item["rows"])
            item["rows_truncated"] = acceptance.get("rows_truncated") is True or item["rows_total"] > len(item["rows"])
            public["acceptance"] = item
        public["acceptance_records"] = []
        for record in backend.get("acceptance_records", []):
            if not isinstance(record, dict):
                continue
            item = _fields(record, {"id", "status", "pr_number", "title", "summary", "scope", "recorded_at", "tested_commit", "tested_tree", "tree_matches_head", "source", "archive_available"})
            item["pr_url"] = _public_url(record.get("pr_url"))
            item["archive_url"] = None
            public["acceptance_records"].append(item)
        public["results"] = []
        for row in backend.get("results", [])[:1000]:
            if not isinstance(row, dict):
                continue
            item = _fields(row, RESULT_FIELDS)
            item["evidence_url"] = None
            item["reason"] = {"passed": "原始结果检查通过", "recorded_pass": "已核对历史验收记录",
                              "missing": "尚无匹配证据", "stale": "证据与当前版本不匹配", "failed": "结果检查未通过"}.get(item.get("status"), "")
            public["results"].append(item)
        result["backends"].append(public)
    result["errors"] = ["部分上游数据暂不可用；管理员可在本机检查详情。"] if snapshot.get("errors") else []
    result["read_only"] = True
    return result


class SnapshotCache:
    """Public projection of the persistent, non-blocking monitor cache."""
    def __init__(self, monitor, refresh_interval=60, *, namespace="public"):
        if refresh_interval != 0 and refresh_interval < 30:
            raise ValueError("refresh_interval must be 0 (disabled) or at least 30 seconds")
        self.monitor = monitor
        self.refresh_interval = refresh_interval
        self.default_branch = monitor.branch
        self._cache = MonitorSnapshotCache(monitor, refresh_interval=refresh_interval,
                                           namespace=namespace, transform=sanitize_snapshot)
        self._lock = threading.Lock()
        self.allowed_branches = self._cache.known_branches | {self.default_branch}
        branch_options = getattr(monitor, "branch_options", None)
        if callable(branch_options):
            try:
                # Only local tracking refs; collecting PRs/CI is left to workers.
                options = branch_options()
                self.allowed_branches.update(branch for branch in options.get("branches", [])
                                             if isinstance(branch, str) and _scalar(branch))
            except Exception:
                # Saved branches and the configured default remain usable offline.
                pass

    def _branch(self, branch):
        if branch is not None and not isinstance(branch, str):
            raise ValueError("branch must be a string")
        branch = branch or self.default_branch
        with self._lock:
            self.allowed_branches.update(self._cache.known_branches)
            if branch not in self.allowed_branches:
                raise ValueError("Branch is not configured for this shared dashboard")
        return branch

    def _public_view(self, value):
        if not value["collection"]["has_snapshot"]:
            # Empty placeholders also cross the public projection boundary.
            collection = value["collection"]
            value = sanitize_snapshot(value)
            value["collection"] = collection
            with self._lock:
                value["branches"] = sorted(self.allowed_branches)[:100]
        return value

    def get(self, branch=None):
        return self._public_view(self._cache.get(self._branch(branch)))

    def refresh(self, branch=None):
        # The shared cache deduplicates concurrent and repeated manual refreshes.
        return self._public_view(self._cache.refresh(self._branch(branch)))

    def close(self):
        self._cache.close()


class ProjectSnapshotCache:
    """Separate public caches; one project's failure cannot hide its peers."""
    def __init__(self, registry, refresh_interval=60):
        if refresh_interval != 0 and refresh_interval < 30:
            raise ValueError("refresh_interval must be 0 (disabled) or at least 30 seconds")
        self.registry = registry
        self.refresh_interval = refresh_interval
        self._caches = {}
        self._cache_locks = {project.id: threading.Lock() for project in registry.projects}
        from concurrent.futures import ThreadPoolExecutor
        def initialize(project):
            try:
                self.get_cache(project.id)
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=min(8, len(registry.projects))) as pool:
            list(pool.map(initialize, registry.projects))

    def get_cache(self, project_id):
        project = self.registry.select("project=" + project_id)
        with self._cache_locks[project.id]:
            if project_id not in self._caches:
                if project.monitor is None:
                    raise RuntimeError("Project monitor is unavailable")
                self._caches[project_id] = SnapshotCache(project.monitor, self.refresh_interval,
                                                        namespace="public-" + project.id)
            return self._caches[project_id]

    def get(self, branch=None, *, project=None):
        project_id = project or self.registry.projects[0].id
        return self.get_cache(project_id).get(branch)

    def refresh(self, branch=None, *, project=None):
        project_id = project or self.registry.projects[0].id
        return self.get_cache(project_id).refresh(branch)

    def statuses(self):
        from concurrent.futures import ThreadPoolExecutor
        def status(project):
            result = _fields(project.metadata(read_only=True), {"id", "label", "engine"})
            result.update(status=None, error=None, adaptation_tasks=project.adaptation_tasks(read_only=True))
            try:
                result["status"] = self.get(project=project.id)
            except Exception:
                result["error"] = "Project data is temporarily unavailable"
            return result
        with ThreadPoolExecutor(max_workers=min(8, len(self.registry.projects))) as pool:
            return {"projects": list(pool.map(status, self.registry.projects))}

    def close(self):
        for cache in self._caches.values():
            cache.close()


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_tls_context(cert_path=None, key_path=None):
    """Load optional direct HTTPS credentials before starting monitor workers."""
    if (cert_path is None) != (key_path is None):
        raise ValueError("--tls-cert and --tls-key must be provided together")
    if cert_path is None:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        # No interactive password prompt during unattended service startup.
        context.load_cert_chain(certfile=cert_path, keyfile=key_path, password="")
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(f"Cannot load TLS certificate/key: {exc}") from exc
    return context


def _validate_anonymous_lan(host, *, password=None, tls_context=None, trusted_ingress=False):
    if password is not None:
        raise ValueError("--anonymous-lan cannot be combined with a password")
    if trusted_ingress:
        raise ValueError("--anonymous-lan cannot be combined with --trusted-ingress")
    try:
        address = ipaddress.IPv4Address(host) if isinstance(host, str) else None
    except ipaddress.AddressValueError:
        address = None
    private_networks = (ipaddress.IPv4Network("10.0.0.0/8"),
                        ipaddress.IPv4Network("172.16.0.0/12"),
                        ipaddress.IPv4Network("192.168.0.0/16"))
    if address is None or not any(address in network for network in private_networks):
        raise ValueError("--anonymous-lan requires a specific RFC1918 IPv4 address")
    if tls_context is None:
        raise ValueError("--anonymous-lan requires HTTPS with --tls-cert and --tls-key")


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(cache, host="127.0.0.1", port=8765, *, username="viewer", password=None,
                trusted_ingress=False, base_path="", index_path=None, tls_context=None, anonymous_lan=False,
                access_shares=None, control_url=None):
    if password is not None and (not isinstance(password, str) or not password or len(password.encode("utf-8")) > 1024 or any(c in password for c in "\r\n\x00")):
        raise ValueError("Password must be nonempty, at most 1024 UTF-8 bytes, and contain no control newline")
    if not isinstance(username, str) or not username or len(username.encode("utf-8")) > 128 or any(c in username for c in ":\r\n\x00"):
        raise ValueError("Invalid Basic auth username")
    if anonymous_lan:
        _validate_anonymous_lan(host, password=password, tls_context=tls_context, trusted_ingress=trusted_ingress)
    elif not _loopback(host) and password is None and not trusted_ingress:
        raise ValueError("Non-loopback binding requires an environment password or --trusted-ingress with external ingress authentication")
    prefix = "/" + base_path.strip("/") if base_path.strip("/") else ""
    if prefix and not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", prefix):
        raise ValueError("base_path must contain simple URL path segments")
    if access_shares is not None and control_url is None:
        raise ValueError("Access sharing requires the control URL")
    device_control_url = validate_control_url(control_url) if control_url is not None else None
    device_refresher = PublicDeviceRefresher(device_control_url) if device_control_url else None
    gateway = None
    if access_shares is not None:
        from fl_dashboard.services.share_gateway import ShareGateway
        gateway = ShareGateway(access_shares, control_url, tls_context=tls_context, base_path=prefix)
    registry = cache.registry if isinstance(cache, ProjectSnapshotCache) else ProjectRegistry.single(monitor=cache.monitor)
    page = Path(index_path or (TEMPLATES / ("overview.html" if registry.multi_project else "index.html"))).read_bytes()
    monitor_page = Path(index_path or (TEMPLATES / "index.html")).read_bytes()
    devices_page = (TEMPLATES / "devices.html").read_bytes()
    def public_projects():
        return {"projects": [_fields(project.metadata(read_only=True),
                                     {"id", "label", "engine", "default_branch", "github_repo", "sessions", "workflow"})
                             for project in registry.projects], "read_only": True}
    def project_snapshot(project, branch=None, *, refresh=False):
        method = cache.refresh if refresh else cache.get
        return method(branch, project=project.id) if isinstance(cache, ProjectSnapshotCache) else method(branch)
    expected_user = username.encode("utf-8")
    expected_password = password.encode("utf-8") if password is not None else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "FLReadOnly/1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *_):
            # Avoid logging URLs, headers, credentials, and private client details.
            pass

        def _authorized(self):
            if expected_password is None:
                return True
            authorization = self.headers.get("Authorization", "")
            valid = False
            if len(authorization) <= 4096 and authorization.startswith("Basic "):
                try:
                    credential = base64.b64decode(authorization[6:], validate=True)
                    supplied_user, separator, supplied_password = credential.partition(b":")
                    valid = bool(separator) and len(credential) <= 1153
                    valid = secrets.compare_digest(supplied_user, expected_user) & secrets.compare_digest(supplied_password, expected_password) & valid
                except (ValueError, binascii.Error):
                    pass
            if not valid:
                self._send(401, {"error": "Authentication required"}, challenge=True)
            return bool(valid)

        def _send(self, code, value, *, content_type="application/json; charset=utf-8", challenge=False, cookie=None):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            if getattr(self, "_expired_share_cookie", False) and cookie is None:
                cookie = gateway._clear_cookie()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Connection", "close")
            if challenge:
                self.send_header("WWW-Authenticate", 'Basic realm="FL dashboard", charset="UTF-8"')
            if code == 405:
                self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            self.close_connection = True

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if gateway is not None and gateway.handle(self):
                return
            if not self._authorized():
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            if prefix and path == prefix:
                self.send_response(308)
                self.send_header("Location", prefix + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if prefix and path.startswith(prefix + "/"):
                path = path[len(prefix):]
            try:
                project = registry.select(parsed.query)
            except ValueError:
                self._send(400, {"error": "Unknown or duplicate project"})
                return
            if path in ("/", "/index.html"):
                self._send(200, page, content_type="text/html; charset=utf-8")
            elif path == "/assets/overview-export.js":
                asset = ASSETS / "overview-export.js"
                if not asset.is_file():
                    self._send(404, {"error": "Export script is not installed"})
                    return
                self._send(200, asset.read_bytes(), content_type="application/javascript; charset=utf-8")
            elif path in ("/monitor", "/monitor.html"):
                self._send(200, monitor_page, content_type="text/html; charset=utf-8")
            elif path in ("/devices", "/devices.html"):
                self._send(200, devices_page, content_type="text/html; charset=utf-8")
            elif path == "/api/devices":
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
                    if set(query) - {"project"} or any(len(values) != 1 for values in query.values()):
                        raise ValueError("Invalid device query")
                except ValueError:
                    self._send(400, {"error": "Invalid device query"})
                    return
                if device_control_url is None:
                    self._send(503, {"error": "设备资源服务尚未配置，请联系管理员。"})
                    return
                try:
                    data = read_public_devices(device_control_url, project.id)
                    data["public_refresh_retry_after"] = device_refresher.retry_after()
                    self._send(200, data)
                except Exception:
                    self._send(503, {"error": "设备资源暂不可用，请稍后重试。"})
            elif path == "/api/projects":
                self._send(200, public_projects())
            elif path == "/api/projects-status":
                if isinstance(cache, ProjectSnapshotCache):
                    self._send(200, cache.statuses())
                else:
                    result = _fields(project.metadata(read_only=True), {"id", "label", "engine"})
                    result.update(status=None, error=None, adaptation_tasks=project.adaptation_tasks(read_only=True))
                    try:
                        result["status"] = cache.get()
                    except Exception:
                        result["error"] = "Project data is temporarily unavailable"
                    self._send(200, {"projects": [result]})
            elif path == "/api/adaptation-tasks":
                self._send(200, {"tasks": project.adaptation_tasks(read_only=True), "error": None})
            elif path == "/api/capabilities":
                self._send(200, {**CAPABILITIES, "refresh": project.monitor is not None, "default_branch": _scalar(project.default_branch),
                                 "project_id": _scalar(project.id), "label": _scalar(project.label), "multi_project": registry.multi_project,
                                 "engine": _scalar(project.engine_name), "workflow": False,
                                 "devices": device_control_url is not None, "device_refresh": device_control_url is not None,
                                 "devices_refresh": device_control_url is not None, "jumpserver_settings": False,
                                 "adaptation_tasks": project.native_tasks is not None or project.readonly_sessions is not None})
            elif path == "/healthz":
                self._send(200, {"ok": True, "read_only": True})
            elif path == "/api/branch-status":
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=5)
                    branch = query.get("branch", [None])[0]
                    self._send(200, project_snapshot(project, branch))
                except ValueError:
                    self._send(400, {"error": "Invalid or unavailable branch"})
                except Exception:
                    self._send(503, {"error": "Branch data is temporarily unavailable"})
            else:
                self._send(404, {"error": "Not available in the read-only dashboard"})

        def _device_refresh_trusted(self):
            if gateway is not None:
                return gateway._trusted(self, post=True)
            hosts, origins = self.headers.get_all("Host", []), self.headers.get_all("Origin", [])
            fetch_sites = self.headers.get_all("Sec-Fetch-Site", [])
            trusted = len(hosts) == len(origins) == 1 and len(fetch_sites) <= 1
            try:
                origin = urlsplit(origins[0]) if trusted else None
                scheme = "https" if tls_context is not None else "http"
                trusted = bool(origin and origin.scheme == scheme and origin.netloc.lower() == hosts[0].lower()
                               and not origin.username and not origin.password and not origin.path
                               and not origin.query and not origin.fragment
                               and (not fetch_sites or fetch_sites[0].lower() in ("same-origin", "none")))
                if trusted and not trusted_ingress:
                    # Direct listeners accept their configured/local address, not
                    # an arbitrary Host supplied through a rebinding DNS name.
                    bound = self.server.server_address[0]
                    names = {host.lower(), str(bound).lower(), str(self.connection.getsockname()[0]).lower()}
                    if _loopback(bound):
                        names.update({"localhost", "127.0.0.1", "::1"})
                    trusted = (origin.hostname or "").lower() in names and (
                        origin.port or (443 if scheme == "https" else 80)) == self.server.server_port
            except (ValueError, IndexError):
                trusted = False
            if not trusted:
                self._send(403, {"error": "Resource refresh must originate from this dashboard"})
            return trusted

        def do_POST(self):
            if gateway is not None and gateway.handle(self):
                return
            if not self._authorized():
                return
            target = urlsplit(self.path)
            path = target.path
            if prefix and path.startswith(prefix + "/"):
                path = path[len(prefix):]
            try:
                project = registry.select(target.query)
            except ValueError:
                self._send(400, {"error": "Unknown or duplicate project"})
                return
            if path == "/api/devices/refresh":
                if device_refresher is None:
                    self._send(503, {"error": "设备资源服务尚未配置，请联系管理员。"})
                    return
                if not self._device_refresh_trusted():
                    return
                try:
                    query = parse_qs(target.query, keep_blank_values=True, max_num_fields=2)
                    if (target.scheme or target.netloc or target.fragment or "\\" in self.path
                            or len(self.path) > 8192 or any(ord(c) < 32 for c in self.path)
                            or set(query) - {"project"} or any(len(values) != 1 for values in query.values())):
                        raise ValueError("Invalid resource refresh query")
                    from fl_dashboard.services.share_gateway import ShareGateway
                    _, body = ShareGateway._body(self, 4096)
                    if body:
                        raise ValueError("Public resource refresh accepts no fields")
                except (ValueError, TypeError, UnicodeError):
                    self._send(400, {"error": "资源刷新只接受当前仓库和空 JSON 对象。"})
                    return
                try:
                    self._send(202, device_refresher.refresh(project.id))
                except Exception:
                    self._send(503, {"error": "设备资源暂不可用，请稍后重试。"})
                return
            if path != "/api/branch-refresh":
                self._send(405, {"error": "This shared dashboard is read-only"})
                return
            origin = self.headers.get("Origin")
            if origin is not None:
                try:
                    parsed = urlsplit(origin)
                    same_origin = (parsed.scheme in ("http", "https") and parsed.netloc.lower() == self.headers.get("Host", "").lower()
                                   and not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment)
                except ValueError:
                    same_origin = False
                if not same_origin:
                    self._send(403, {"error": "Refresh must originate from this dashboard"})
                    return
            if self.headers.get("Sec-Fetch-Site", "").lower() not in ("", "same-origin", "none"):
                self._send(403, {"error": "Cross-site refresh is not allowed"})
                return
            if self.headers.get("Content-Type", "").partition(";")[0].strip().lower() != "application/json":
                self._send(415, {"error": "Refresh requires a JSON body"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 4096:
                    self._send(413, {"error": "Refresh body is too large"})
                    return
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Refresh requires a JSON object")
                self._send(200, project_snapshot(project, body.get("branch"), refresh=True))
            except (ValueError, TypeError):
                self._send(400, {"error": "Invalid or unavailable branch"})
            except Exception:
                self._send(503, {"error": "Refresh is temporarily unavailable; the previous snapshot is retained"})

        def _deny_control(self):
            if gateway is not None and gateway.handle(self):
                return
            if self._authorized():
                self._send(405, {"error": "This shared dashboard is read-only"})

        do_PUT = _deny_control
        do_PATCH = _deny_control
        do_DELETE = _deny_control
        do_OPTIONS = _deny_control

    server = BoundedHTTPServer((host, port), Handler)
    if tls_context is not None:
        try:
            # Handshake in the bounded request worker, under its connection timeout.
            server.socket = tls_context.wrap_socket(server.socket, server_side=True,
                                                    do_handshake_on_connect=False)
        except Exception:
            server.server_close()
            raise
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitor-config", type=Path)
    parser.add_argument("--projects-config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--username", default="viewer")
    parser.add_argument("--password-env", default="FLFLOW_SHARE_PASSWORD")
    parser.add_argument("--trusted-ingress", action="store_true", help="Trust a private/reverse-proxy ingress that provides access control and TLS")
    parser.add_argument("--anonymous-lan", action="store_true", help="Serve without login only on a specific RFC1918 IPv4 address using direct HTTPS")
    parser.add_argument("--base-path", default="")
    parser.add_argument("--access-shares-file", type=Path, help="Local share-grant store (hashes only), shared with the control server")
    parser.add_argument("--public-url", help="HTTPS origin used in access-sharing links")
    parser.add_argument("--control-url", help="Literal loopback HTTP origin; enables public cached device views, and is also used by access sharing")
    parser.add_argument("--tls-cert", type=Path, help="PEM certificate chain for direct HTTPS")
    parser.add_argument("--tls-key", type=Path, help="Unencrypted PEM private key for direct HTTPS")
    parser.add_argument("--refresh-interval", type=int, default=60, help="Git/API refresh seconds; 0 disables background fetching")
    args = parser.parse_args(argv)
    if not args.monitor_config and not args.projects_config:
        parser.error("--monitor-config or --projects-config is required")
    password = None if args.anonymous_lan else os.environ.get(args.password_env)
    access_shares = None
    try:
        supplied = (args.access_shares_file is not None, args.public_url is not None, args.control_url is not None)
        if any(supplied[:2]) and not all(supplied):
            raise ValueError("Access sharing needs --access-shares-file, --public-url and --control-url together")
        if args.control_url is not None:
            validate_control_url(args.control_url)
        tls_context = load_tls_context(args.tls_cert, args.tls_key)
        if all(supplied):
            from fl_dashboard.services.access_shares import AccessShareStore
            from fl_dashboard.services.share_gateway import ShareGateway
            access_shares = AccessShareStore(args.access_shares_file, args.public_url)
            ShareGateway(access_shares, args.control_url, tls_context=tls_context, base_path=args.base_path)
        if args.anonymous_lan:
            _validate_anonymous_lan(args.host, password=password, tls_context=tls_context,
                                    trusted_ingress=args.trusted_ingress)
    except ValueError as exc:
        parser.error(str(exc))
    cache = None
    server = None
    try:
        if args.projects_config:
            registry = ProjectRegistry.from_config(args.projects_config, read_only=True)
            cache = ProjectSnapshotCache(registry, args.refresh_interval)
        else:
            cache = SnapshotCache(BranchMonitor(args.monitor_config), args.refresh_interval)
        server = make_server(cache, args.host, args.port, username=args.username, password=password,
                             trusted_ingress=args.trusted_ingress, base_path=args.base_path, tls_context=tls_context,
                             anonymous_lan=args.anonymous_lan, access_shares=access_shares, control_url=args.control_url)
        print(f"Shared dashboard listening via {'HTTPS' if tls_context else 'HTTP'} on port {server.server_port}", flush=True)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        if cache:
            cache.close()


if __name__ == "__main__":
    main()
