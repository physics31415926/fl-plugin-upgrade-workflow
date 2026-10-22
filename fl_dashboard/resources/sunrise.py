"""Bounded discovery of Sunrise's installed pt_smi utility.

The current live host times out before returning metrics. Keep that distinct
from an unsupported vendor or a healthy empty machine; no metrics are invented.
"""

def probe_sunrise():
    return r'''P=$(command -v pt_smi 2>/dev/null)
[ -n "$P" ] || { [ -x /usr/local/tangrt/bin/pt_smi ] && P=/usr/local/tangrt/bin/pt_smi; }
if [ -n "$P" ]; then
  echo '##SUNRISE'
  detail=$(timeout -k 1s 6s "$P" 2>/dev/null); detail_rc=$?
  if [ "$detail_rc" -eq 0 ]; then
    echo 'fl-dashboard-sunrise-output'
    printf '%s\n' "$detail" | bounded_head 1024
  elif [ "$detail_rc" -eq 124 ] || [ "$detail_rc" -eq 137 ]; then
    echo '##STALLED:sunrise:tool-timeout'
  else
    echo '##STALLED:sunrise:query-failed'
  fi
  echo '##FL-VENDOR-END'
fi
'''
