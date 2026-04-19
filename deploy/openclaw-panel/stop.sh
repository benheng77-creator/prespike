#!/usr/bin/env bash
# Stop the panel and the cloudflared tunnel.
set -euo pipefail

BUNDLE_ROOT="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${BUNDLE_ROOT}/state"

stop_pid_file() {
  local pidfile="$1"
  local label="$2"
  if [[ -f "${pidfile}" ]]; then
    local pid
    pid="$(tr -d '[:space:]' <"${pidfile}")"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      echo "Stopping ${label} (pid ${pid}) ..."
      kill "${pid}" 2>/dev/null || true
      sleep 0.5
      kill -9 "${pid}" 2>/dev/null || true
    fi
    rm -f "${pidfile}"
  fi
}

stop_pid_file "${STATE_DIR}/tunnel.pid" "cloudflared"
stop_pid_file "${STATE_DIR}/panel.pid" "panel"
echo "Done."
