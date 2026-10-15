"""Conservative backend adaptation discovery from PR purpose."""
import re

ALIASES = {
    "cuda": ("cuda", "nvidia"), "ascend": ("ascend",),
    "metax": ("metax",), "corex": ("corex", "iluvatar"),
    "hygon": ("hygon",), "musa": ("musa", "moore threads", "mthreads"),
    "thead": ("thead", "t-head"), "enflame": ("enflame",),
    "kunlunxin": ("kunlunxin",), "tsingmicro": ("tsingmicro", "清微"),
}
PURPOSE = {"summary", "description", "overview", "purpose", "摘要", "描述", "概述", "适配说明"}

def _backends(text):
    return {backend for backend, aliases in ALIASES.items()
            if any(re.search(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])", text, re.I)
                   for alias in aliases)}

def _purpose(body):
    body = re.sub(r"<!--.*?-->", "", str(body or "")[:65536], flags=re.S)
    active, fence = True, None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith((chr(96)*3, "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*$", stripped)
        if heading:
            active = heading[1].strip().casefold() in PURPOSE
            continue
        if active and not stripped.startswith((">", "|", "- [", "* [")):
            yield stripped.replace(chr(96), "").replace("**", "")

def adaptation_backend(pr):
    """Source branch names alone never prove adaptation intent."""
    title = str(pr.get("title") or "")[:1000].casefold()
    targets = set()
    scoped = re.match(r"^(?:adapt|upgrade|migrate|port)\(([^)]+)\)!?:", title)
    feature = re.match(r"^feat\(([^)]+)\)!?:\s*(.+)", title)
    if feature and re.search(r"\b(?:upgrade|adaptation|adapt|support)\b", feature[2]) and re.search(r"\b(?:sglang|vllm)\s+\d", feature[2]):
        targets.update(_backends(feature[1]))
    if scoped:
        targets.update(_backends(scoped[1]))
    for line in _purpose(pr.get("body")):
        for sentence in re.split(r"(?<=[.!?。])\s+", line):
            statement = sentence.strip(" -*").casefold()
            if re.match(r"^(?:(?:this pr|this change)\s+)?(?:adapt|adapts|upgrade|upgrades|migrate|migrates|port|ports)\s+(?:the\s+)?(?:vllm(?:-plugin-fl)?|sglang(?:-plugin-fl)?|plugin|backend)\b", statement):
                targets.update(_backends(statement))
            elif re.match(r"^(?:(?:this pr|this change)\s+)?(?:upgrade|upgrades|adapt|adapts)\s+(?:the\s+)?", statement) and re.search(r"\b(?:backend|runtime|target)\s+(?:from|to)\s+(?:sglang|vllm)\s+\d", statement):
                targets.update(_backends(statement.split(",")[0]))
            elif re.match(r"^(?:本pr|本次|此pr)?(?:将)?(?:vllm(?:-plugin-fl)?|sglang(?:-plugin-fl)?|插件).*(?:适配|升级|迁移)", statement):
                targets.update(_backends(statement))
    if len(targets) != 1:
        return None
    target = next(iter(targets))
    head = pr.get("head") or {}
    match = re.match(r"^(?:codex/backend-|adapt/)([a-z0-9]+)-", str(head.get("ref", "")))
    if match and _backends(match[1]) != {target}:
        return None
    return target
