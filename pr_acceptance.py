"""Summarize explicit PR test results without copying untrusted PR prose."""
from __future__ import annotations

import re

TEST_CONTEXT = re.compile(r"(?i)\b(?:tests?|testing|validation|acceptance|verification|gate|e2e|smoke|benchmark|pytest|offline|concurrent|inference|mtp)\b|验收|验证|测试|并发|离线|推理|运行结果")
CI_ONLY = re.compile(r"(?i)^\W*(?:ci|github\s+actions?|workflow|流水线)\b")
PENDING = re.compile(r"(?i)\b(?:not\s+(?:yet\s+)?(?:run|rerun|re-run|tested|executed|validated|verified|covered|completed)|pending|todo|planned|will\s+(?:run|test)|expected\s+to|should\s+(?:pass|succeed)|expected\s+result)\b|未(?:执行|运行|测试|验证|验收|覆盖)|尚未|待(?:执行|运行|测试|验证|验收)|计划(?:执行|运行|测试|验证)|预计")
FAILED = re.compile(r"(?i)\b(?:failed|failure|fail|error)\b|失败|未通过|不通过|❌|✗")
PASSED = re.compile(r"(?i)\b(?:passed|pass|success|successful|ok|completed\s+(?:successfully|in\s+full))\b|通过|成功|✅|✔|✓|\bEXIT\s*:\s*0\b")
PARTIAL = re.compile(r"(?i)\bpartial(?:ly)?\b|部分通过|部分验收")
RATIO = re.compile(r"(?<![\w.])(\d{1,6})\s*/\s*(\d{1,6})(?![\w.])")
ZERO_FAILURES = re.compile(r"(?i)\b0\s+(?:failed|failures?|errors?)\b|\b(?:failed|failures?|errors?)\s*[:=]\s*0\b|(?:失败|错误)\s*[:：=]?\s*0")
NEGATIVE_PASS = re.compile(r"(?i)\b(?:not|never|no)\s+(?:a\s+)?(?:pass(?:ed)?|success(?:ful)?)\b|并非通过|没有通过|不能(?:判定|认定|证明|视为).{0,12}通过")
CI_LABEL = re.compile(r"(?i)\bci\b|github\s+actions?|workflow|流水线")
NON_REQUIRED = re.compile(r"(?i)non[- ]blocking|not\s+counted|diagnostic|non[- ]required|not\s+required|performance[- ]only|非必需|非必测|额外诊断")
RESULT_HEADER = re.compile(r"(?i)^(?:result(?:s)?|status|outcome|结果|状态)(?:\b|\s|$)")
NEGATED_FAILURE = re.compile(r"(?i)\b(?:no|without|zero|nonfatal|non-fatal|not)\s+(?:[\w-]+\s+){0,4}(?:failures?|errors?)\b")
HISTORICAL_FAILURE = re.compile(r"(?i)\b(?:prior|previous|historical)\s+(?:(?!(?:but|however|current|now|succeeded|passed|successful)\b)[\w-]+\s+){0,4}(?:failed|failures?|errors?)\b")


def _without_ci_clauses(line: str) -> str:
    # CI conclusions cannot supply an experimental result, even after a prefix
    # such as 'All'. Preserve separately stated runtime results in mixed lines.
    clauses = re.split(r"[;；]|(?<=[.!?。])\s+", line)
    return "; ".join(clause for clause in clauses if not CI_LABEL.search(clause))


def _row_status(line: str, *, table_result: bool = False) -> str | None:
    # Unchecked test plans and expected output are not execution evidence.
    if re.search(r"^\s*[-*+]\s*\[\s*\]", line):
        return "pending"
    if re.search(r"(?i)\b(?:should\s+(?:pass|succeed)|expected\s+result|expected\s+to\s+(?:pass|succeed))\b", line):
        return "pending"
    clean = HISTORICAL_FAILURE.sub("", NEGATED_FAILURE.sub("", ZERO_FAILURES.sub("", line)))
    if NEGATIVE_PASS.search(clean):
        return "pending"
    failed, passed = bool(FAILED.search(clean)), bool(PASSED.search(clean))
    if PENDING.search(line):
        return "partial" if passed else "pending"
    ratios = [(int(a), int(b)) for a, b in RATIO.findall(clean)]
    valid = [(a, b) for a, b in ratios if b > 0 and 0 <= a <= b]
    attached_ratio = re.search(r"(?i)(?:pass(?:ed)?\s+(?:only\s+)?\d+\s*/\s*\d+|\d+\s*/\s*\d+\s+(?:tests?|requests?|runs?|cases?|checks?|scenarios?)?\s*(?:pass(?:ed)?|successful))", clean)
    result_ratio = table_result and (re.match(r"^\W*\d+\s*/\s*\d+", clean) or re.search(r"(?i)\d+\s*/\s*\d+\s+(?:(?:content|semantic)\s+)?(?:tests?|requests?|runs?|cases?|checks?|scenarios?|outputs?)\b", clean))
    if PARTIAL.search(clean) or (valid and any(a < b for a, b in valid) and (result_ratio or attached_ratio)):
        return "partial"
    if failed:
        return "partial" if passed else "failed"
    if passed or (table_result and valid and all(a == b for a, b in valid)):
        return "passed"
    return None


def parse_pr_description(body) -> dict | None:
    """Return a bounded aggregate of explicit results, never body text or commands.

    Read result lines in validation sections/tables, plus self-contained test
    statements. CI-only badges, fenced commands and commented templates do not
    imply experimental acceptance. Results describe the PR author's stated scope.
    """
    if not isinstance(body, str) or not body.strip():
        return None
    body = re.sub(r"<!--.*?-->", "", body[:262144], flags=re.S)
    headings: list[tuple[int, bool]] = []
    fence = None
    result_index = None
    table_validation = False
    excluded_level = None
    counts = {"passed": 0, "partial": 0, "failed": 0, "pending": 0}
    seen = set()
    for raw in body.splitlines()[:4000]:
        line = raw.strip()
        marker = re.match(r"^(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = (marker[1][0], len(marker[1]))
            elif marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not line[len(marker[1]):].strip():
                fence = None
            continue
        if fence is not None or not line or len(line) > 4000:
            if not line:
                result_index = None
                table_validation = False
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)", line)
        if heading:
            level, title = len(heading[1]), heading[2]
            if excluded_level is not None and level <= excluded_level:
                excluded_level = None
            if CI_ONLY.search(title) or NON_REQUIRED.search(title):
                excluded_level = level
            while headings and headings[-1][0] >= level:
                headings.pop()
            parent = headings[-1][1] if headings else False
            headings.append((level, not CI_ONLY.search(title) and not NON_REQUIRED.search(title) and (bool(TEST_CONTEXT.search(title)) or parent)))
            result_index = None
            table_validation = False
            continue
        if excluded_level is not None:
            continue
        if CI_ONLY.search(line) or NON_REQUIRED.search(line):
            continue
        is_table = line.startswith("|") and line.endswith("|")
        candidate = line
        if is_table:
            cells = [part.strip() for part in re.split(r"(?<!\\)\|", line.strip("|"))]
            header_index = next((i for i, part in enumerate(cells) if RESULT_HEADER.search(part)), None)
            if header_index is not None:
                result_index = header_index
                table_validation = bool(headings and headings[-1][1]) or bool(TEST_CONTEXT.search(" ".join(cells))) or bool(re.search(r"(?i)\b(?:script|scope|case)\b|脚本|用例", " ".join(cells)))
                continue
            if re.fullmatch(r"[| :\-]+", line) or not table_validation or result_index is None or result_index >= len(cells):
                continue
            if CI_LABEL.search(cells[0]):
                continue
            scope_limits = " ".join(cell for i, cell in enumerate(cells)
                                    if i != result_index and PENDING.search(cell))
            candidate = cells[result_index] + " " + scope_limits
            context = True
        else:
            result_index = None
            table_validation = False
            # Free text must itself describe a test, or be a bullet in a test
            # section. Arbitrary CI diagnostics after a result table are ignored.
            context = bool(TEST_CONTEXT.search(line)) or (bool(headings and headings[-1][1]) and bool(re.match(r"^[-*+]\s", line)))
            if "/actions/runs/" in line:
                continue
        if not context:
            continue
        candidate = _without_ci_clauses(candidate)
        status = _row_status(candidate, table_result=is_table)
        if status is None:
            continue
        identity = re.sub(r"\s+", " ", line).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        counts[status] += 1
    if not sum(counts.values()):
        return None
    positive = counts["passed"] + counts["partial"]
    if counts["partial"] or (positive and (counts["failed"] or counts["pending"])):
        status = "partial"
    elif counts["failed"]:
        status = "failed"
    elif counts["passed"]:
        status = "passed"
    else:
        status = "pending"
    parts = [f"{count} 条{label}" for key, label in (("passed", "通过"), ("partial", "部分通过"),
                                                   ("failed", "失败"), ("pending", "未完成"))
             if (count := counts[key])]
    return {"status": status, "source": "pr_description", "title": "PR 验收结果",
            "summary": "PR 描述报告：" + "，".join(parts) + "；测试范围见 PR 明细", "counts": counts}


def acceptance_summary(pr, pr_data, records, gate, *, branch: str, head: str) -> dict:
    """Choose one display result while preserving independent raw Gate evidence."""
    pr = pr if isinstance(pr, dict) else {}
    number, url = pr.get("number"), pr.get("url")
    base = {"pr_number": number, "pr_url": url, "archive_available": False, "archive_url": None}
    data = pr_data if isinstance(pr_data, dict) else {}
    if number is not None and data.get("base", {}).get("ref") == branch and data.get("number") == number:
        description = parse_pr_description(data.get("body"))
        if description:
            # No raw Markdown, headings, commands or other free text survive.
            return {**base, **description}
    eligible = [record for record in records if isinstance(record, dict)
                and record.get("status") in ("passed", "partial", "failed")
                and (number is None or record.get("pr_number") == number)]
    if eligible:
        record = max(eligible, key=lambda item: str(item.get("recorded_at") or ""))
        status = record["status"]
        label = {"passed": "通过", "partial": "部分通过", "failed": "失败"}[status]
        return {**base, "status": status, "source": "local_record", "title": "本地验收记录",
                "summary": f"本地验收记录：{label}；测试版本和范围见明细",
                "pr_number": record.get("pr_number") or number, "pr_url": record.get("pr_url") or url,
                "archive_available": bool(record.get("archive_available")), "archive_url": record.get("archive_url")}
    raw = gate if isinstance(gate, dict) else {}
    status = raw.get("status", "missing")
    if status in ("passed", "recorded_pass") and not raw.get("tree_matches_head") and raw.get("latest_commit") and raw["latest_commit"] != head:
        status = "stale"
    if status in ("passed", "recorded_pass", "partial", "failed"):
        return {**base, "status": "passed" if status == "recorded_pass" else status, "source": "current_gate",
                "title": "本地逐项验收", "summary": f"逐项结果 {int(raw.get('passed') or 0)}/{int(raw.get('total') or 0)}；展开分支详情查看"}
    return {**base, "status": "reference" if number else "missing", "source": "pr_reference" if number else "current_gate",
            "title": "PR 验收说明" if number else "验收结果", "summary": "PR 描述尚无可识别的测试结果；点击查看明细" if number else "暂无验收结果"}
