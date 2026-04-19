#!/usr/bin/env bash
# OpenClaw Control Panel — Linux/macOS launcher.
#
# Same flow as start.cmd:
#   1. Verify Node 22+.
#   2. Download cloudflared on first run.
#   3. Start the panel server in the background.
#   4. Start a Cloudflare Quick Tunnel.
#   5. Capture the *.trycloudflare.com URL into state/tunnel.url.
#   6. Print the full panel URL (with token) for the user.
#
# This script is meant to run on the TARGET machine, not on the setup PC.

set -euo pipefail

BUNDLE_ROOT="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${BUNDLE_ROOT}/state"
BIN_DIR="${BUNDLE_ROOT}/bin"
PANEL_LOG="${STATE_DIR}/panel.log"
TUNNEL_LOG="${STATE_DIR}/tunnel.log"
TUNNEL_URL_FILE="${STATE_DIR}/tunnel.url"
TOKEN_FILE="${STATE_DIR}/token.txt"
PORT="${OPENCLAW_PANEL_PORT:-8787}"

mkdir -p "${STATE_DIR}" "${BIN_DIR}"

echo
echo "=== OpenClaw Control Panel ==="
echo "Bundle root: ${BUNDLE_ROOT}"
echo "State dir:   ${STATE_DIR}"
echo "Port:        ${PORT}"
echo

# ---- 1. Check Node ----
if ! command -v node >/dev/null 2>&1; then
  echo "[error] Node.js is not installed."
  echo "Install Node 22 LTS from https://nodejs.org/  then re-run this script."
  exit 1
fi
echo "Node version: $(node --version)"

# ---- 2. Detect platform / arch and download cloudflared ----
detect_cf_asset() {
  local uname_s uname_m
  uname_s="$(uname -s)"
  uname_m="$(uname -m)"
  case "${uname_s}" in
    Linux)
      case "${uname_m}" in
        x86_64) echo "cloudflared-linux-amd64" ;;
        aarch64|arm64) echo "cloudflared-linux-arm64" ;;
        armv7l) echo "cloudflared-linux-arm" ;;
        *) return 1 ;;
      esac ;;
    Darwin)
      case "${uname_m}" in
        x86_64|arm64) echo "cloudflared-darwin-amd64.tgz" ;;
        *) return 1 ;;
      esac ;;
    *) return 1 ;;
  esac
}

CLOUDFLARED="${BIN_DIR}/cloudflared"
if [[ ! -x "${CLOUDFLARED}" ]]; then
  asset="$(detect_cf_asset || true)"
  if [[ -z "${asset}" ]]; then
    echo "[error] unsupported platform: $(uname -sm)"
    exit 1
  fi
  url="https://github.com/cloudflare/cloudflared/releases/latest/download/${asset}"
  echo "Downloading ${url} ..."
  if [[ "${asset}" == *.tgz ]]; then
    tmp="$(mktemp -d)"
    curl -fsSL "${url}" -o "${tmp}/cf.tgz"
    tar -xzf "${tmp}/cf.tgz" -C "${tmp}"
    mv "${tmp}/cloudflared" "${CLOUDFLARED}"
    rm -rf "${tmp}"
  else
    curl -fsSL "${url}" -o "${CLOUDFLARED}"
  fi
  chmod +x "${CLOUDFLARED}"
fi

# ---- 3. Start the panel ----
echo "Starting panel on http://127.0.0.1:${PORT} ..."
nohup node "${BUNDLE_ROOT}/panel/server.mjs" >>"${PANEL_LOG}" 2>&1 &
PANEL_PID=$!
echo "${PANEL_PID}" >"${STATE_DIR}/panel.pid"

# Give the panel a moment to bind the port and emit the token.
sleep 2

# ---- 4. Start cloudflared ----
echo "Starting Cloudflare Quick Tunnel ..."
: >"${TUNNEL_LOG}"
rm -f "${TUNNEL_URL_FILE}"
nohup "${CLOUDFLARED}" tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" \
  >>"${TUNNEL_LOG}" 2>&1 &
TUNNEL_PID=$!
echo "${TUNNEL_PID}" >"${STATE_DIR}/tunnel.pid"

# ---- 5. Capture URL ----
echo "Waiting for tunnel URL ..."
TUNNEL_URL=""
for i in $(seq 1 30); do
  sleep 1
  if grep -Eo "https://[a-z0-9-]+\.trycloudflare\.com" "${TUNNEL_LOG}" >/dev/null 2>&1; then
    TUNNEL_URL="$(grep -Eo "https://[a-z0-9-]+\.trycloudflare\.com" "${TUNNEL_LOG}" | head -n 1)"
    printf "%s\n" "${TUNNEL_URL}" >"${TUNNEL_URL_FILE}"
    break
  fi
done

PANEL_TOKEN=""
if [[ -f "${TOKEN_FILE}" ]]; then
  PANEL_TOKEN="$(tr -d '\r\n' <"${TOKEN_FILE}")"
fi

echo
echo "============================================================"
if [[ -n "${TUNNEL_URL}" ]]; then
  echo " Public panel URL:"
  echo "   ${TUNNEL_URL}/p/${PANEL_TOKEN}/"
else
  echo " Tunnel URL not yet ready (see ${TUNNEL_LOG})."
fi
echo " Local panel URL:"
echo "   http://127.0.0.1:${PORT}/p/${PANEL_TOKEN}/"
echo "============================================================"
echo
echo "Panel PID:  ${PANEL_PID}"
echo "Tunnel PID: ${TUNNEL_PID}"
echo "Logs: ${PANEL_LOG} , ${TUNNEL_LOG}"
echo
echo "Panel is running detached. To stop, run: bash ${BUNDLE_ROOT}/stop.sh"
