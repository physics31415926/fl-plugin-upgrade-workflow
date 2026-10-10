"""Read-only experiment records, kept separate from current-HEAD Gate results."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _read(path: Path, root: Path, limit: int) -> bytes | None:
    if not _inside(path, root):
        return None
    try:
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        return data if len(data) <= limit else None
    except OSError:
        return None


def _json(path: Path, root: Path) -> dict | None:
    data = _read(path, root, 128 * 1024)
    try:
        value = json.loads(data.decode('utf-8-sig')) if data is not None else None
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError):
        return None


def _text(value, limit=800):
    return value if isinstance(value, str) and value and len(value) <= limit and '\x00' not in value else None


def _valid_pr_number(value) -> int | None:
    return value if type(value) is int and 1 <= value <= 10_000_000 else None


def _source_pr_numbers(sources, github_repo: str) -> list[int]:
    if not github_repo:
        return []
    values = sources if isinstance(sources, list) else [sources]
    numbers = []
    for source in values:
        url = source.get('url') if isinstance(source, dict) else source
        if not isinstance(url, str) or any(ord(char) < 32 for char in url):
            continue
        try:
            parsed = urlsplit(url.strip())
        except ValueError:
            continue
        # Canonical HTTPS GitHub identities only; reject other repositories,
        # lookalike hosts, userinfo, and ports before deriving a PR number.
        if parsed.scheme.lower() != 'https' or parsed.netloc.lower() != 'github.com':
            continue
        match = re.fullmatch(r'/' + re.escape(github_repo) + r'/pull/([1-9][0-9]*)/?', parsed.path, re.I)
        number = _valid_pr_number(int(match.group(1))) if match and len(match.group(1)) <= 8 else None
        if number is not None and number not in numbers:
            numbers.append(number)
    return numbers


def _legacy_pr_number(record: dict, pr: dict | None, github_repo: str) -> int | None:
    explicit = _valid_pr_number(record.get('pr_number'))
    if explicit is not None:
        return explicit
    numbers = _source_pr_numbers(record.get('sources'), github_repo)
    selected = _valid_pr_number((pr or {}).get('number'))
    # A selected PR may identify a multi-source record only when it really
    # occurs in that archive's sources. Unknown selections never rename it.
    return selected if selected in numbers else (numbers[0] if numbers else None)


class AcceptanceRecords:
    def __init__(self, evidence_dir: Path, engine: str, github_repo: str):
        self.evidence_dir = Path(evidence_dir)
        self.engine = engine
        self.github_repo = github_repo

    def entries(self, backend: str, branch: str, pr: dict | None = None) -> list[dict]:
        if not isinstance(backend, str) or not IDENTIFIER.fullmatch(backend):
            return []
        root = self.evidence_dir / backend
        record_root = root / 'records'
        if not _inside(root, self.evidence_dir) or not _inside(record_root, root):
            return []
        records = []
        for path in sorted(record_root.glob('*.json'))[:50]:
            value = _json(path, record_root)
            if (not value or value.get('schema_version') != 1 or value.get('engine') != self.engine
                    or value.get('backend') != backend or value.get('branch') != branch
                    or value.get('id') != path.stem or not IDENTIFIER.fullmatch(path.stem)
                    or value.get('status') not in ('passed', 'partial', 'failed', 'reference')):
                continue
            number = value.get('pr_number')
            if type(number) is not int or not 1 <= number <= 10_000_000 or not self.github_repo:
                continue
            entry = {'id': value['id'], 'status': value['status'], 'pr_number': number,
                     'pr_url': f'https://github.com/{self.github_repo}/pull/{number}',
                     'title': _text(value.get('title'), 160) or f'PR #{number} 实验记录',
                     'summary': _text(value.get('summary')), 'scope': _text(value.get('scope')),
                     'recorded_at': _text(value.get('recorded_at'), 64),
                     'tested_commit': value.get('tested_commit') if isinstance(value.get('tested_commit'), str) and SHA.fullmatch(value['tested_commit']) else None,
                     'tested_tree': value.get('tested_tree') if isinstance(value.get('tested_tree'), str) and SHA.fullmatch(value['tested_tree']) else None,
                     'source': 'experiment_record', 'archive_available': False}
            archive = value.get('archive')
            if isinstance(archive, dict) and isinstance(archive.get('file'), str) and isinstance(archive.get('sha256'), str) and SHA256.fullmatch(archive['sha256']):
                archive_path = record_root / archive['file']
                raw = _read(archive_path, record_root, MAX_ARCHIVE_BYTES)
                if raw is not None and hashlib.sha256(raw).hexdigest() == archive['sha256']:
                    try:
                        raw.decode('utf-8-sig')
                    except UnicodeError:
                        pass
                    else:
                        entry.update(archive_available=True, _archive_path=archive_path,
                                     _archive_sha256=archive['sha256'], _archive_root=record_root)
            records.append(entry)
        legacy = _json(root / 'acceptance.json', root)
        if (legacy and legacy.get('schema_version') == 1 and legacy.get('evidence_type') == 'audited_record'
                and legacy.get('engine', 'vllm') == self.engine
                and legacy.get('backend') == backend and legacy.get('branch') == branch and legacy.get('sources')
                and isinstance(legacy.get('gate'), dict) and legacy['gate'].get('status') == 'passed'):
            gate = legacy['gate']
            number = _legacy_pr_number(legacy, pr, self.github_repo)
            raw = _read(root / 'acceptance.json', root, MAX_ARCHIVE_BYTES)
            records.append({'id': 'historical-acceptance', 'status': 'passed',
                'pr_number': number, 'pr_url': f'https://github.com/{self.github_repo}/pull/{number}' if type(number) is int and self.github_repo else None,
                'title': '历史完整验收存档', 'summary': f"{gate.get('scenarios_passed')}/{gate.get('scenarios_total')} 场景 · {gate.get('requests_total')} 请求",
                'scope': '存档中记录的模型、模式与场景；当前 Gate 仍独立核验',
                'recorded_at': legacy.get('recorded_at') or legacy.get('verified_at'), 'tested_commit': legacy.get('tested_commit'),
                'tested_tree': legacy.get('tree'), 'source': 'audited_record', 'archive_available': raw is not None,
                '_archive_path': root / 'acceptance.json', '_archive_root': root,
                '_archive_sha256': hashlib.sha256(raw).hexdigest() if raw is not None else None})
        if not records and pr and type(pr.get('number')) is int and self.github_repo:
            number = pr['number']
            records.append({'id': f'pr-{number}', 'status': 'reference', 'pr_number': number,
                'pr_url': f'https://github.com/{self.github_repo}/pull/{number}', 'title': 'PR 测试说明',
                'summary': '暂无本地验收存档，可查看 PR 中的实验说明', 'scope': None,
                'source': 'pr_reference', 'archive_available': False, 'tested_commit': None, 'tested_tree': None})
        return records

    def read_archive(self, backend: str, branch: str, record_id: str) -> dict:
        if not isinstance(record_id, str) or not IDENTIFIER.fullmatch(record_id):
            raise ValueError('Invalid acceptance record ID')
        entry = next((item for item in self.entries(backend, branch) if item['id'] == record_id), None)
        if not entry or not entry.get('archive_available'):
            raise ValueError('Acceptance archive is unavailable; use the PR reference')
        raw = _read(entry['_archive_path'], entry['_archive_root'], MAX_ARCHIVE_BYTES)
        if raw is None or hashlib.sha256(raw).hexdigest() != entry['_archive_sha256']:
            raise ValueError('Acceptance archive changed or is unavailable')
        return {'filename': entry['_archive_path'].name, 'content': raw.decode('utf-8-sig')}

    @staticmethod
    def public(entry: dict) -> dict:
        return {key: value for key, value in entry.items() if not key.startswith('_')}
