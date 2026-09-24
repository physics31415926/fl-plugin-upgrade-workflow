#!/usr/bin/env python3
"""Read-only deployment of the branch dashboard, without the control server.

Non-loopback HTTP requires Basic auth or --trusted-ingress. The latter means
authentication/TLS is enforced by a trusted reverse proxy or private ingress.
Set the password in an environment variable; never put it in a config or URL.
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
import threading
from urllib.parse import parse_qs, urlsplit

from branch_monitor import BranchMonitor


CAPABILITIES = {"read_only": True, "sessions": False, "refresh": False, "evidence": False}
STATUS_FIELDS = {"status", "scope", "commit", "target_commit", "tree", "tree_matches_head", "run_id", "attempt"}
GATE_FIELDS = {"status", "passed", "total", "failed", "missing", "requests_passed", "requests_total",
               "latest_commit", "evidence_type", "tree_matches_head", "recorded_cases", "raw_results_available"}
RESULT_FIELDS = {"model", "mode", "scenario", "requests", "status", "requests_passed", "evidence_type", "raw_results_available"}
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
    result = _fields(snapshot, {"branch", "branch_ref", "head", "observed_at", "vllm_version"})
    result["head_url"] = _public_url(snapshot.get("head_url"))
    for key in ("branches", "models", "modes"):
        result[key] = [clean for value in snapshot.get(key, []) if (clean := _scalar(value)) is not None][:100]
    result["cases"] = [_fields(case, {"scenario", "requests", "file", "test"}) for case in snapshot.get("cases", [])[:100] if isinstance(case, dict)]
    result["branch_ci"] = _status(snapshot.get("branch_ci", {}))
    result["backends"] = []
    for backend in snapshot.get("backends", [])[:50]:
        if not isinstance(backend, dict):
            continue
        public = _fields(backend, {"id", "label", "ci_enabled"})
        for key in ("integration", "ci", "current_ci"):
            public[key] = _status(backend.get(key, {}))
        pr = backend.get("pr")
        public["pr"] = {**_fields(pr, {"number", "title", "draft"}), "url": _public_url(pr.get("url"))} if isinstance(pr, dict) else None
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
                    value = sanitize_snapshot(self.monitor.refresh_branch(branch=branch))
                    with self._lock:
                        self._snapshots[branch] = value
                        self.allowed_branches.update(value.get("branches", []))
                except Exception:
                    with self._lock:
                        self._snapshots[branch]["errors"] = ["上游刷新暂不可用；当前显示上次记录。"]

    def close(self):
        self._stop.set()


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
                trusted_ingress=False, base_path="", index_path=None):
    if password is not None and (not isinstance(password, str) or not password or len(password.encode("utf-8")) > 1024 or any(c in password for c in "\r\n\x00")):
        raise ValueError("Password must be nonempty, at most 1024 UTF-8 bytes, and contain no control newline")
    if not isinstance(username, str) or not username or len(username.encode("utf-8")) > 128 or any(c in username for c in ":\r\n\x00"):
        raise ValueError("Invalid Basic auth username")
    if not _loopback(host) and password is None and not trusted_ingress:
        raise ValueError("Non-loopback binding requires an environment password or --trusted-ingress with external ingress authentication")
    prefix = "/" + base_path.strip("/") if base_path.strip("/") else ""
    if prefix and not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", prefix):
        raise ValueError("base_path must contain simple URL path segments")
    page = Path(index_path or Path(__file__).with_name("index.html")).read_bytes()
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
            if path in ("/", "/index.html"):
                self._send(200, page, content_type="text/html; charset=utf-8")
            elif path == "/api/capabilities":
                self._send(200, {**CAPABILITIES, "default_branch": cache.default_branch})
            elif path == "/healthz":
                self._send(200, {"ok": True, "read_only": True})
            elif path == "/api/branch-status":
                try:
                    query = parse_qs(parsed.query, max_num_fields=5)
                    branch = query.get("branch", [None])[0]
                    self._send(200, cache.get(branch))
                except ValueError:
                    self._send(400, {"error": "Invalid or unavailable branch"})
                except Exception:
                    self._send(503, {"error": "Branch data is temporarily unavailable"})
            else:
                self._send(404, {"error": "Not available in the read-only dashboard"})

        def do_POST(self):
            if self._authorized():
                self._send(405, {"error": "This shared dashboard is read-only"})

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        do_OPTIONS = do_POST

    return BoundedHTTPServer((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitor-config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--username", default="viewer")
    parser.add_argument("--password-env", default="FLFLOW_SHARE_PASSWORD")
    parser.add_argument("--trusted-ingress", action="store_true", help="Trust a private/reverse-proxy ingress that provides access control and TLS")
    parser.add_argument("--base-path", default="")
    parser.add_argument("--refresh-interval", type=int, default=300, help="Git/API refresh seconds; 0 disables background fetching")
    args = parser.parse_args(argv)
    cache = None
    server = None
    try:
        cache = SnapshotCache(BranchMonitor(args.monitor_config), args.refresh_interval)
        server = make_server(cache, args.host, args.port, username=args.username, password=os.environ.get(args.password_env),
                             trusted_ingress=args.trusted_ingress, base_path=args.base_path)
        print(f"Read-only dashboard listening on port {server.server_port}", flush=True)
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
