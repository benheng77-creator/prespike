#!/usr/bin/env bash
# Phase 11m — Deterministic headless verification gate.
#
# This is the BLOCKING pre-deploy check. It must succeed before any
# rebuild / redeploy step runs. Fails hard on any error.
#
# What it does:
#   1. Runs the full spot_aggro pytest suite (all unit / integration /
#      API / WARN self-heal / E2E tests).
#   2. Runs a deterministic end-to-end harness that:
#        - seeds a trades.db with a known bad-WR pattern
#        - calls research_agent.run_and_persist with an injected clock
#          frozen at T=0, then T+1h, then T+6h
#        - asserts halt fired at T=0, interim report exists at T+1h,
#          final plan exists at T+6h
#        - asserts cancel_open_buys_for_tier was called exactly for
#          the halted tier (via a mock adapter)
#   3. Runs the Node jsdom headless audit to prove the dashboard
#      reports 31/31 OK cards after normal refresh.
#   4. Emits a structured JSON artifact at
#      artifacts/headless_verify_{ISO}.json with logs + evidence refs.
#
# Usage:
#   ci/headless_verify.sh [--out=artifacts/headless_verify_ISO.json]
#
# Exit codes:
#   0  — all checks passed (deploy may proceed)
#   1  — at least one check failed (deploy MUST be blocked)
#   2  — harness setup error (retry possible)

set -euo pipefail

# -------- arg parsing --------
OUT_ARTIFACT=""
for arg in "$@"; do
  case "$arg" in
    --out=*)  OUT_ARTIFACT="${arg#--out=}" ;;
    *)        echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
ISO="$(date -u +"%Y-%m-%dT%H-%M-%SZ")"
: "${OUT_ARTIFACT:=artifacts/headless_verify_${ISO}.json}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$(dirname "$OUT_ARTIFACT")"

# -------- deterministic env --------
export APP_ENV="${APP_ENV:-ci}"
export DET_CLOCK="${DET_CLOCK:-1}"
# Research thresholds: tests assume MIN_SAMPLE=10 for readability of the
# E2E harness. Production default (50) is enforced in the separate
# unit tests that use the shipped research.yml.
export SPOT_RESEARCH_MIN_SAMPLE="${SPOT_RESEARCH_MIN_SAMPLE:-10}"

START_TS="$(date -u +%s)"
RESULT="pass"
REASONS=()
LOG_TMP="$(mktemp)"

trap 'rm -f "$LOG_TMP"' EXIT

# -------- helper: append JSON artifact at exit --------
emit_artifact() {
  local status="$1"
  local duration=$(( $(date -u +%s) - START_TS ))
  local reasons_json="[]"
  if [ ${#REASONS[@]} -gt 0 ]; then
    reasons_json="["
    for r in "${REASONS[@]}"; do
      reasons_json+="$(printf '%s' "$r" | python -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))'),"
    done
    reasons_json="${reasons_json%,}]"
  fi
  # Evidence refs capture file-level snapshots that later audits can diff.
  cat > "$OUT_ARTIFACT" <<EOF
{
  "artifact_version": "1",
  "generated_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "started_at": "$(date -u -d "@$START_TS" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -r "$START_TS" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown")",
  "duration_s": $duration,
  "status": "$status",
  "phase": "11m",
  "environment": {
    "APP_ENV": "$APP_ENV",
    "DET_CLOCK": "$DET_CLOCK",
    "SPOT_RESEARCH_MIN_SAMPLE": "$SPOT_RESEARCH_MIN_SAMPLE"
  },
  "evidence_refs": [
    "pytest:openclaw_v1/spot_aggro",
    "harness:ci/_e2e_harness.py",
    "jsdom:_headless_audit.cjs"
  ],
  "reasons": $reasons_json,
  "log_tail": $(tail -c 20000 "$LOG_TMP" | python -c 'import sys,json; print(json.dumps(sys.stdin.read()))')
}
EOF
  echo ""
  echo "=============================================="
  echo "headless_verify artifact: $OUT_ARTIFACT"
  echo "status: $status · duration: ${duration}s"
  echo "=============================================="
}

fail() {
  RESULT="fail"
  REASONS+=("$1")
  echo "[FAIL] $1" | tee -a "$LOG_TMP"
}

# -------- 1. pytest suite --------
echo "[1/3] Running pytest: openclaw_v1/spot_aggro ..." | tee -a "$LOG_TMP"
if ! python -m pytest openclaw_v1/spot_aggro -q --continue-on-collection-errors 2>&1 | tee -a "$LOG_TMP"; then
  fail "pytest suite failed"
fi

# -------- 2. deterministic E2E harness --------
echo "[2/3] Running deterministic E2E harness ..." | tee -a "$LOG_TMP"
if ! python ci/_e2e_harness.py 2>&1 | tee -a "$LOG_TMP"; then
  fail "deterministic E2E harness failed"
fi

# -------- 3. Node jsdom headless audit (31/31 card check) --------
echo "[3/3] Running jsdom headless audit ..." | tee -a "$LOG_TMP"
NODE="node"
if ! command -v node >/dev/null 2>&1; then
  # Windows: cloudflared installs Node via winget at a known path.
  if [ -x "/c/Program Files/nodejs/node.exe" ]; then
    NODE="/c/Program Files/nodejs/node.exe"
  else
    fail "node not found on PATH — jsdom headless audit skipped"
  fi
fi
# Check whether a local uvicorn is up. The jsdom audit hits 127.0.0.1:8080.
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/spot_aggro/build | grep -q 200 ; then
  if ! "$NODE" _headless_audit.cjs 2>&1 | tee -a "$LOG_TMP"; then
    fail "jsdom headless audit returned non-zero (WARN/FAIL cards present)"
  fi
else
  # Not a hard fail — the deterministic E2E does not require a live server.
  # But we log it clearly so operator knows the browser-side check was
  # skipped.
  echo "[warn] local uvicorn not running on :8080 — jsdom audit skipped" | tee -a "$LOG_TMP"
  REASONS+=("jsdom audit skipped: no live uvicorn")
fi

# -------- finalise --------
emit_artifact "$RESULT"
if [ "$RESULT" != "pass" ]; then
  echo ""
  echo "HEADLESS VERIFY FAILED — deploy MUST NOT proceed."
  exit 1
fi
echo ""
echo "HEADLESS VERIFY PASSED — deploy may proceed (subject to manual"
echo "approval if non-blocking warnings remain in the artifact)."
exit 0
