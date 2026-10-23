"""Use Ascend's read-only npu-smi query with bounded, sanitized diagnostics."""
from __future__ import annotations

import re


def adapt_ascend_probe(script: str) -> str:
    """Replace only the pinned Ascend probe; preserve its output line limit."""
    lines = script.splitlines()
    indices = [i for i, line in enumerate(lines) if "if command -v npu-smi " in line]
    if len(indices) != 1:
        raise RuntimeError("Unexpected Ascend section in pinned telemetry probe")
    index = indices[0]
    bound = re.search(r"\bhead -(\d+)\b", lines[index])
    if bound is None:
        raise RuntimeError("Unexpected Ascend output bound")
    # Non-login SSH shells may omit these installation directories from PATH.
    # Keep stderr remote: only fixed failure markers leave an unsuccessful query.
    lines[index] = (
        'A=$(command -v npu-smi 2>/dev/null); '
        '[ -n "$A" ] || { for p in /usr/local/bin/npu-smi /usr/local/sbin/npu-smi; '
        'do [ ! -x "$p" ] || { A=$p; break; }; done; }; '
        'if [ -n "$A" ]; then echo "##ASCEND"; '
        'out=$(timeout 6s "$A" info 2>&1); rc=$?; '
        'case "$rc" in '
        '0) printf "%s\\n" "$out" | head -' + bound[1] + ';; '
        '124|137) echo "##STALLED:ascend:tool-timeout";; '
        '*) case "$out" in '
        '*"dcmi module initialize failed"*|*"DrvMngGetConsoleLogLevel failed"*) '
        'echo "##STALLED:ascend:initialization-failed";; '
        '*) echo "##STALLED:ascend:query-failed";; esac;; esac; fi'
    )
    return "\n".join(lines) + ("\n" if script.endswith("\n") else "")
