"""Private JumpServer authentication and complete, bounded permission inventories."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from fl_dashboard.agents.agent_settings import _dpapi
from fl_dashboard.agents.codex_sessions import atomic_json

MAX_ASSETS = 512
MAX_RESPONSE = 2 * 1024 * 1024
MAX_PAGES = 32
REQUEST_TIMEOUT = 15
DEFAULTS = {"enabled": False, "base_url": "", "username": "", "ssh_host": "", "ssh_port": 2222,
            "identity_file": "", "default_account": ""}
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@+-]{0,99}\Z")
LOGIN_ERROR = "JumpServer 登录失败，请检查账号、密码和服务器登录策略。"
AUTH_ERROR = "JumpServer 登录已失效或没有权限，请重新登录。"
INVENTORY_ERROR = "JumpServer 资产列表未完整返回，已保留上次结果，请重试。"
NETWORK_ERROR = "无法连接 JumpServer，请检查服务地址、网络和 HTTPS 证书后重试。"


class JumpServerError(ValueError):
    """An actionable, already redacted public error."""


class _Unauthorized(JumpServerError):
    pass


class _Cancelled(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(method, url, *, headers, json_body, timeout, max_bytes):
    """Never follow a redirect or trust an environment proxy with credentials."""
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    request = Request(url, data=data, headers=headers, method=method)
    opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        status = response.code
        pieces, length = [], 0
        deadline = time.monotonic() + timeout
        read = getattr(response, "read1", response.read)
        while length <= max_bytes:
            piece = read(min(65536, max_bytes + 1 - length))
            if not piece:
                break
            pieces.append(piece)
            length += len(piece)
            if time.monotonic() >= deadline:
                raise TimeoutError()
        raw = b"".join(pieces)
    if len(raw) > max_bytes:
        raise JumpServerError("JumpServer 响应超过上限，未采用结果。")
    if not 200 <= status < 300:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise JumpServerError("JumpServer 返回格式无效，请检查服务地址。") from None


def _base_url(value):
    if not isinstance(value, str) or not value or len(value) > 1000 or any(ord(c) <= 32 for c in value):
        raise JumpServerError("请输入有效的 JumpServer HTTPS 地址。")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        loopback = host == "localhost" or bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        try:
            parts = urlsplit(value)
            host, port, loopback = parts.hostname, parts.port, False
        except ValueError:
            raise JumpServerError("请输入有效的 JumpServer HTTPS 地址。") from None
    if (parts.scheme not in {"https", "http"} or (parts.scheme == "http" and not loopback)
            or not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment
            or "\\" in value or "?" in value or "#" in value or (port is not None and not 1 <= port <= 65535)):
        raise JumpServerError("JumpServer 地址必须使用 HTTPS，且不能包含凭据、查询或片段。")
    return value.rstrip("/")


def _text(value, maximum=200):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise JumpServerError("JumpServer 配置格式无效。")
    return value.strip()


def _settings(payload, previous):
    if not isinstance(payload, dict) or set(payload) - set(DEFAULTS):
        raise JumpServerError("JumpServer 设置包含不支持的字段。")
    value = {**previous, **payload}
    if not isinstance(value["enabled"], bool):
        raise JumpServerError("JumpServer enabled 必须为布尔值。")
    value["base_url"] = _text(value["base_url"], 1000)
    if value["base_url"]:
        value["base_url"] = _base_url(value["base_url"])
    value["username"] = _text(value["username"])
    if value["username"] and not _USER.fullmatch(value["username"]):
        raise JumpServerError("JumpServer 用户名格式无效。")
    host = _text(value["ssh_host"], 253)
    if ("base_url" in payload and "ssh_host" not in payload and previous.get("base_url")
            and host == urlsplit(previous["base_url"]).hostname):
        host = ""
    if not host and value["base_url"]:
        host = urlsplit(value["base_url"]).hostname or ""
    if host and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host):
        raise JumpServerError("KoKo SSH 主机地址格式无效。")
    value["ssh_host"] = host
    port = value["ssh_port"]
    if isinstance(port, bool) or not re.fullmatch(r"[0-9]{1,5}", str(port)) or not 1 <= int(port) <= 65535:
        raise JumpServerError("KoKo SSH 端口必须在 1 到 65535 之间。")
    value["ssh_port"] = int(port)
    identity = _text(value["identity_file"], 2000)
    if identity:
        path = Path(identity).expanduser()
        if any(c in identity for c in '\r\n\x00"%$`') or not path.is_absolute() or not path.is_file():
            raise JumpServerError("SSH 密钥路径必须是本机现有文件的绝对路径。")
        identity = str(path.resolve())
    value["identity_file"] = identity
    account = _text(value["default_account"])
    if account and not _USER.fullmatch(account):
        raise JumpServerError("默认资产账号格式无效。")
    value["default_account"] = account
    if value["enabled"] and (not value["base_url"] or not value["username"]):
        raise JumpServerError("请先填写 JumpServer 地址和用户名。")
    return value


def _asset(row):
    if not isinstance(row, dict):
        raise JumpServerError(INVENTORY_ERROR)
    raw = row.get("asset") if isinstance(row.get("asset"), dict) else row
    identity = raw.get("id") or row.get("asset_id")
    name = raw.get("name") or row.get("asset_name")
    if not isinstance(identity, str) or not _UUID.fullmatch(identity) or not isinstance(name, str) or not name.strip():
        raise JumpServerError(INVENTORY_ERROR)
    platform = raw.get("platform") or ""
    if isinstance(platform, dict):
        platform = platform.get("name") or ""
    tags = raw.get("tags") or []
    if not isinstance(tags, list) or len(tags) > 100:
        raise JumpServerError(INVENTORY_ERROR)
    clean_tags = []
    for tag in tags:
        tag = tag.get("name") if isinstance(tag, dict) else tag
        clean_tags.append(_text(tag))
    protocols = raw.get("protocols")
    supported = True
    if protocols is not None:
        if not isinstance(protocols, list):
            raise JumpServerError(INVENTORY_ERROR)
        supported = any((p.get("name") if isinstance(p, dict) else p) == "ssh" for p in protocols)
    return {"id": identity.lower(), "name": _text(name, 300), "accounts": [], "platform": _text(platform),
            "tags": sorted(set(clean_tags)), "ssh_supported": supported}


class JumpServerInventory:
    """Shared source of authorized assets; never returns tokens or saves passwords."""

    def __init__(self, directory, *, encrypt=None, decrypt=None, requester=None):
        self.path = Path(directory) / "jumpserver.json"
        self._encrypt = encrypt or (_dpapi if os.name == "nt" else None)
        self._decrypt = decrypt or ((lambda data: _dpapi(data, decrypt=True)) if os.name == "nt" else None)
        self._requester = requester or _request
        self._lock = threading.RLock()
        self._settings = dict(DEFAULTS)
        self._token = ""
        self._assets = []
        self._authenticated = False
        self._refreshing = False
        self._updated_at = None
        self._error = None
        self._generation = 0
        self._revision = 0
        self._closed = False
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jumpserver-list")
        self._accounts = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jumpserver-account")
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError()
            record = json.loads(raw)
            if record.get("schema_version") != 1:
                raise ValueError()
            self._settings = _settings(record.get("settings", {}), DEFAULTS)
            assets = record.get("assets", [])
            if not isinstance(assets, list) or len(assets) > MAX_ASSETS:
                raise ValueError()
            for row in assets:
                clean = _asset(row)
                accounts = row.get("accounts", [])
                if not isinstance(accounts, list) or any(not isinstance(a, str) or not _USER.fullmatch(a) for a in accounts):
                    raise ValueError()
                clean["accounts"] = list(dict.fromkeys(accounts))
                clean["ssh_supported"] = bool(row.get("ssh_supported", True))
                self._assets.append(clean)
            self._updated_at = record.get("updated_at") if isinstance(record.get("updated_at"), str) else None
            encrypted = record.get("encrypted_token")
            if encrypted and self._decrypt:
                token = self._decrypt(base64.b64decode(encrypted, validate=True)).decode("utf-8")
                self._token = self._validate_token(token)
            # A restored credential is not proof that today's permissions are unchanged.
            self._error = "请刷新 JumpServer 资产列表。" if self._token else ("请登录 JumpServer。" if self._settings["enabled"] else None)
        except Exception:
            self._token = ""
            self._assets = []
            self._authenticated = False
            self._error = "JumpServer 本机配置无法读取，请重新配置并登录。"

    @staticmethod
    def _validate_token(token):
        if not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise JumpServerError("JumpServer 需要 MFA/SSO 等交互认证，或未返回可用登录令牌。")
        return token

    def _save(self):
        try:
            encrypted = None
            if self._token and self._encrypt:
                encrypted = base64.b64encode(self._encrypt(self._token.encode("utf-8"))).decode("ascii")
            record = {"schema_version": 1, "settings": self._settings, "encrypted_token": encrypted,
                      "assets": self._assets, "updated_at": self._updated_at}
            atomic_json(self.path, record)
            if os.name != "nt":
                self.path.chmod(0o600)
                self.path.parent.chmod(0o700)
        except Exception:
            raise JumpServerError("无法安全保存 JumpServer 本机配置，请检查存储目录权限。") from None

    def _snapshot(self):
        return {**self._settings, "authenticated": self._authenticated and bool(self._token),
                "refreshing": self._refreshing, "updated_at": self._updated_at, "error": self._error,
                "asset_count": len(self._assets), "assets": copy.deepcopy(self._assets)}

    def snapshot(self):
        with self._lock:
            return self._snapshot()

    def catalog(self):
        with self._lock:
            return {**self._snapshot(), "generation": self._generation}

    def _open(self):
        if self._closed:
            raise JumpServerError("JumpServer 服务已停止。")

    def configure(self, payload):
        with self._lock:
            self._open()
            value = _settings(payload, self._settings)
            identity_changed = any(value[k] != self._settings[k] for k in ("base_url", "username"))
            if value != self._settings:
                self._revision += 1
                self._generation += 1
                self._refreshing = False
                if identity_changed:
                    self._token, self._assets, self._updated_at = "", [], None
                    self._authenticated = False
                if not value["enabled"]:
                    self._authenticated = False
                self._settings = value
                self._error = "请登录 JumpServer。" if value["enabled"] and not self._token else None
                self._save()
            return self._snapshot()

    def login(self, payload):
        if not isinstance(payload, dict) or set(payload) - (set(DEFAULTS) | {"password"}):
            raise JumpServerError("JumpServer 登录参数格式无效。")
        password = payload.get("password")
        if not isinstance(password, str) or not password or len(password) > 4096 or "\x00" in password:
            raise JumpServerError("请输入 JumpServer 密码。")
        self.configure({**{k: v for k, v in payload.items() if k in DEFAULTS}, "enabled": True})
        with self._lock:
            self._open()
            self._revision += 1
            self._generation += 1
            revision = self._revision
            settings = dict(self._settings)
            self._token, self._authenticated, self._refreshing = "", False, False
            self._save()
        try:
            status, body = self._call("POST", self._url(settings, "authentication/auth/"), None,
                                      {"username": settings["username"], "password": password})
            if not 200 <= status < 300:
                raise JumpServerError(LOGIN_ERROR)
            token = self._validate_token(body.get("token") if isinstance(body, dict) else None)
            with self._lock:
                self._ensure_active(revision)
                self._token, self._authenticated, self._error = token, True, None
                self._save()
        except _Cancelled:
            raise JumpServerError("JumpServer 设置已更改，请重新登录。") from None
        except JumpServerError as exc:
            with self._lock:
                if revision == self._revision and not self._closed:
                    self._token, self._authenticated, self._error = "", False, str(exc)
                    self._generation += 1
            raise
        return self.refresh()

    @staticmethod
    def _url(settings, path):
        return settings["base_url"] + "/api/v1/" + path

    def _call(self, method, url, token, body=None):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            result = self._requester(method, url, headers=headers, json_body=body,
                                     timeout=REQUEST_TIMEOUT, max_bytes=MAX_RESPONSE)
            status, payload = result
            if not isinstance(status, int) or not 100 <= status <= 599:
                raise ValueError()
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE:
                raise JumpServerError("JumpServer 响应超过上限，未采用结果。")
            return status, payload
        except JumpServerError:
            raise
        except Exception:
            raise JumpServerError(NETWORK_ERROR) from None

    def _ensure_active(self, revision):
        if self._closed or self._revision != revision:
            raise _Cancelled()

    def _pages(self, settings, token, path, revision, maximum, *, asset_ids=False):
        base = self._url(settings, path)
        offset, result, expected, seen_offsets = 0, [], None, set()
        for _ in range(MAX_PAGES):
            with self._lock:
                self._ensure_active(revision)
            if offset in seen_offsets:
                raise JumpServerError(INVENTORY_ERROR)
            seen_offsets.add(offset)
            url = base + "?" + urlencode({"limit": min(100, maximum + 1 - len(result)), "offset": offset})
            status, payload = self._call("GET", url, token)
            if status in (401, 403):
                raise _Unauthorized(AUTH_ERROR)
            if not 200 <= status < 300:
                raise JumpServerError(INVENTORY_ERROR)
            if isinstance(payload, list):
                rows, following, explicit_next = payload, None, False
            elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
                rows, following, explicit_next = payload["results"], payload.get("next"), "next" in payload
                count = payload.get("count")
                if count is not None:
                    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= maximum:
                        raise JumpServerError(INVENTORY_ERROR)
                    if expected is not None and count != expected:
                        raise JumpServerError(INVENTORY_ERROR)
                    expected = count
            else:
                raise JumpServerError(INVENTORY_ERROR)
            if any(not isinstance(row, dict) for row in rows):
                raise JumpServerError(INVENTORY_ERROR)
            result.extend(rows)
            if len(result) > maximum or (expected is not None and len(result) > expected):
                raise JumpServerError(INVENTORY_ERROR)
            if following:
                if not isinstance(following, str) or not rows:
                    raise JumpServerError(INVENTORY_ERROR)
                next_url, origin = urlsplit(urljoin(url, following)), urlsplit(base)
                if (next_url.scheme, next_url.netloc, next_url.path) != (origin.scheme, origin.netloc, origin.path) or next_url.fragment:
                    raise JumpServerError(INVENTORY_ERROR)
                query = parse_qs(next_url.query)
                if set(query) - {"offset", "limit"} or len(query.get("offset", [])) != 1:
                    raise JumpServerError(INVENTORY_ERROR)
                try:
                    next_offset = int(query["offset"][0])
                except ValueError:
                    raise JumpServerError(INVENTORY_ERROR) from None
                if next_offset != offset + len(rows):
                    raise JumpServerError(INVENTORY_ERROR)
                offset = next_offset
                continue
            if explicit_next or (expected is not None and len(result) >= expected) or (expected is None and len(rows) < 100):
                if expected is not None and len(result) != expected:
                    raise JumpServerError(INVENTORY_ERROR)
                if asset_ids and expected is not None and len({_asset(row)["id"] for row in result}) != expected:
                    raise JumpServerError(INVENTORY_ERROR)
                return result
            if not rows:
                raise JumpServerError(INVENTORY_ERROR)
            offset += len(rows)
        raise JumpServerError(INVENTORY_ERROR)

    def _fetch(self, settings, token, revision):
        rows = self._pages(settings, token, "perms/users/my/assets/", revision, MAX_ASSETS, asset_ids=True)
        assets = {}
        for row in rows:
            asset = _asset(row)
            if asset["id"] in assets and assets[asset["id"]] != asset:
                raise JumpServerError(INVENTORY_ERROR)
            assets[asset["id"]] = asset

        def accounts(asset):
            rows = self._pages(settings, token, "perms/users/assets/" + asset["id"] + "/accounts/", revision, 128)
            values = []
            for row in rows:
                username = row.get("username") or row.get("alias")
                if not isinstance(username, str) or not _USER.fullmatch(username):
                    raise JumpServerError("JumpServer 授权账号列表无效，请检查资产账号配置。")
                values.append(username)
            return asset["id"], list(dict.fromkeys(values))

        pending = []
        try:
            for asset in assets.values():
                with self._lock:
                    self._ensure_active(revision)
                if asset["ssh_supported"]:
                    pending.append(self._accounts.submit(accounts, asset))
            for future in as_completed(pending):
                identity, values = future.result()
                assets[identity]["accounts"] = values
            return sorted(assets.values(), key=lambda row: (row["name"].casefold(), row["id"]))
        finally:
            for future in pending:
                future.cancel()

    def refresh(self):
        with self._lock:
            self._open()
            if not self._settings["enabled"]:
                return self._snapshot()
            if not self._token:
                raise JumpServerError("请先登录 JumpServer，再刷新授权资产。")
            if not self._refreshing:
                self._refreshing, self._error = True, None
                self._worker.submit(self._refresh, dict(self._settings), self._token, self._revision)
            return self._snapshot()

    def _refresh(self, settings, token, revision):
        try:
            assets = self._fetch(settings, token, revision)
            with self._lock:
                self._ensure_active(revision)
                previous = self._assets, self._updated_at
                self._assets = assets
                self._updated_at = datetime.now(timezone.utc).isoformat()
                try:
                    self._save()
                except JumpServerError:
                    self._assets, self._updated_at = previous
                    raise
                self._authenticated, self._error = True, None
                self._generation += 1
        except _Cancelled:
            pass
        except Exception as exc:
            with self._lock:
                if revision == self._revision and not self._closed:
                    self._authenticated = False
                    self._error = str(exc) if isinstance(exc, JumpServerError) else INVENTORY_ERROR
                    self._generation += 1
                    if isinstance(exc, _Unauthorized):
                        self._token = ""
                        try:
                            self._save()
                        except JumpServerError:
                            self._error = "JumpServer 登录已失效，且无法清理本机凭据；请检查存储目录权限。"
        finally:
            with self._lock:
                if revision == self._revision:
                    self._refreshing = False

    def logout(self):
        with self._lock:
            self._open()
            self._revision += 1
            self._generation += 1
            self._settings["enabled"] = False
            self._token, self._authenticated, self._refreshing, self._error = "", False, False, None
            self._save()
            return self._snapshot()

    def close(self):
        with self._lock:
            self._closed = True
            self._revision += 1
            self._refreshing = False
            self._token = ""
        self._worker.shutdown(wait=False, cancel_futures=True)
        self._accounts.shutdown(wait=False, cancel_futures=True)
