"""Bounded local history of device display metrics, never connection credentials.

Both writes and reads project an allowlist. Cached availability is always unknown;
only a fresh live probe may make a device available again. Process lists, sample
counters, private profiles and original error messages are deliberately omitted.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import threading
import uuid

MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 2048
MAX_CARDS = 256
_SCHEMA = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_BACKENDS = {"cuda", "ascend", "kunlunxin", "metax", "enflame", "musa", "tsingmicro", "thead", "corex", "hygon", "sunrise"}
_FAMILIES = {"nvidia", "ascend", "kunlunxin", "metax", "enflame", "mthreads", "tsingmicro", "thead", "iluvatar", "hygon"}
_HISTORY = "本地缓存记录，等待重新采集"
_SECRET = re.compile(r"(?i)(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9_]{12,}|github_pat_[a-z0-9_]+)")


def _text(value, limit=120):
    if not isinstance(value, str):
        return ""
    value = "".join(c for c in value[:limit * 4] if c.isprintable()).strip()[:limit]
    return _SECRET.sub("[redacted]", value)


def _number(value, minimum=0, maximum=2**40):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and minimum <= value <= maximum else None
    except OverflowError:
        return None


def _integer(value, maximum=MAX_CARDS):
    return value if type(value) is int and 0 <= value <= maximum else None


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc).isoformat(timespec="seconds") if stamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _backend(value):
    return value if isinstance(value, str) and value in _BACKENDS else ""


def _card(value):
    if not isinstance(value, dict):
        return None
    used, total = _number(value.get("mem_used")), _number(value.get("mem_total"))
    if not total:
        total = None
    if used is not None and total is not None and used > total:
        used = None
    return {"index": _integer(value.get("index"), 1000000), "card": _integer(value.get("card"), 1000000),
            "name": _text(value.get("name"), 100), "util": _number(value.get("util"), maximum=100),
            "mem_used": used, "mem_total": total,
            "temp": _number(value.get("temp"), minimum=-50, maximum=250),
            "power": _number(value.get("power"), maximum=100000),
            "busy": None, "residual": False, "state": "unknown", "procs": [], "procs_shared": False}


def _view(value, entry):
    if not isinstance(value, dict):
        value = {}
    accel = value.get("accel") if isinstance(value.get("accel"), dict) else {}
    node = value.get("node") if isinstance(value.get("node"), dict) else {}
    raw_cards = accel.get("cards") if isinstance(accel.get("cards"), list) else []
    cards = [card for value in raw_cards[:MAX_CARDS] if (card := _card(value)) is not None]
    total = max(len(cards), _integer(accel.get("total"), MAX_CARDS) or 0)
    physical = _integer(accel.get("physical_cards"), total) or len({c["card"] for c in cards if c["card"] is not None}) or total
    native_family = accel.get("family")
    family = native_family if isinstance(native_family, str) and native_family in _FAMILIES else "unknown"
    backend = _backend(value.get("backend")) or entry["backend"]
    memory_total = _number(node.get("mem_total_mib"))
    memory_available = _number(node.get("mem_avail_mib"))
    if memory_total is not None and memory_available is not None and memory_available > memory_total:
        memory_available = None
    return {"reachable": None, "verdict": "unknown", "stale": True, "error": _HISTORY,
            "updated_at": entry["updated_at"], "backend": backend,
            "backend_label": _text(value.get("backend_label")) or entry["backend_label"],
            "supported": value.get("supported") is True and family != "unknown",
            "accel": {"family": family, "label": _text(accel.get("label")),
                      "card_name": _text(accel.get("card_name"), 100), "total": total,
                      "free": 0, "busy": 0, "residual": 0, "unknown": total,
                      "physical_cards": physical, "chips_per_card": total // physical if physical else 1,
                      "cards": cards},
            "node": {"cpu_pct": _number(node.get("cpu_pct"), maximum=100),
                     "load_per_cpu": _number(node.get("load_per_cpu"), maximum=1000000),
                     "mem_pct": _number(node.get("mem_pct"), maximum=100),
                     "mem_total_mib": memory_total, "mem_avail_mib": memory_available,
                     "uptime_s": _number(node.get("uptime_s"))}}


def _entry(key, value):
    if not isinstance(key, str) or not _HASH.fullmatch(key) or not isinstance(value, dict):
        return None
    project, fingerprint = value.get("project_id"), value.get("fingerprint")
    if (not isinstance(project, str) or not _ID.fullmatch(project)
            or not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint)
            or value.get("source_type") not in ("jumpserver", "ssh")):
        return None
    expected = hashlib.sha256((project + ":" + fingerprint).encode("utf-8")).hexdigest()
    if key != expected:
        return None
    if any(not isinstance(value.get(field), str) or not _ID.fullmatch(value[field]) for field in ("id", "ssh_alias")):
        return None
    row = {"fingerprint": fingerprint, "project_id": project, "source_type": value["source_type"],
           "id": value["id"], "ssh_alias": value["ssh_alias"], "label": _text(value.get("label"), 200),
           "backend": _backend(value.get("backend")), "backend_label": _text(value.get("backend_label")),
           "updated_at": _timestamp(value.get("updated_at"))}
    row["view"] = _view(value.get("view"), row)
    return row


class DeviceTelemetryCache:
    """Atomic JSON display cache; failure is nonfatal and never returns raw data."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self):
        with self._lock:
            try:
                if self.path.is_symlink() or self.path.stat().st_size > MAX_CACHE_BYTES:
                    return {}
                with self.path.open("rb") as stream:
                    raw = stream.read(MAX_CACHE_BYTES + 1)
                if len(raw) > MAX_CACHE_BYTES:
                    return {}
                payload = json.loads(raw)
                if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != _SCHEMA:
                    return {}
                values = payload.get("entries")
                if not isinstance(values, dict):
                    return {}
                result = {}
                for key, value in islice(values.items(), MAX_ENTRIES):
                    entry = _entry(key, value)
                    if entry is not None:
                        result[key] = entry
                return result
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                return {}

    def save(self, entries):
        """Return True for a complete save; False for errors or bounded truncation.

        A truncated write may still contain the accepted subset of history. It
        never preserves availability, and cannot replace fresh collector state.
        """
        if not isinstance(entries, dict):
            return False
        with self._lock:
            temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                pieces, size, complete = [], len(b'{"schema_version":1,"entries":{}}'), len(entries) <= MAX_ENTRIES
                for key, value in islice(entries.items(), MAX_ENTRIES):
                    entry = _entry(key, value)
                    if entry is None:
                        complete = False
                        continue
                    piece = (json.dumps(key) + ":" + json.dumps(entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False)).encode("utf-8")
                    cost = len(piece) + (1 if pieces else 0)
                    if size + cost > MAX_CACHE_BYTES:
                        complete = False
                        break
                    pieces.append(piece)
                    size += cost
                raw = b'{"schema_version":1,"entries":{' + b",".join(pieces) + b'}}'
                if len(raw) > MAX_CACHE_BYTES:
                    return False
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                return complete
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                return False
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
