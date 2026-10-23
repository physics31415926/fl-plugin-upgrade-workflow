"""Bounded read-only jms-top probes and a deliberately small dashboard projection.

The imported parsers are pinned and attributed in vendor/jms_top/NOTICE. Remote
output is untrusted: command lines, raw replies, and free-form tool errors never
leave this adapter. Missing or incomplete signals never advertise free devices.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re

from fl_dashboard.monitoring.branch_monitor import LABELS
from fl_dashboard.resources.amd import parse_amd, probe_amd
from fl_dashboard.resources.ascend import adapt_ascend_probe
from fl_dashboard.resources.metax import adapt_metax_probe, metax_metrics_complete
from fl_dashboard.resources.vendor_metrics import augment_vendor_metrics, probe_vendor_metrics, vendor_sections
from fl_dashboard.resources.sunrise import probe_sunrise
from fl_dashboard.vendor.jms_top.cpu import CpuSample
from fl_dashboard.vendor.jms_top.families import AccelResult, Card, Proc, _NPU_CARD, _NPU_CHIP, _NPU_PROC
from fl_dashboard.vendor.jms_top.model import HostView
from fl_dashboard.vendor.jms_top.probe import ProbeLimits, build_script, SENTINEL
from fl_dashboard.vendor.jms_top.scan import parse_reply, reached

COMPLETE = "fl-dashboard-probe-complete"
MAX_REPLY_BYTES = 128 * 1024
MAX_CARDS = 256
DEVICE_LABELS = {**LABELS, "rocm": "AMD / 超威半导体"}
FAMILY_BACKENDS = {
    "nvidia": "cuda", "ascend": "ascend", "kunlunxin": "kunlunxin",
    "metax": "metax", "enflame": "enflame", "mthreads": "musa",
    "tsingmicro": "tsingmicro", "thead": "thead", "iluvatar": "corex", "hygon": "hygon",
    "sunrise": "sunrise", "amd": "rocm",
}


def probe_script() -> str:
    """A fixed Linux shell probe: counters only, no installation or job control."""
    body = adapt_metax_probe(build_script(limits=ProbeLimits(top_n=0, tile_n=260, half=90)))
    body = adapt_ascend_probe(body)
    # Upstream emits `T=timeout 6`, which executes a command named `6`.
    # T is a fixed command and integer here; word splitting yields timeout argv.
    body = body.replace("T=timeout 6;", "T='timeout 6';")
    body = body.replace("[ $? -eq 124 ]", "[ $? -ne 0 ]")
    body = body.replace("[ $rc -eq 124 ]", '[ "$rc" -ne 0 ]')
    body = body.replace("T='timeout 6';", "T='timeout 12';", 1)
    # An unsuccessful process query must not masquerade as an empty process list.
    process_query = ("$T nvidia-smi --query-compute-apps=gpu_bus_id,pid,process_name,used_memory "
                     "--format=csv,noheader,nounits 2>/dev/null")
    if body.count(process_query) != 1:
        raise RuntimeError("Unexpected process query in pinned telemetry probe")
    body = body.replace(process_query, 'apps=$(' + process_query + '); prc=$?; '
                        'if [ "$prc" -eq 0 ]; then printf "%s\\n" "$apps"; echo "fl-dashboard-process-ok:nvidia"; '
                        "else echo '##STALLED:nvidia:process-query-failed'; fi")
    # MetaX emits its process proof before choosing the output layout. Give it
    # its own vendor section so proof cannot leak into a previous tool's section.
    metax_start = 'if [ -n "$M" ]; then u='
    if body.count(metax_start) != 1:
        raise RuntimeError("Unexpected MetaX probe header")
    body = body.replace(metax_start, 'if [ -n "$M" ]; then echo "##METAX-PROBE"; u=')
    # Preserve command status before a pipe hides vendor query failures.
    for var, flag in (("u", "--show-usage"), ("m", "--show-memory")):
        query = var + '=$($T "$M" ' + flag + ' 2>/dev/null);'
        body = body.replace(query, query + " [ $? -eq 0 ] || echo '##STALLED:metax:query-failed';")
    body = body.replace('p=$($T "$M" --show-process 2>/dev/null | sed -n \'/Process:/,/End of Log/p\');',
                        'p=$($T "$M" --show-process 2>/dev/null); prc=$?; '
                        '[ "$prc" -eq 0 ] && echo "fl-dashboard-process-ok:metax" '
                        '|| echo "##STALLED:metax:process-query-failed"; '
                        'p=$(printf "%s\\n" "$p" | sed -n \'/Process:/,/End of Log/p\');')
    body = body.replace('$T efsmi pmon -c 1 2>/dev/null | head -90;',
                        'p=$($T efsmi pmon -c 1 2>/dev/null); prc=$?; '
                        '[ "$prc" -eq 0 ] && echo "fl-dashboard-process-ok:enflame" '
                        '|| echo "##STALLED:enflame:process-query-failed"; printf "%s\\n" "$p" | head -90;')
    body = body.replace("echo '##HYGON-PROCS';", "echo '##HYGON-PROCS'; "
                        '[ -r /sys/class/kfd/kfd/proc ] && [ -x /sys/class/kfd/kfd/proc ] '
                        '&& echo "fl-dashboard-process-ok:hygon" || echo "##STALLED:hygon:process-unavailable";')
    # The optional Hygon model-name query adds no availability evidence.
    body = body.replace('echo \'##HYGON-MODEL\'; $T "$H" --showproductname 2>/dev/null | head -4; ', "")
    # A completion footer cannot hide an individually truncated vendor table.
    body = re.sub(r"\bhead -(\d+)\b", r"bounded_head \1", body)
    bounded_head = ("bounded_head() { awk -v cap=\"$1\" 'NR<=cap {print; next} "
                    "{print \"##STALLED:generic:output-truncated\"; exit}'; }\n")
    # Correct idle+iowait columns in the pinned upstream /proc/stat snippet.
    body = body.replace("$8+$9, $4+$5", "$8+$9, $5+$6")
    # Never collect node users, command arguments, or secret-bearing process text.
    body = "\n".join(line for line in body.splitlines()
                     if not line.startswith(("echo '##WHO'", "who ", "echo '##TOP'", "ps axo ")))
    body = body.replace('c=$(tr \'\\0\' \' \' < "/proc/$p/cmdline" 2>/dev/null | cut -c1-64)',
                        'c=$(cat "/proc/$p/comm" 2>/dev/null | cut -c1-64)')
    if "/cmdline" in body or "cmd=" in body:
        raise RuntimeError("Unexpected command-line collection in pinned telemetry probe")
    # The current upstream node snippet places counters before uptime, although
    # its parser expects uptime before counters. Keep its positional protocol.
    lines = body.splitlines()
    cpu_line = next(i for i, line in enumerate(lines) if line.startswith("awk '/^cpu /"))
    uptime_line = next(i for i, line in enumerate(lines) if line.startswith("cut -d. -f1 /proc/uptime"))
    lines[cpu_line], lines[uptime_line] = lines[uptime_line], lines[cpu_line]
    body = bounded_head + "\n".join(lines) + "\n" + probe_vendor_metrics() + probe_sunrise() + probe_amd()
    # A wedged SMI may ignore TERM; stop only our bounded query after one second.
    body = body.replace("timeout 6", "timeout -k 1s 6").replace("timeout 12", "timeout -k 1s 12")
    return (f"echo '{SENTINEL}'\n"
            "command -v timeout >/dev/null 2>&1 || { echo '##STALLED:generic:no-timeout'; "
            f"echo '{COMPLETE}'; exit 0; }}\n"
            "timeout -k 1s 28s bash <<'FL_DASHBOARD_FIXED_PROBE'\nexport LC_ALL=C\n" + body +
            "\nFL_DASHBOARD_FIXED_PROBE\nrc=$?\n"
            "[ \"$rc\" -eq 0 ] || echo '##STALLED:generic:probe-timeout-or-failure'\n"
            f"echo '{COMPLETE}'\n")


def _text(value, limit=100):
    if not isinstance(value, str):
        return ""
    text = "".join(c for c in value if c.isprintable())[:limit]
    # Vendor process-name fields occasionally contain a path or arguments.
    # Model names keep spaces, while `_process_name` below is stricter.
    return re.sub(r"(?i)(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9_]{12,}|github_pat_[a-z0-9_]+)",
                  "[redacted]", text)


def _number(value, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < minimum or maximum is not None and value > maximum:
            return None
    except OverflowError:
        return None
    return value


def _process_name(value):
    text = _text(value, 200).strip()
    # Strip command arguments and directories before returning executable labels.
    text = re.split(r"\s+", text, maxsplit=1)[0].replace("\\", "/").rsplit("/", 1)[-1]
    return text[:64] if re.fullmatch(r"[\w.+-]+", text) else "未知进程"


def _card(card, *, invalidated=False):
    util = _number(card.util, maximum=100)
    used, total = _number(card.mem_used), _number(card.mem_total)
    invalid = ((card.util is not None and util is None)
               or (card.mem_used is not None and used is None)
               or (card.mem_total is not None and (total is None or total == 0))
               or (used is not None and total is not None and used > total))
    if total == 0:
        total = None
    if used is not None and total is not None and used > total:
        used = None
    unmapped_processes = card.procs_shared and not card.procs_known
    safe = Card(index=card.index, name=_text(card.name), util=util,
                mem_used=used, mem_total=total, health=_text(card.health, 40) or None,
                procs=[] if unmapped_processes else card.procs, procs_known=card.procs_known)
    busy = None if invalidated or invalid else safe.busy
    if busy is False and (not card.procs_known or (used is not None and used > 0 and total is None)):
        # In particular efsmi can report positive memory with no capacity. Its
        # zero utilisation does not establish that this allocation is available.
        busy = None
    residual = False if busy is None or unmapped_processes else safe.residual
    reason = None
    if busy is None:
        reason = ("本轮采集不完整，等待有效读数" if invalidated else
                  "设备返回了无效指标" if invalid else
                  "未取得该卡的完整进程信息" if not card.procs_known else
                  "显存或利用率指标不完整")
    procs = [{"pid": p.pid if type(p.pid) is int and 0 < p.pid < 2**32 else None,
              "user": _text(p.user, 64) or None, "name": _process_name(p.name),
              "mem_mib": _number(p.mem_mib)} for p in card.procs[:80]]
    return {"index": card.index, "card": card.card, "name": safe.name,
            "util": util, "mem_used": used, "mem_total": total,
            "temp": _number(card.temp, minimum=-50, maximum=250), "power": _number(card.power, maximum=100000),
            "health": safe.health, "busy": busy, "residual": residual,
            "state": "unknown" if busy is None else "residual" if residual else "busy" if busy else "free",
            "reason": reason,
            "procs": procs, "procs_shared": bool(card.procs_shared)}


def _ascend_owners(view, raw):
    """Use npu-smi's explicit board numbers, including repeated 910C headers."""
    owner = None
    chips = {}
    by_index = {card.index: card for card in view.cards}
    for line in raw.splitlines():
        header, chip = _NPU_CARD.match(line), _NPU_CHIP.match(line)
        if header:
            owner = int(header.group("card"))
        elif chip and owner is not None:
            card = by_index.get(int(chip.group("phy")))
            if card is not None:
                card.card = owner
                card.procs = []
                chips[owner, int(chip.group("chip"))] = card
    for line in raw.splitlines():
        process = _NPU_PROC.match(line)
        if process:
            card = chips.get((int(process.group("card")), int(process.group("chip"))))
            if card is not None:
                card.procs.append(Proc(pid=int(process.group("pid")), user=None,
                                       name=process.group("name"), mem_mib=float(process.group("mem"))))


def _process_evidence(view, raw):
    """The pinned parsers default procs_known=True; verify evidence explicitly."""
    family = view.family
    raw = vendor_sections(raw, family)
    if family == "nvidia":
        known = "fl-dashboard-process-ok:nvidia" in raw
    elif family == "metax":
        known = "fl-dashboard-process-ok:metax" in raw and "Process:" in raw
    elif family == "enflame":
        known = "fl-dashboard-process-ok:enflame" in raw and ("*Dev Pid" in raw or "*Idx #" in raw)
    elif family == "hygon":
        known = "fl-dashboard-process-ok:hygon" in raw and not any(card.procs for card in view.cards)
    elif family == "thead":
        # Its upstream parser has no per-card process parser yet. Only a confirmed
        # empty process table establishes free cards; active metrics remain busy.
        known = "No running processes" in raw
    else:
        known = bool(re.search(r"Processes:|Process id|No running processes", raw))
    for card in view.cards:
        card.procs_known = known


def parse_telemetry(alias: str, raw: str, previous_sample: CpuSample | None = None):
    """Return a safe host projection and the opaque CPU sample for its next poll."""
    complete = isinstance(raw, str) and raw.rstrip().endswith("\n" + COMPLETE)
    valid_size = isinstance(raw, str) and len(raw.encode("utf-8", errors="replace")) <= MAX_REPLY_BYTES
    reachable = valid_size and reached(raw)
    view = HostView(name=alias, reachable=reachable)
    malformed = False
    amd_incomplete = False
    if reachable:
        try:
            parse_reply(view, raw)
            if view.family == "ascend":
                _ascend_owners(view, vendor_sections(raw, "ascend"))
            _process_evidence(view, raw)
            augment_vendor_metrics(view, raw)
            if not view.cards:
                amd, amd_complete = parse_amd(raw)
                if amd is not None:
                    view.accel = amd
                amd_incomplete = not amd_complete
            if not view.cards and re.search(r"(?m)^##SUNRISE$", raw):
                view.accel = AccelResult(family="sunrise", label=LABELS["sunrise"])
        except (ValueError, TypeError, IndexError, OverflowError, AttributeError):
            malformed = True
            view = HostView(name=alias, reachable=True)
    failures = re.findall(r"(?m)^##STALLED(?::([a-z]+):([^\r\n]*))?\s*$", raw) if valid_size else []
    # A leftover NVIDIA executable may fail on a healthy MetaX host. Only the
    # family that actually returned cards, or a whole-probe failure, invalidates it.
    relevant = [(family or "generic", detail) for family, detail in failures
                if not family or family == "generic" or family == view.family or view.family == "generic"]
    stalled = bool(relevant)
    metax_incomplete = view.family == "metax" and not metax_metrics_complete(view, vendor_sections(raw, "metax"))
    invalidated = not complete or malformed or stalled or not valid_size or len(view.cards) > MAX_CARDS or metax_incomplete or amd_incomplete
    cards = [_card(card, invalidated=invalidated) for card in view.cards[:MAX_CARDS]]
    if not invalidated and view.family == "hygon" and any(card.procs_shared for card in view.cards):
        for card in cards:
            if card["busy"] is None:
                card["reason"] = "hy-smi 返回了未归属到具体卡的显存进程"
    total = len(cards)
    free = sum(c["busy"] is False for c in cards)
    busy = sum(c["busy"] is True for c in cards)
    unknown = sum(c["busy"] is None for c in cards)
    residual = sum(c["residual"] for c in cards)
    family = view.family
    backend = FAMILY_BACKENDS.get(family)
    node = {key: _number(getattr(view.node, key), maximum=100 if key in ("cpu_pct", "mem_pct") else None)
            for key in ("cpu_pct", "mem_pct", "mem_total_mib", "mem_avail_mib", "uptime_s")}
    node["load_per_cpu"] = _number(view.node.load_per_cpu)
    sample = view.node.cpu_sample if reachable and not invalidated else None
    if sample is not None:
        node["cpu_pct"] = _number(sample.busy_pct_since(previous_sample), maximum=100)
    error = None
    if not reachable:
        verdict, error = "down", "SSH 未返回有效的设备采集响应"
    elif invalidated:
        if view.family == "sunrise" and any(detail == "tool-timeout" for _, detail in relevant):
            error = "pt_smi 查询超时，请检查曦望驱动与运行时状态"
        elif any("output-truncated" in detail for _, detail in relevant):
            error = "SMI 输出超过采集上限，本轮数据不完整"
        elif amd_incomplete or view.family == "amd" and any(family == "amd" for family, _ in relevant):
            error = ("amd-smi 查询超时，请检查 AMD 驱动与运行时状态"
                     if any(family == "amd" and detail == "tool-timeout" for family, detail in relevant) else
                     "amd-smi 未返回完整的逐卡指标或进程信息，请检查工具与驱动状态")
        elif metax_incomplete:
            error = "mx-smi 返回的卡数或指标不完整"
        elif view.family == "ascend" and any(family == "ascend" for family, _ in relevant):
            details = {detail for family, detail in relevant if family == "ascend"}
            error = ("npu-smi 初始化失败，请检查采集账号的驱动访问权限和昇腾驱动状态"
                     if "initialization-failed" in details else
                     "npu-smi 查询超时，请检查昇腾驱动与运行时状态"
                     if "tool-timeout" in details else
                     "npu-smi info 查询失败，请检查采集账号的驱动访问权限和昇腾驱动状态")
        elif stalled:
            error = "厂商 SMI 查询失败或超时，本轮可用性待确认"
        elif malformed:
            error = "SMI 输出格式无法完整解析"
        else:
            error = "采集输出不完整，本轮可用性待确认"
        verdict = "unknown"
    elif not total:
        verdict, error = "unknown", ("pt_smi 未返回可解析的设备指标，请检查工具版本与驱动"
                                    if view.family == "sunrise" else
                                    "未读取到加速器指标，请检查厂商 SMI 工具及驱动")
    elif unknown == total:
        verdict, error = "unknown", ("efsmi 未返回完整的显存或进程指标" if family == "enflame" else
                                    "未取得完整的逐卡进程或资源指标，无法确认空闲容量")
    elif free == total:
        verdict = "free" if node["load_per_cpu"] is not None and node["load_per_cpu"] < 0.75 else "partial"
    elif free:
        verdict = "partial"
    elif residual == total:
        verdict = "residual"
    else:
        verdict = "busy"
    physical = len({c["card"] for c in cards if c["card"] is not None}) or total
    return {"host": _text(alias, 120), "reachable": reachable, "verdict": verdict,
            "error": error, "collection_complete": bool(reachable and not invalidated),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "backend": backend, "backend_label": DEVICE_LABELS.get(backend, "无法识别"), "supported": backend is not None and family != "sunrise",
            "accel": {"family": family, "label": DEVICE_LABELS.get(backend, "无法识别"),
                      "card_name": _text(view.card_name), "total": total, "free": free, "busy": busy,
                      "unknown": unknown, "residual": residual, "physical_cards": physical,
                      "chips_per_card": total // physical if physical else 1, "cards": cards}, "node": node}, sample
