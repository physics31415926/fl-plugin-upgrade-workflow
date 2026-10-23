"""AMD SMI JSON telemetry, validated against both installed output layouts."""
from __future__ import annotations

import json
import math
import re
import shlex

from fl_dashboard.vendor.jms_top.families import AccelResult, Card, Proc

# Run on the monitored host using its existing Python. Project only consumed
# fields before transport: busy hosts can emit hundreds of KiB of repeated data.
AMD_COMPACTOR = r'''
import json, os, sys
try:
    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError()
    payload = json.loads(raw)
    rows = payload.get('gpu_data') if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not 0 < len(rows) <= 256:
        raise ValueError()
    kind, result, processes = sys.argv[1], [], 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError()
        out = {'gpu': row.get('gpu')}
        if kind == 'static':
            info = row.get('asic', {})
            out['asic'] = {k: info.get(k) for k in ('market_name', 'vendor_id')}
        elif kind == 'metric':
            for key, fields in (('usage', ('gfx_activity',)), ('mem_usage', ('total_vram', 'used_vram')),
                                ('temperature', ('edge',)), ('power', ('socket_power',))):
                info = row.get(key)
                info = info if isinstance(info, dict) else {}
                out[key] = {k: info.get(k) for k in fields}
        elif kind == 'process':
            entries = row.get('process_list')
            if not isinstance(entries, list):
                raise ValueError()
            processes += len(entries)
            if processes > 2048:
                raise ValueError()
            out['process_list'] = []
            for entry in entries:
                info = entry.get('process_info') if isinstance(entry, dict) else None
                if isinstance(info, dict):
                    info = {k: info.get(k) for k in ('pid', 'name', 'mem_usage')}
                    name = info.get('name')
                    if isinstance(name, str):
                        info['name'] = os.path.basename(name.split()[0])[:100] if name.split() else ''
                out['process_list'].append({'process_info': info})
        else:
            raise ValueError()
        result.append(out)
    encoded = json.dumps(result, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode('utf-8')) > 64 * 1024:
        raise ValueError()
    print(encoded)
except (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError):
    sys.exit(1)
'''


def probe_amd() -> str:
    """Only static, metric and process queries; never configure a device."""
    return r'''AMD_SMI=$(command -v amd-smi 2>/dev/null)
[ -n "$AMD_SMI" ] || { [ ! -x /opt/rocm/bin/amd-smi ] || AMD_SMI=/opt/rocm/bin/amd-smi; }
if [ -n "$AMD_SMI" ]; then
  AMD_PY=$(command -v python3 2>/dev/null)
  if [ -z "$AMD_PY" ]; then
    echo '##STALLED:amd:json-helper-missing'
  else
    amd_query() {
      kind=$1; shift
      case "$kind" in
        static) echo '##AMD-STATIC';;
        metric) echo '##AMD-METRIC';;
        process) echo '##AMD-PROCESS';;
      esac
      detail=$(timeout 6s "$AMD_SMI" "$@" --json 2>/dev/null); rc=$?
      case "$rc" in
        0)
          compact=$(printf '%s\n' "$detail" | timeout -k 1s 2s "$AMD_PY" -c COMPACTOR_CODE "$kind" 2>/dev/null); rc=$?
          if [ "$rc" -eq 0 ]; then
            echo "fl-dashboard-amd-$kind-ok"
            printf '%s\n' "$compact"
          else
            echo '##STALLED:amd:invalid-json'
          fi;;
        124|137) echo '##STALLED:amd:tool-timeout';;
        *) echo '##STALLED:amd:query-failed';;
      esac
    }
    amd_query static static --asic
    amd_query metric metric --usage --mem-usage --temperature --power
    amd_query process process --general
    echo '##AMD-END'
  fi
fi
'''.replace('COMPACTOR_CODE', shlex.quote(AMD_COMPACTOR))


def _rows(raw, kind):
    marker = '##AMD-' + kind.upper()
    lines = raw.splitlines()
    starts = [i for i, line in enumerate(lines) if line == marker]
    if len(starts) != 1:
        raise ValueError()
    block = []
    for line in lines[starts[0] + 1:]:
        if line.startswith('##'):
            break
        block.append(line)
    if not block or block.pop(0) != 'fl-dashboard-amd-' + kind + '-ok':
        raise ValueError()
    text = '\n'.join(block)
    if len(text.encode('utf-8')) > 64 * 1024:
        raise ValueError()
    payload = json.loads(text)
    rows = payload.get('gpu_data') if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not 0 < len(rows) <= 256:
        raise ValueError()
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or type(row.get('gpu')) is not int
                or not 0 <= row['gpu'] < 4096 or row['gpu'] in result):
            raise ValueError()
        result[row['gpu']] = row
    return result


def _value(field, units, *, maximum=2**40):
    if not isinstance(field, dict) or field.get('unit') not in units:
        raise ValueError()
    value = field.get('value')
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError()
    return float(value) * units[field['unit']]


def _optional(field, unit, maximum):
    try:
        return _value(field, {unit: 1}, maximum=maximum)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_amd(raw: str):
    """Return (hardware, complete); every incomplete query keeps cards unknown."""
    if not re.search(r'(?m)^##AMD-(?:STATIC|METRIC|PROCESS)$|^##STALLED:amd:', raw):
        return None, True
    result = None
    try:
        static = _rows(raw, 'static')
        cards = []
        for index, row in sorted(static.items()):
            info = row.get('asic')
            if not isinstance(info, dict) or str(info.get('vendor_id')).lower() != '0x1002':
                raise ValueError()
            name = info.get('market_name')
            if not isinstance(name, str) or not name.strip():
                raise ValueError()
            cards.append(Card(index=index, name=name, procs_known=False))
        result = AccelResult(family='amd', label='AMD / 超威半导体', cards=cards)
        metrics = _rows(raw, 'metric')
        if metrics.keys() != static.keys():
            raise ValueError()
        # AMD SMI labels its MiB counters 'MB'; verified against ROCm byte counts.
        memory_units = {'MB': 1, 'MiB': 1, 'B': 1 / 1048576}
        for card in cards:
            row = metrics[card.index]
            card.util = _value(row.get('usage', {}).get('gfx_activity'), {'%': 1}, maximum=100)
            memory = row.get('mem_usage', {})
            used = _value(memory.get('used_vram'), memory_units)
            total = _value(memory.get('total_vram'), memory_units)
            if total <= 0 or used > total:
                raise ValueError()
            card.mem_used, card.mem_total = used, total
            temperature, power = row.get('temperature'), row.get('power')
            card.temp = _optional(temperature.get('edge') if isinstance(temperature, dict) else None, 'C', 200)
            card.power = _optional(power.get('socket_power') if isinstance(power, dict) else None, 'W', 10000)
        processes = _rows(raw, 'process')
        if processes.keys() != static.keys():
            raise ValueError()
        count = 0
        for card in cards:
            entries = processes[card.index].get('process_list')
            if not isinstance(entries, list):
                raise ValueError()
            count += len(entries)
            if count > 2048:
                raise ValueError()
            for entry in entries:
                info = entry.get('process_info') if isinstance(entry, dict) else None
                if info == 'No running processes detected' and len(entries) == 1:
                    continue
                if not isinstance(info, dict) or type(info.get('pid')) is not int or not 0 < info['pid'] <= 2**31:
                    raise ValueError()
                name = info.get('name')
                if not isinstance(name, str):
                    raise ValueError()
                memory = _value(info.get('mem_usage'), memory_units)
                card.procs.append(Proc(pid=info['pid'], user=None, name=name, mem_mib=memory))
        for card in cards:
            card.procs_known = True
        return result, True
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError):
        return result, False