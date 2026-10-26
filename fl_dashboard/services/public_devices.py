"""Read-only projection of cached device telemetry for ordinary LAN visitors.

The public server never constructs a fleet or accepts a caller-selected upstream.
Only a fixed GET against the configured loopback controller is permitted.
"""
from __future__ import annotations

from http.client import HTTPConnection
import json
import math
import re
from urllib.parse import urlencode, urlsplit

from fl_dashboard.services.share_gateway import validate_control_url

MAX_RESPONSE = 16 * 1024 * 1024
_HOST = {"id", "label", "ssh_alias", "backend", "backend_label", "reachable", "verdict",
         "error", "collection_complete", "updated_at", "age_seconds", "stale", "refreshing", "supported"}
_ACCEL = {"family", "label", "card_name", "total", "free", "busy", "unknown", "residual",
          "physical_cards", "chips_per_card"}
_CARD = {"index", "card", "name", "util", "mem_used", "mem_total", "temp", "power", "health",
         "busy", "residual", "state", "reason", "procs_shared"}
_PROCESS = {"pid", "user", "name", "mem_mib"}
_NODE = {"cpu_pct", "mem_pct", "mem_total_mib", "mem_avail_mib", "uptime_s", "load_per_cpu"}
_SUMMARY = {"hosts", "online", "free_hosts", "total_cards", "free_cards", "busy_cards", "unknown_cards", "residual_cards"}
_SECRET = re.compile(r"(?i)(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9_]{12,}|github_pat_[a-z0-9_]+)")


def _scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if type(value) in (int, float):
        try:
            return value if math.isfinite(value) and abs(value) <= 2**53 else None
        except OverflowError:
            return None
    if isinstance(value, str):
        return _SECRET.sub("[redacted]", "".join(c for c in value[:1600] if c.isprintable())[:400])
    return None


def _fields(value, allowed):
    return {key: _scalar(value[key]) for key in allowed if key in value} if isinstance(value, dict) else {}


def _rows(value, limit):
    return [row for row in value[:limit] if isinstance(row, dict)] if isinstance(value, list) else []


def public_snapshot(value, project):
    if not isinstance(value, dict) or value.get("project_id") != project or not isinstance(value.get("hosts"), list):
        raise ValueError("Invalid cached device response")
    result = _fields(value, {"project_id", "refreshing", "updated_at", "refresh_interval", "error"})
    result.update(read_only=True, progress=_fields(value.get("progress"), {"done", "total"}),
                  concurrency=_fields(value.get("concurrency"), {"limit", "running", "queued"}),
                  summary=_fields(value.get("summary"), _SUMMARY), hosts=[])
    for host in _rows(value["hosts"], 512):
        row = _fields(host, _HOST)
        row["node"] = _fields(host.get("node"), _NODE)
        accel = host.get("accel") if isinstance(host.get("accel"), dict) else {}
        row["accel"] = _fields(accel, _ACCEL)
        row["accel"]["cards"] = []
        for card in _rows(accel.get("cards"), 256):
            item = _fields(card, _CARD)
            item["procs"] = [_fields(proc, _PROCESS) for proc in _rows(card.get("procs"), 80)]
            row["accel"]["cards"].append(item)
        result["hosts"].append(row)
    return result


def read_public_devices(control_url, project):
    control = urlsplit(validate_control_url(control_url))
    if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", project):
        raise ValueError("Invalid device project")
    connection = HTTPConnection(control.hostname, control.port, timeout=8)
    try:
        connection.request("GET", "/api/devices?" + urlencode({"project": project}),
                           headers={"Host": control.netloc, "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if response.status != 200 or len(raw) > MAX_RESPONSE:
            raise ValueError("Cached device data is unavailable")
        return public_snapshot(json.loads(raw), project)
    finally:
        connection.close()
