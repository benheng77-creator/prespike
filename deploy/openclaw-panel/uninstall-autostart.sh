#!/usr/bin/env bash
set -euo pipefail

uname_s="$(uname -s)"

case "${uname_s}" in
  Linux)
    UNIT_FILE="${HOME}/.config/systemd/user/openclaw-panel.service"
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now openclaw-panel.service 2>/dev/null || true
    fi
    rm -f "${UNIT_FILE}"
    echo "Removed ${UNIT_FILE}"
    ;;
  Darwin)
    PLIST_FILE="${HOME}/Library/LaunchAgents/com.openclaw.panel.plist"
    launchctl unload "${PLIST_FILE}" 2>/dev/null || true
    rm -f "${PLIST_FILE}"
    echo "Removed ${PLIST_FILE}"
    ;;
  *)
    echo "[error] unsupported OS: ${uname_s}"
    exit 1
    ;;
esac
