"""Revocable capability links for an explicitly shared dashboard workspace.

Only the loopback control service writes this store. The LAN gateway reads it
on every authenticated request so revocation does not depend on a cache or
process restart. Bearer tokens are returned once and never persisted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit, urlunsplit
import uuid


MAX_ACTIVE_SHARES = 100
MAX_STORED_SHARES = 200
MAX_STORE_BYTES = 256 * 1024
_DEFAULT_EXPIRY = object()
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_PATH = re.compile(r"(?:/[A-Za-z0-9_-]+)*\Z")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def validate_public_url(value: str) -> str:
    """Return a canonical HTTPS deployment root, optionally with a base path."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("分享地址必须是有效的 HTTPS 地址")
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("分享地址包含无效字符")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValueError("分享地址必须是有效的 HTTPS 地址") from exc
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or "?" in value or "#" in value or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("分享地址必须使用 HTTPS，且不能包含账号、查询参数或片段")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                     for label in host.split(".")):
            raise ValueError("分享地址的主机名无效") from None
        authority = host.lower()
    else:
        if address.is_unspecified or address.is_multicast:
            raise ValueError("分享地址必须指定可访问的主机")
        authority = f"[{address.compressed}]" if address.version == 6 else address.compressed
    if port not in (None, 443):
        authority += f":{port}"
    path = parsed.path.rstrip("/")
    if not _PATH.fullmatch(path):
        raise ValueError("分享地址的路径只能包含字母、数字、下划线和连字符")
    return urlunsplit(("https", authority, path, "", ""))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("Invalid share timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _label(value: object) -> str:
    if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("分享名称最多 80 个字符，且不能包含控制字符")
    return value.strip()


def _token_hash(token: str) -> str:
    return hashlib.sha256(("fl-dashboard-access-v1:" + token).encode("ascii")).hexdigest()


class AccessShareStore:
    """Hash-only, atomically replaced grants; one local writer, many readers."""

    def __init__(self, path: str | Path, public_url: str | None = None):
        self.path = Path(path)
        self.public_url = validate_public_url(public_url) if public_url is not None else None
        key = os.path.normcase(str(self.path.absolute()))
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > MAX_STORE_BYTES:
                raise ValueError("Invalid share store")
            raw = self.path.read_bytes()
            if len(raw) > MAX_STORE_BYTES:
                raise ValueError("Share store is too large")
            data = json.loads(raw)
            if (not isinstance(data, dict) or data.get("version") != 1
                    or set(data) != {"version", "shares"} or not isinstance(data.get("shares"), list)
                    or len(data["shares"]) > MAX_STORED_SHARES):
                raise ValueError("Invalid share store")
            ids, hashes = set(), set()
            for share in data["shares"]:
                fields = {"id", "label", "created_at", "expires_at", "revoked_at", "token_hash"}
                if (not isinstance(share, dict) or set(share) not in (fields, fields | {"permanent"})
                        or not isinstance(share.get("id"), str) or not _ID.fullmatch(share["id"])
                        or not isinstance(share.get("token_hash"), str) or not _HASH.fullmatch(share["token_hash"])):
                    raise ValueError("Invalid share record")
                _label(share["label"])
                created = _parse_time(share["created_at"])
                permanent = share.get("permanent", False)
                if type(permanent) is not bool:
                    raise ValueError("Invalid permanent-share flag")
                if permanent:
                    if share["expires_at"] is not None:
                        raise ValueError("Permanent shares cannot have an expiry")
                else:
                    # Legacy records without an explicit flag remain expiring.
                    expires = _parse_time(share["expires_at"])
                    if not timedelta(hours=1) <= expires - created <= timedelta(hours=168):
                        raise ValueError("Invalid share expiry")
                if share["revoked_at"] is not None and _parse_time(share["revoked_at"]) < created:
                    raise ValueError("Invalid share revocation")
                if share["id"] in ids or share["token_hash"] in hashes:
                    raise ValueError("Duplicate share record")
                ids.add(share["id"])
                hashes.add(share["token_hash"])
            return data["shares"]
        except (OSError, UnicodeError, ValueError, TypeError, OverflowError) as exc:
            raise ValueError("分享权限存档无法读取，请检查本机配置；现有授权已暂停") from exc

    def _write(self, shares: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("分享权限存档不能使用符号链接")
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                descriptor = None
                json.dump({"version": 1, "shares": shares}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _metadata(share: dict, now: datetime) -> dict:
        permanent = share.get("permanent", False)
        expired = not permanent and _parse_time(share["expires_at"]) <= now
        status = "revoked" if share["revoked_at"] else "expired" if expired else "active"
        return {**{key: share[key] for key in ("id", "label", "created_at", "expires_at", "revoked_at")},
                "permanent": permanent, "status": status, "active": status == "active"}

    def list(self) -> list[dict]:
        with self._lock:
            now = _utc_now()
            return [self._metadata(share, now) for share in reversed(self._read())]

    def create(self, label: str = "", expires_in_hours=_DEFAULT_EXPIRY, *, permanent: bool = False) -> dict:
        label = _label(label)
        if type(permanent) is not bool:
            raise ValueError("永久有效选项必须是布尔值")
        if permanent and expires_in_hours is not _DEFAULT_EXPIRY:
            raise ValueError("永久有效与限时有效期不能同时设置")
        if expires_in_hours is _DEFAULT_EXPIRY:
            expires_in_hours = 24
        if type(expires_in_hours) is not int or not 1 <= expires_in_hours <= 168:
            raise ValueError("有效期必须是 1 到 168 小时的整数")
        with self._lock:
            now = _utc_now().replace(microsecond=0)
            shares = self._read()
            active = [item for item in shares if self._metadata(item, now)["active"]]
            if len(active) >= MAX_ACTIVE_SHARES:
                raise ValueError("有效分享链接已达上限，请先撤销不再使用的链接")
            # Preserve every active grant; bound old audit history without growing indefinitely.
            if len(shares) >= MAX_STORED_SHARES:
                remove = len(shares) - MAX_STORED_SHARES + 1
                kept = []
                for item in shares:
                    if remove and not self._metadata(item, now)["active"]:
                        remove -= 1
                    else:
                        kept.append(item)
                shares = kept
            token = secrets.token_urlsafe(32)
            share = {"id": uuid.uuid4().hex, "label": label, "created_at": _format_time(now),
                     "expires_at": None if permanent else _format_time(now + timedelta(hours=expires_in_hours)),
                     "permanent": permanent, "revoked_at": None, "token_hash": _token_hash(token)}
            self._write([*shares, share])
            result = {**self._metadata(share, now), "token": token}
            if self.public_url:
                result["url"] = self.public_url + "/access#token=" + token
            return result

    def revoke(self, share_id: str) -> dict:
        if not isinstance(share_id, str) or not _ID.fullmatch(share_id):
            raise ValueError("无效的分享链接编号")
        with self._lock:
            shares = self._read()
            now = _utc_now()
            for share in shares:
                if share["id"] == share_id:
                    if not share["revoked_at"]:
                        share["revoked_at"] = _format_time(max(now, _parse_time(share["created_at"])))
                        self._write(shares)
                    return self._metadata(share, now)
            raise ValueError("分享链接不存在")

    def validate(self, token: object) -> dict | None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return None
        try:
            with self._lock:
                shares = self._read()
            digest = _token_hash(token)
            now = _utc_now()
            for share in shares:
                if hmac.compare_digest(digest, share["token_hash"]):
                    metadata = self._metadata(share, now)
                    return metadata if metadata["active"] else None
        except (OSError, ValueError):
            # An unreadable or corrupt store must never authorize a request.
            pass
        return None
