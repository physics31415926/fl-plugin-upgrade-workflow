"""Dashboard compatibility for complete MetaX SMI query output.

The pinned upstream probe bounded each verbose query at ninety lines. Eight C550
cards already produce more memory-query lines than that. Compact only the fields
we consume, then retain an explicit bound; never silence a query failure.
"""
from __future__ import annotations

import math
import re

MAX_METAX_LINES = 2048
_MARKER = "M=$(command -v mx-smi 2>/dev/null)"
_FIELDS = {
    "m": r"/^GPU#[0-9]+[[:space:]]/ || /^[[:space:]]+vis_vram[[:space:]]+(total|used)[[:space:]]*:/ || /^Attached GPUs[[:space:]]*:/ {print}",
    "u": r"/^GPU#[0-9]+[[:space:]]/ || /^[[:space:]]+GPU[[:space:]]*:/ || /^Attached GPUs[[:space:]]*:/ {print}",
}


def adapt_metax_probe(script: str) -> str:
    """Patch only the vendor's one-line MetaX section, before or after bounding."""
    lines = script.splitlines()
    indices = [i for i, line in enumerate(lines) if _MARKER in line]
    if len(indices) != 1:
        raise RuntimeError("Unexpected MetaX section in pinned telemetry probe")
    index = indices[0]
    line = lines[index]
    for variable in ("m", "u", "p"):
        query = 'printf \'%s\\n\' "$' + variable + '" | '
        matches = [query + bound for bound in ("head -90", "bounded_head 90")
                   if query + bound in line]
        if len(matches) != 1:
            raise RuntimeError("Unexpected MetaX query bound in pinned telemetry probe")
        bound = "bounded_head " if matches[0].endswith("bounded_head 90") else "head -"
        compact = "awk '" + _FIELDS[variable] + "' | " if variable in _FIELDS else ""
        line = line.replace(matches[0], query + compact + bound + str(MAX_METAX_LINES), 1)
    lines[index] = line
    return "\n".join(lines) + ("\n" if script.endswith("\n") else "")


def metax_metrics_complete(view, raw: str) -> bool:
    """Require complete counters for every card and honor SMI's device count."""
    cards = view.cards
    if not cards or len(cards) > 256:
        return False
    ids = [card.index for card in cards]
    if any(type(index) is not int or index < 0 for index in ids) or len(set(ids)) != len(ids):
        return False
    declared = [int(value) for value in re.findall(r"(?m)^Attached GPUs\s*:\s*(\d+)\s*$", raw)]
    if declared and any(value != len(cards) for value in declared):
        return False
    for card in cards:
        values = (card.util, card.mem_used, card.mem_total)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            return False
        try:
            if not all(math.isfinite(value) for value in values):
                return False
        except OverflowError:
            return False
        if not (0 <= card.util <= 100 and 0 <= card.mem_used <= card.mem_total and card.mem_total > 0):
            return False
    return True
