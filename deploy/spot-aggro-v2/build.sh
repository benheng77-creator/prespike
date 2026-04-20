#!/bin/bash
# Build public dashboard for spot-aggro-v2.pages.dev.
#
# Phase 11h — Path B2 (tunnel-backed public dashboard).
#
# Previously this script rewrote /apex/* and /spot_aggro/* routes to
# target the Cloudflare Worker stub at apex-omega-api.benheng77.workers.dev.
# That Worker only implements ~5 of the 19 endpoints the Phase 11d
# dashboard needs (no /spot_aggro/build, no /auth/ping, no tier_toggles,
# no tracker for trades/forensic/governor/watchdog/etc.), so ~80% of
# cards came out empty or broken on the public URL.
#
# The new backend is the local uvicorn exposed through Cloudflare Tunnel.
# The tunnel serves the exact same endpoints as /ops/ locally, so the
# only rewrite needed is the API base URL (localhost -> tunnel).
#
# Usage:
#   deploy/spot-aggro-v2/build.sh <TUNNEL_URL>
#
# Example:
#   deploy/spot-aggro-v2/build.sh https://washing-dictionaries-lung-coal.trycloudflare.com
#
# Operators can also override at runtime by setting
# localStorage.spot_aggro_api in their browser — useful when the
# Quick Tunnel rotates.

set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$REPO/web/ops/index.html"
DST="$REPO/deploy/spot-aggro-v2/index.html"

# Phase 11n-9-uu — verify the HTML dashboard-build meta tag matches the
# server SERVER_BUILD constant. Mismatch causes the operator-facing
# "BUILD · MISMATCH · TAP TO RELOAD" pill, which is annoying-yet-correct
# symptom of forgetting to bump the HTML when the server bumps.
SERVER_BUILD=$(grep -oE 'SERVER_BUILD *= *"phase-11n-9-[a-z]+-2026-04-20"' \
    "$REPO/openclaw_v1/spot_aggro/api/routes.py" \
    | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
HTML_BUILD=$(grep -oE 'dashboard-build" content="phase-11n-9-[a-z]+-2026-04-20"' \
    "$SRC" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')

if [ "$SERVER_BUILD" != "$HTML_BUILD" ]; then
  echo "[build] ERROR: build tag mismatch"
  echo "[build]   SERVER_BUILD in routes.py : $SERVER_BUILD"
  echo "[build]   dashboard-build in HTML   : $HTML_BUILD"
  echo "[build] Bump the <meta name=\"dashboard-build\"> content in $SRC to match."
  exit 1
fi
echo "[build] build tags aligned: $SERVER_BUILD"

TUNNEL_URL="${1:-https://washing-dictionaries-lung-coal.trycloudflare.com}"

# Strip any trailing slash.
TUNNEL_URL="${TUNNEL_URL%/}"

echo "[build] source: $SRC"
echo "[build] target: $DST"
echo "[build] tunnel: $TUNNEL_URL"

cp "$SRC" "$DST"

# Only rewrite needed: API base URL. Every route stays the same because
# the tunnel is a transparent proxy to the local uvicorn.
# localStorage.spot_aggro_api lets operators override at runtime.
#
# We use Python (not sed) because the replacement string contains both
# shell-metacharacters and quotes. The path is converted from bash-style
# /c/Users/... to Windows-style C:\Users\... so Windows Python can open it.
DST_WIN=$(cygpath -w "$DST" 2>/dev/null || echo "$DST")
TUNNEL_URL="$TUNNEL_URL" DST_PATH="$DST_WIN" python -c "
import os
dst = os.environ['DST_PATH']
tunnel = os.environ['TUNNEL_URL']
src = open(dst, encoding='utf-8').read()
old = 'const API = \"http://localhost:8080\";'
new = 'const API = localStorage.getItem(\"spot_aggro_api\") || \"' + tunnel + '\";'
assert old in src, 'expected API constant not found in web/ops/index.html'
open(dst, 'w', encoding='utf-8').write(src.replace(old, new))
print('[build] API base rewritten: localhost:8080 -> ' + tunnel)
"

# Verify no localhost references remain in the deployed artifact.
if grep -q "localhost:8080" "$DST"; then
  echo "[build] ERROR: localhost:8080 still present in $DST -- rewrite failed"
  grep -n "localhost:8080" "$DST" | head -5
  exit 1
fi

size=$(wc -c < "$DST")
echo "[build] wrote $size bytes"

# Phase 11n-9-ss — also build the isolated Contrarian + Deep Value panel.
CDV_SRC="$REPO/web/strategy/contrarian-deepvalue/index.html"
CDV_DST_DIR="$REPO/deploy/spot-aggro-v2/strategy/contrarian_deepvalue"
CDV_DST="$CDV_DST_DIR/index.html"
if [ -f "$CDV_SRC" ]; then
  mkdir -p "$CDV_DST_DIR"
  cp "$CDV_SRC" "$CDV_DST"
  # The CDV panel's JS derives API_BASE from location.hostname so no
  # hard-coded localhost rewrite is needed. Just verify the file landed.
  cdv_size=$(wc -c < "$CDV_DST")
  echo "[build] CDV panel shipped to /strategy/contrarian_deepvalue/ ($cdv_size bytes)"
else
  echo "[build] CDV panel source missing — skipping"
fi

echo "[build] done"
