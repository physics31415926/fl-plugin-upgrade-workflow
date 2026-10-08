#!/usr/bin/env python3
"""Read-only deployment of the branch dashboard, without the control server.

Non-loopback HTTP requires Basic auth or --trusted-ingress. The latter means
authentication/TLS is enforced by a trusted reverse proxy or private ingress.
Explicit --anonymous-lan permits direct HTTPS on a specific RFC1918 IPv4 address.
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
import time
from urllib.parse import parse_qs, urlsplit

from branch_monitor import BranchMonitor
from project_registry import ProjectRegistry


CAPABILITIES = {"read_only": True, "sessions": False, "refresh": True, "evidence": False}
STATUS_FIELDS = {"status", "scope", "commit", "target_commit", "tree", "tree_matches_head", "run_id", "attempt"}
GATE_FIELDS = {"status", "passed", "total", "failed", "missing", "requests_passed", "requests_total",
               "latest_commit", "evidence_type", "tree_matches_head", "recorded_cases", "raw_results_available"}
RESULT_FIELDS = {"model", "mode", "scenario", "requests", "status", "requests_passed", "evidence_type", "raw_results_available", "label", "source", "source_blob"}
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


def _status(value):
    result = _fields(value, STATUS_FIELDS)
    result["url"] = _public_url(value.get("url")) if isinstance(value, dict) else None
    scope = {"branch": "当前分支 CI", "pr": "候选 PR CI", "accepted-tree": "已验收代码树 CI"}.get(result.get("scope"), "状态记录")
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
        public["gate"]["detail"] = "共享视图；验收状态与原始证据分开记录"
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
    def __init__(self, monitor, refresh_interval=300):
        if refresh_interval != 0 and refresh_interval < 30:
            raise ValueError("refresh_interval must be 0 (disabled) or at least 30 seconds")
        self.monitor = monitor
        self.refresh_interval = refresh_interval
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._last_refresh = {}
        self._stop = threading.Event()
        self._snapshots = {}
        self.default_branch = monitor.branch
        initial = sanitize_snapshot(monitor.status())
        self.allowed_branches = set(initial.get("branches", [])) | {self.default_branch}
        self._snapshots[self.default_branch] = initial
        self._worker = None
        if refresh_interval:
            self._worker = threading.Thread(target=self._refresh, daemon=True)
            self._worker.start()

    def get(self, branch=None):
        branch = branch or self.default_branch
        with self._lock:
            if branch not in self.allowed_branches:
                raise ValueError("Branch is not configured for this shared dashboard")
            if branch not in self._snapshots:
                if len(self._snapshots) >= 12:
                    raise ValueError("Shared dashboard branch cache is full")
                self._snapshots[branch] = sanitize_snapshot(self.monitor.status(branch=branch))
            return json.loads(json.dumps(self._snapshots[branch]))

    def _refresh(self):
        while not self._stop.wait(self.refresh_interval):
            with self._lock:
                branches = list(self._snapshots)
            for branch in branches:
                if self._stop.is_set():
                    return
                try:
                    self.refresh(branch)
                except Exception:
                    with self._lock:
                        self._snapshots[branch]["errors"] = ["上游刷新暂不可用；当前显示上次记录。"]

    def refresh(self, branch=None):
        if branch is not None and not isinstance(branch, str):
            raise ValueError("branch must be a string")
        branch = branch or self.default_branch
        # One Git fetch at a time; repeated clicks share the latest collection.
        with self._refresh_lock:
            with self._lock:
                if branch not in self.allowed_branches:
                    raise ValueError("Branch is not configured for this shared dashboard")
                if branch not in self._snapshots and len(self._snapshots) >= 12:
                    raise ValueError("Shared dashboard branch cache is full")
                recent = self._last_refresh.get(branch)
                if recent is not None and time.monotonic() - recent < 30:
                    return json.loads(json.dumps(self._snapshots[branch]))
            value = sanitize_snapshot(self.monitor.refresh_branch(branch=branch))
            with self._lock:
                self._snapshots[branch] = value
                self._last_refresh[branch] = time.monotonic()
                self.allowed_branches.update(value.get("branches", []))
            return json.loads(json.dumps(value))

    def close(self):
        self._stop.set()


class ProjectSnapshotCache:
    """Separate public caches; one project's failure cannot hide its peers."""
    def __init__(self, registry, refresh_interval=300):
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
                self._caches[project_id] = SnapshotCache(project.monitor, self.refresh_interval)
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
            result.update(status=None, error=None)
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
                trusted_ingress=False, base_path="", index_path=None, tls_context=None, anonymous_lan=False):
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
    registry = cache.registry if isinstance(cache, ProjectSnapshotCache) else ProjectRegistry.single(monitor=cache.monitor)
    page = Path(index_path or Path(__file__).with_name("overview.html" if registry.multi_project else "index.html")).read_bytes()
    monitor_page = Path(index_path or Path(__file__).with_name("index.html")).read_bytes()
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

        def _send(self, code, value, *, content_type="application/json; charset=utf-8", challenge=False):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
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
            elif path in ("/monitor", "/monitor.html"):
                self._send(200, monitor_page, content_type="text/html; charset=utf-8")
            elif path == "/api/projects":
                self._send(200, public_projects())
            elif path == "/api/projects-status":
                if isinstance(cache, ProjectSnapshotCache):
                    self._send(200, cache.statuses())
                else:
                    result = _fields(project.metadata(read_only=True), {"id", "label", "engine"})
                    result.update(status=None, error=None)
                    try:
                        result["status"] = cache.get()
                    except Exception:
                        result["error"] = "Project data is temporarily unavailable"
                    self._send(200, {"projects": [result]})
            elif path == "/api/capabilities":
                self._send(200, {**CAPABILITIES, "refresh": project.monitor is not None, "default_branch": _scalar(project.default_branch),
                                 "project_id": _scalar(project.id), "label": _scalar(project.label), "multi_project": registry.multi_project,
                                 "engine": _scalar(project.engine_name), "workflow": False})
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

        def do_POST(self):
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
    parser.add_argument("--tls-cert", type=Path, help="PEM certificate chain for direct HTTPS")
    parser.add_argument("--tls-key", type=Path, help="Unencrypted PEM private key for direct HTTPS")
    parser.add_argument("--refresh-interval", type=int, default=300, help="Git/API refresh seconds; 0 disables background fetching")
    args = parser.parse_args(argv)
    if not args.monitor_config and not args.projects_config:
        parser.error("--monitor-config or --projects-config is required")
    password = None if args.anonymous_lan else os.environ.get(args.password_env)
    try:
        tls_context = load_tls_context(args.tls_cert, args.tls_key)
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
                             anonymous_lan=args.anonymous_lan)
        print(f"Read-only dashboard listening via {'HTTPS' if tls_context else 'HTTP'} on port {server.server_port}", flush=True)
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
