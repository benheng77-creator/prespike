#!/bin/bash
set -euo pipefail
ROOT="/Users/Admin/LIVE-projects/prespike/openclaw_v1"
PY="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
BASE="/Users/Admin/LIVE-projects/prespike/openclaw_v1/ops/pre_spike_24x7"
LOG="$BASE/logs/governor_loop.log"
cd "$ROOT"
while true; do
  {
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) GOVERNOR TICK ==="
    "$PY" "$BASE/governor.py" --once
  } >> "$LOG" 2>&1 || true
  sleep 60
done
