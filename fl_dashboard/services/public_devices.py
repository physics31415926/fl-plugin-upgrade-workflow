"""Public device views and bounded refreshes of the configured controller.

Only fixed resource routes are forwarded. Responses always retain the public
projection; refreshing telemetry never grants task or configuration access.
"""
from __future__ import annotations

from http.client import HTTPConnection
import json
import math
import re
import threading
import time
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
    result.update(read_only=True, device_refresh=True, progress=_fields(value.get("progress"), {"done", "total"}),
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


def _validate_project(project):
    if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", project):
        raise ValueError("Invalid device project")


def _request_devices(control_url, project, *, refresh=False):
    control_url = validate_control_url(control_url)
    control = urlsplit(control_url)
    _validate_project(project)
    connection = HTTPConnection(control.hostname, control.port, timeout=8)
    try:
        headers = {"Host": control.netloc, "Connection": "close"}
        route = "/api/devices"
        if refresh:
            route += "/refresh"
            headers.update({"Origin": control_url, "Content-Type": "application/json",
                            "Sec-Fetch-Site": "same-origin"})
        connection.request("POST" if refresh else "GET", route + "?" + urlencode({"project": project}),
                           body=b"{}" if refresh else None, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if response.status not in ((200, 202) if refresh else (200,)) or len(raw) > MAX_RESPONSE:
            raise ValueError("Invalid device response")
        return public_snapshot(json.loads(raw), project)
    except Exception:
        # Neither upstream error bodies nor connection diagnostics are public.
        raise ValueError("Device resource refresh is unavailable" if refresh else
                         "Cached device data is unavailable") from None
    finally:
        connection.close()


def read_public_devices(control_url, project):
    return _request_devices(control_url, project)


class PublicDeviceRefresher:
    """One global refresh gate per public server, shared by all projects/users."""

    def __init__(self, control_url, *, cooldown=30, clock=time.monotonic):
        self.control_url = validate_control_url(control_url)
        if (type(cooldown) not in (int, float) or not math.isfinite(cooldown)
                or not 1 <= cooldown <= 300):
            raise ValueError("Public refresh cooldown must be between 1 and 300 seconds")
        self.cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._busy = False
        self._next_refresh = float("-inf")

    def retry_after(self):
        with self._lock:
            remaining = math.ceil(max(0, self._next_refresh - self._clock()))
            return max(remaining, 1 if self._busy else 0)

    def _project(self, value):
        # Do not accept cooldown or capabilities supplied by the upstream.
        return {**value, "read_only": True, "device_refresh": True,
                "public_refresh_retry_after": self.retry_after()}

    def snapshot(self, project):
        return self._project(read_public_devices(self.control_url, project))

    def refresh(self, project):
        _validate_project(project)
        with self._lock:
            now = self._clock()
            claimed = not self._busy and now >= self._next_refresh
            if claimed:
                self._busy = True
                self._next_refresh = now + self.cooldown
        # No network operation holds the claim lock: other visitors may keep
        # reading cached progress while the sole request is in flight.
        if not claimed:
            return self.snapshot(project)
        try:
            current = read_public_devices(self.control_url, project)
            value = current if current.get("refreshing") is True else _request_devices(
                self.control_url, project, refresh=True)
        except Exception:
            raise ValueError("Device resource refresh is unavailable") from None
        finally:
            # Retain cooldown after any failure; repeated clicks must not turn
            # a controller outage into repeated SSH or upstream request bursts.
            with self._lock:
                self._busy = False
        return self._project(value)
