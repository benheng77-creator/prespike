#!/usr/bin/env bash
# ============================================================
# Install OpenClaw Control Panel as a user-level auto-start
# service on the TARGET PC.
#
#   Linux  -> systemd --user unit ~/.config/systemd/user/openclaw-panel.service
#   macOS  -> launchd plist     ~/Library/LaunchAgents/com.openclaw.panel.plist
#
# Run this ONCE on the target machine. Run uninstall-autostart.sh to remove.
# ============================================================
set -euo pipefail

BUNDLE_ROOT="$(cd "$(dirname "$0")" && pwd)"
START_SH="${BUNDLE_ROOT}/start.sh"

if [[ ! -x "${START_SH}" ]]; then
  chmod +x "${START_SH}" 2>/dev/null || true
fi
if [[ ! -f "${START_SH}" ]]; then
  echo "[error] start.sh not found at ${START_SH}"
  exit 1
fi

uname_s="$(uname -s)"

case "${uname_s}" in
  Linux)
    UNIT_DIR="${HOME}/.config/systemd/user"
    UNIT_FILE="${UNIT_DIR}/openclaw-panel.service"
    mkdir -p "${UNIT_DIR}"
    cat >"${UNIT_FILE}" <<UNIT
[Unit]
Description=OpenClaw Control Panel
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${BUNDLE_ROOT}
ExecStart=/usr/bin/env bash ${START_SH}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
    echo "Wrote ${UNIT_FILE}"
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user daemon-reload
      systemctl --user enable --now openclaw-panel.service
      echo "Enabled and started. Status: systemctl --user status openclaw-panel"
    else
      echo "[warn] systemctl not found. Unit file written but not enabled."
    fi
    ;;

  Darwin)
    PLIST_DIR="${HOME}/Library/LaunchAgents"
    PLIST_FILE="${PLIST_DIR}/com.openclaw.panel.plist"
    mkdir -p "${PLIST_DIR}"
    cat >"${PLIST_FILE}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>com.openclaw.panel</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>${START_SH}</string>
    </array>
    <key>WorkingDirectory</key><string>${BUNDLE_ROOT}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>${BUNDLE_ROOT}/state/launchd.out</string>
    <key>StandardErrorPath</key><string>${BUNDLE_ROOT}/state/launchd.err</string>
  </dict>
</plist>
PLIST
    echo "Wrote ${PLIST_FILE}"
    launchctl unload "${PLIST_FILE}" 2>/dev/null || true
    launchctl load -w "${PLIST_FILE}"
    echo "Loaded. Status: launchctl list | grep openclaw"
    ;;

  *)
    echo "[error] unsupported OS: ${uname_s}"
    exit 1
    ;;
esac

echo "Installed."
