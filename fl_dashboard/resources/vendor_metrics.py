"""Measured vendor fixes kept separate from the pinned jms-top source.

Enflame's summary Mem column is capacity, not allocation. T-Head process names
can be blank. New Hygon drivers expose each PID's HCU indices explicitly.
"""
from __future__ import annotations

import json
import math
import re

from fl_dashboard.vendor.jms_top.families import Proc
from fl_dashboard.vendor.jms_top.scan import split_sections

_MAX_BLOCK = 64 * 1024
_MAX_CARDS = 256
_MAX_PROCS = 2048


def probe_vendor_metrics():
    """Optional, bounded read-only details; failure keeps affected cards unknown."""
    return r'''if command -v efsmi >/dev/null 2>&1; then
  echo '##FL-ENFLAME-MEMORY'
  detail=$(timeout 6 efsmi -q -d MEMORY --json-format 2>/dev/null); detail_rc=$?
  if [ "$detail_rc" -eq 0 ]; then
    echo 'fl-dashboard-enflame-memory-ok'
    printf '%s\n' "$detail" | bounded_head 4096
  else
    echo 'fl-dashboard-enflame-memory-unavailable'
  fi
  echo '##FL-VENDOR-END'
fi
H=$(command -v hy-smi 2>/dev/null)
[ -n "$H" ] || { [ -x /opt/hyhal/bin/hy-smi ] && H=/opt/hyhal/bin/hy-smi; }
if [ -n "$H" ]; then
  echo '##FL-HYGON-PIDS'
  detail=$(timeout 6 "$H" --showpids 2>/dev/null); detail_rc=$?
  if [ "$detail_rc" -eq 0 ]; then
    echo 'fl-dashboard-hygon-pids-ok'
    printf '%s\n' "$detail" | bounded_head 4096
  else
    echo 'fl-dashboard-hygon-pids-unavailable'
  fi
  echo '##FL-VENDOR-END'
fi
'''


def vendor_sections(raw, family):
    """Keep evidence within the exact accelerator family and its subsections."""
    prefix = family.upper()
    return "\n".join("##" + name + "\n" + "\n".join(lines)
                     for name, lines in split_sections(raw).items()
                     if name == prefix or name.startswith(prefix + "-"))


def _block(raw, name, proof):
    marker = "##" + name
    lines = raw.splitlines()
    matches = [index for index, line in enumerate(lines) if line.strip() == marker]
    if len(matches) != 1:
        return None
    out = []
    for line in lines[matches[0] + 1:]:
        if line.startswith("##"):
            break
        out.append(line)
    if not out or out[0].strip() != proof:
        return None
    text = "\n".join(out[1:]).strip()
    return text if len(text.encode("utf-8")) <= _MAX_BLOCK else None


def _number(value, *, positive=False):
    return (float(value) if type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0) and value <= 2**40 else None)


def _enflame(view, raw):
    # The measured summary column is total capacity; never show it as used.
    for card in view.cards:
        card.mem_used = None
        card.mem_total = None
    block = _block(raw, "FL-ENFLAME-MEMORY", "fl-dashboard-enflame-memory-ok")
    values = None
    try:
        payload = json.loads(block) if block is not None else None
        rows = payload.get("devices") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not 0 < len(rows) <= _MAX_CARDS:
            raise ValueError()
        values = {}
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] in values:
                raise ValueError()
            info = row.get("Device Mem Info")
            if not isinstance(info, dict):
                raise ValueError()
            total, used = _number(info.get("Total Size"), positive=True), _number(info.get("Used Size"))
            if total is None or used is None or used > total:
                raise ValueError()
            values[row["id"]] = (used, total)
        if set(values) != {card.index for card in view.cards}:
            raise ValueError()
    except (ValueError, TypeError, OverflowError, RecursionError):
        values = None
    for card in view.cards:
        if values is None:
            card.procs_known = False
        else:
            card.mem_used, card.mem_total = values[card.index]


_THEAD_PROCESS = re.compile(
    r"^\|\s*(?P<card>\d+)\s+(?:N/A|\d+)\s+(?:N/A|\d+)\s+"
    r"(?P<pid>\d+)\s+[CG](?:\+[CG])?\s*(?P<name>.*?)\s+(?P<memory>[\d.]+)MiB\s*\|$"
)


def _thead(view, raw):
    # The driver can intentionally withhold process names while still reporting
    # the PID and its allocation. Those rows must still count as occupied.
    by_index = {card.index: card for card in view.cards}
    process_lines = []
    in_table = False
    header = False
    complete = False
    for line in raw.splitlines():
        if line.startswith("##") and in_table:
            break
        if re.match(r"^\|?\s*Processes:", line):
            in_table = True
            continue
        if not in_table:
            continue
        if "PPU" in line and "PID" in line and "Process name" in line:
            header = True
        process = _THEAD_PROCESS.fullmatch(line.strip())
        if process:
            process_lines.append(process)
        elif re.match(r"^\|\s*\d+", line):
            return  # A process row with an unknown layout cannot prove empty cards.
        elif process_lines and re.fullmatch(r"\+[+-]+\+", line.strip()):
            complete = True
    if not header or not complete or len(process_lines) > _MAX_PROCS:
        return
    parsed = {index: [] for index in by_index}
    for row in process_lines:
        index, pid, memory = int(row["card"]), int(row["pid"]), float(row["memory"])
        if index not in parsed or not 0 < pid < 2**32 or not math.isfinite(memory) or memory < 0:
            return
        parsed[index].append(Proc(pid=pid, user=None, name=row["name"].strip() or "未知进程", mem_mib=memory))
    for card in view.cards:
        card.procs = parsed[card.index]
        card.procs_known = True
        card.procs_shared = False


_HCU_LIST = re.compile(r"(?:\[\s*'\d+'(?:\s*,\s*'\d+')*\s*\])?\s*\Z")


def _hygon(view, raw):
    block = _block(raw, "FL-HYGON-PIDS", "fl-dashboard-hygon-pids-ok")
    if block is None or "PIDs for KFD processes:" not in block or "End of SMI Log" not in block:
        return
    by_index = {card.index: card for card in view.cards}
    known = {proc.pid: proc for card in view.cards for proc in card.procs}
    parsed = {index: [] for index in by_index}
    matches = list(re.finditer(r"(?m)^PID:\s*(\d+)\s*$", block))
    # An unrecognised empty/error response must keep the conservative fallback.
    if not matches or len(matches) > _MAX_PROCS:
        return
    seen = set()
    unassigned = []
    for number, match in enumerate(matches):
        pid = int(match[1])
        if pid in seen or not 0 < pid < 2**32:
            return
        seen.add(pid)
        entry = block[match.end():matches[number + 1].start() if number + 1 < len(matches) else len(block)]
        fields = re.findall(r"(?m)^[ \t]*HCU Index:[ \t]*([^\r\n]*)", entry)
        if len(fields) != 1 or not _HCU_LIST.fullmatch(fields[0]):
            return
        indices = [int(index) for index in re.findall(r"'(\d+)'", fields[0])]
        if len(indices) != len(set(indices)) or any(index not in by_index for index in indices):
            return
        source = known.get(pid)
        memory = re.findall(r"(?m)^[ \t]*VRAM USED\(MiB\):[ \t]*([^\r\n]*)", entry)
        try:
            used = _number(float(memory[0])) if len(memory) == 1 else None
        except (ValueError, OverflowError):
            used = None
        proc = Proc(pid=pid, user=source.user if source else None,
                    name=source.name if source else "未知进程", mem_mib=used)
        if not indices:
            # Empty HCU Index can coexist with positive VRAM on real drivers.
            # Only explicit zero allocation establishes that it can be ignored.
            if used != 0:
                unassigned.append(proc)
            continue
        # VRAM USED is per PID across all its cards; do not duplicate that total.
        for index in indices:
            parsed[index].append(Proc(pid=proc.pid, user=proc.user, name=proc.name,
                                      mem_mib=used if len(indices) == 1 else None))
    for card in view.cards:
        card.procs = parsed[card.index]
        card.procs_known = bool(card.procs) or not unassigned
        card.procs_shared = False
    if unassigned and view.cards:
        # Preserve host-wide processes once, without assigning them to a GPU.
        # Prefer an unmapped card as the carrier. If every card is mapped, its
        # known=True still means at least one explicit card-owned PID exists.
        carrier = next((card for card in view.cards if not card.procs), view.cards[0])
        carrier.procs = carrier.procs + unassigned
        carrier.procs_shared = True


def augment_vendor_metrics(view, raw):
    """Call after generic process evidence; complete vendor evidence refines it."""
    if view.family == "enflame":
        _enflame(view, raw)
    elif view.family == "thead":
        _thead(view, vendor_sections(raw, "thead"))
    elif view.family == "hygon":
        _hygon(view, raw)
