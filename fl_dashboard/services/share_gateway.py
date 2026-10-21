"""Optional, capability-authenticated forwarding to the loopback dashboard.

The public server never instantiates an agent executor. Its only upstream is the
explicit loopback control origin, and its forwarding surface is an allowlist.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.client import HTTPConnection
import json
import re
from urllib.parse import urlsplit

from fl_dashboard.paths import TEMPLATES
from fl_dashboard.services.access_shares import validate_public_url

COOKIE_NAME = "__Host-fl_access"
MAX_BODY = 1_000_000
MAX_RESPONSE = 16 * 1024 * 1024
GET_ROUTES = frozenset({
    "/", "/index.html", "/monitor", "/monitor.html", "/workflow", "/workflow.html",
    "/sessions", "/sessions.html", "/api/projects", "/api/projects-status",
    "/api/capabilities", "/api/agent-settings", "/api/agent-providers",
    "/api/adaptation-tasks", "/api/codex-sessions", "/api/codex-health",
    "/api/branches", "/api/branch-status", "/api/acceptance-record",
    "/api/branch-evidence", "/api/status", "/api/logs",
})
POST_ROUTES = frozenset({
    "/api/agent-settings", "/api/codex-machines/sync-ssh", "/api/codex-machines",
    "/api/codex-machines/check", "/api/codex-sessions", "/api/branch-refresh",
    "/api/start", "/api/retry", "/api/retry-failed", "/api/pause", "/api/resume", "/api/cancel",
})


def validate_control_url(value):
    """Accept only an HTTP origin on a literal loopback address, never a proxy URL."""
    if (not isinstance(value, str) or any(ord(c) < 33 or ord(c) == 127 for c in value)
            or any(c in value for c in "?#\\")):
        raise ValueError("Control URL must be a literal loopback HTTP origin")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "::1")
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and parsed.username is None and parsed.password is None
                 and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Control URL must be a literal loopback HTTP origin")
    authority = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{authority}:{parsed.port}"


class ShareGateway:
    def __init__(self, store, control_url, *, tls_context, base_path=""):
        if tls_context is None or base_path:
            raise ValueError("Access sharing requires direct HTTPS at the root URL")
        self.store = store
        self.public_url = validate_public_url(store.public_url)
        public = urlsplit(self.public_url)
        if public.path:
            raise ValueError("Access sharing requires a root public URL without a base path")
        self.authority = public.netloc.lower()
        self.control_url = validate_control_url(control_url)
        self.control = urlsplit(self.control_url)

    @staticmethod
    def _cookie(handler):
        values = handler.headers.get_all("Cookie", [])
        if not values:
            return False, None
        raw = ";".join(values)
        if len(values) != 1 or len(raw) > 8192:
            return True, None
        tokens = [part.strip().partition("=")[2] for part in raw.split(";")
                  if part.strip().partition("=")[0] == COOKIE_NAME]
        if not tokens:
            return False, None
        if len(tokens) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", tokens[0]):
            return True, None
        return True, tokens[0]

    def _trusted(self, handler, *, post=False, landing=False):
        hosts = handler.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() != self.authority:
            handler._send(403, {"error": "Host must match the configured sharing address"})
            return False
        if post:
            origins = handler.headers.get_all("Origin", [])
            if origins != [self.public_url]:
                handler._send(403, {"error": "Control requests must originate from this dashboard"})
                return False
        if not landing and handler.headers.get("Sec-Fetch-Site", "").lower() not in ("", "same-origin", "none"):
            handler._send(403, {"error": "Cross-site access is not allowed"})
            return False
        return True

    @staticmethod
    def _body(handler, limit=MAX_BODY):
        if (handler.headers.get_all("Transfer-Encoding") or
                len(handler.headers.get_all("Content-Length", [])) != 1):
            raise ValueError("A single Content-Length is required")
        raw_length = handler.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,8}", raw_length) or int(raw_length) > limit:
            raise ValueError("Request body is too large")
        if handler.headers.get("Content-Type", "").partition(";")[0].strip().lower() != "application/json":
            raise ValueError("Content-Type must be application/json")
        length = int(raw_length)
        data = handler.rfile.read(length)
        if len(data) != length:
            raise ValueError("Incomplete request body")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        return data, value

    @staticmethod
    def _clear_cookie():
        return f"{COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"

    def handle(self, handler):
        """Return True when this request has been handled; False keeps read-only mode."""
        parsed = urlsplit(handler.path)
        path = parsed.path
        has_cookie, token = self._cookie(handler)
        special = path in ("/access", "/api/share-login", "/api/share-logout")
        if not special and not has_cookie:
            return False
        if not self._trusted(handler, post=handler.command == "POST",
                             landing=path == "/access" and handler.command in ("GET", "HEAD")):
            return True
        if (parsed.scheme or parsed.netloc or parsed.fragment or "\\" in handler.path
                or len(handler.path) > 8192 or any(ord(c) < 32 for c in handler.path)):
            handler._send(400, {"error": "Invalid dashboard path"})
            return True
        if path == "/access" and handler.command in ("GET", "HEAD"):
            if parsed.query:
                handler._send(400, {"error": "Open the complete sharing link"})
            else:
                handler._send(200, (TEMPLATES / "access.html").read_bytes(), content_type="text/html; charset=utf-8")
            return True
        if path in ("/api/share-login", "/api/share-logout"):
            if handler.command != "POST" or parsed.query:
                handler._send(405, {"error": "Use a same-origin JSON POST"})
                return True
            try:
                _, body = self._body(handler, 4096)
                if path.endswith("logout"):
                    if body:
                        raise ValueError("Logout does not accept fields")
                    handler._send(200, {"read_only": True}, cookie=self._clear_cookie())
                    return True
                if set(body) != {"token"} or not isinstance(body["token"], str):
                    raise ValueError("A sharing token is required")
                grant = self.store.validate(body["token"])
                if grant is None:
                    handler._send(401, {"error": "Sharing link is invalid, expired or revoked"}, cookie=self._clear_cookie())
                    return True
                expires = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
                seconds = int((expires - datetime.now(timezone.utc)).total_seconds())
                if seconds <= 0:
                    handler._send(401, {"error": "Sharing link has expired"}, cookie=self._clear_cookie())
                    return True
                cookie = (f"{COOKIE_NAME}={body['token']}; Path=/; Max-Age={seconds}; "
                          "Secure; HttpOnly; SameSite=Strict")
                handler._send(200, {"read_only": False, "expires_at": grant["expires_at"]}, cookie=cookie)
            except (ValueError, TypeError, UnicodeError):
                handler._send(400, {"error": "Invalid sharing request"})
            except Exception:
                handler._send(503, {"error": "Sharing access is temporarily unavailable"})
            return True
        try:
            grant = self.store.validate(token) if token else None
        except Exception:
            handler._send(503, {"error": "Sharing access is temporarily unavailable"})
            return True
        if grant is None:
            handler._send(401, {"error": "Sharing access has expired or been revoked"}, cookie=self._clear_cookie())
            return True
        read = handler.command in ("GET", "HEAD")
        allowed = ((path in GET_ROUTES or re.fullmatch(r"/api/codex-sessions/[0-9a-f]{32}/events", path)) if read else
                   handler.command == "POST" and (path in POST_ROUTES or re.fullmatch(
                       r"/api/codex-sessions/[0-9a-f]{32}/(?:start|message|interrupt|respond|desktop|delete|restore)", path)))
        if not allowed:
            handler._send(403, {"error": "This operation is available only on the host computer"})
            return True
        try:
            body = self._body(handler)[0] if handler.command == "POST" else None
        except (ValueError, TypeError, UnicodeError):
            handler._send(400, {"error": "Invalid control request body"})
            return True
        if handler.command == "POST":
            # A grant can be revoked while a slow request body is being read.
            try:
                still_valid = self.store.validate(token)
            except Exception:
                still_valid = None
            if still_valid is None:
                handler._send(401, {"error": "Sharing access has expired or been revoked"}, cookie=self._clear_cookie())
                return True
        self._forward(handler, body, path)
        return True

    def _forward(self, handler, body, path):
        connection = HTTPConnection(self.control.hostname, self.control.port, timeout=120)
        headers = {"Host": self.control.netloc, "Connection": "close"}
        if handler.command == "POST":
            headers.update({"Origin": self.control_url, "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"})
        try:
            # Only the validated path/query, never client credentials or forwarding headers.
            connection.request("GET" if handler.command == "HEAD" else handler.command,
                               handler.path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE or 300 <= response.status < 400 or response.status < 200:
                raise ValueError("Invalid upstream response")
            content_type = response.getheader("Content-Type", "application/json; charset=utf-8")
            if path == "/api/capabilities" and response.status == 200:
                capabilities = json.loads(data)
                if not isinstance(capabilities, dict):
                    raise ValueError("Invalid capabilities response")
                capabilities.update(access_shares=False, shared_access=True)
                data = json.dumps(capabilities, ensure_ascii=False).encode("utf-8")
            handler._send(response.status, data, content_type=content_type)
        except Exception:
            handler._send(502, {"error": "The local dashboard is temporarily unavailable; please retry"})
        finally:
            connection.close()
