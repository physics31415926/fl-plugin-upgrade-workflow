"""Conservative device hints; SSH names never establish actual hardware."""
from __future__ import annotations

import re

ALIAS_BACKEND_SOURCE = "SSH 名称推断（待硬件核验）"

# Hardware identifiers are shared by the task probe and SSH-name grouping.
HARDWARE_HINTS = {
    "cuda": r"\b(?:cuda|nvidia|a100|h100|h200|h20|a800|h800|l40s|geforce|tesla|rtx)\b",
    "ascend": r"\b(?:ascend|huawei|atlas|cann|910b|910c)\b|昇腾",
    "metax": r"\b(?:metax|maca|mc550|c550)\b|沐曦",
    "corex": r"\b(?:iluvatar|corex|bi100|bi150)\b|天数",
    "hygon": r"\b(?:hygon|dcu)\b|海光",
    "musa": r"\b(?:musa|moorethreads|mthreads|mtt)\b|摩尔线程",
    "enflame": r"\b(?:enflame|topsrider|gcu)\b|燧原",
    "tsingmicro": r"\btsingmicro\b|清微",
    "sunrise": r"\bsunrise\b|曦望",
    "kunlunxin": r"\b(?:kunlunxin|kunlun)\b|昆仑芯",
    "thead": r"\b(?:thead|t-head|hanguang)\b|平头哥|含光",
}
ALIAS_HINTS = {
    **HARDWARE_HINTS,
    "corex": HARDWARE_HINTS["corex"] + r"|\btianshu\b",
    "hygon": HARDWARE_HINTS["hygon"] + r"|\bhy\b",
    "musa": HARDWARE_HINTS["musa"] + r"|\bmoore[ .-]+threads\b",
    "enflame": HARDWARE_HINTS["enflame"] + r"|\bsuiyuan\b",
    "sunrise": HARDWARE_HINTS["sunrise"] + r"|\bxiwang\b",
    "thead": HARDWARE_HINTS["thead"] + r"|\b810e\b",
}


def _recognize(value, hints):
    text = str(value or "").casefold().replace("_", " ")
    matches = {backend for backend, pattern in hints.items() if re.search(pattern, text)}
    return next(iter(matches)) if len(matches) == 1 else None


def hardware_backend(value):
    return _recognize(value, HARDWARE_HINTS)


def alias_backend(value):
    return _recognize(value, ALIAS_HINTS)
