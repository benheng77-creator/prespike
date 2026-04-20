@echo off
REM ============================================================
REM Phase 11n-9-e — Restart spot_aggro backend with all auto envs.
REM
REM Run this whenever you pulled new code and the dashboard buttons
REM (Rebuild / Sweep Now / Tick Once) return 404 — the old uvicorn
REM process is still serving a stale build. This script:
REM   1. Kills any process bound to :8080.
REM   2. Relaunches uvicorn with the Phase 11n-9-e auto flags set so
REM      the orchestrator, daily alpha executor, and reconciled
REM      sweeper all run fully auto from the first tick.
REM ============================================================

setlocal enableextensions
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo [restart] killing anything on :8080 ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr :8080 ^| findstr LISTENING') do (
  echo [restart]   stopping PID %%P
  taskkill /F /PID %%P >nul 2>&1
)

REM Auto-orchestrator defaults ON in server.py (11n-8). Explicit here
REM for clarity + forward-compat.
set SPOT_AUTO_ORCHESTRATOR=1
set SPOT_AUTO_INTERVAL_S=300

REM Daily Alpha auto-executor (11n-9-c/e): admitted picks → real orders.
set SPOT_ALPHA_AUTO_EXECUTE=1
set SPOT_ALPHA_NOTIONAL_USD=25

REM Reconciled Sweeper (11n-9-d/e): orphan positions → keep/sell/link.
set SPOT_RECON_SWEEP_EXECUTE=1

REM Phase 11n-9-vv — Path B + C horse race.
REM Contrarian re-enabled alongside deep_value + momentum. Per-variant $40
REM A/B cap (raised from $25 to support 20-60 trades/day volume goal) +
REM $3 24h auto-disable trip-wire + 40-exit Wilson promotion. Phase-ww
REM System Activity auto-heal governor watches component health.
set SPOT_LIVE_VARIANTS=contrarian,deep_value,momentum
set SPOT_LIVE_PER_VARIANT_CAP_USD=40
set SPOT_VARIANT_DD_KILL_USD=3
set SPOT_PROMO_N_EXITS=40

REM Isolated CDV panel visibility (phase-ss).
set FEATURE_CONTRARIAN_DEEPVALUE_PANEL=1

REM Phase 11n-9-ww+ — C1 readiness bypass. Declares the phase-vv commit
REM as the sign-flip "fix" so trade_readiness unblocks live entries. Paired
REM with per-variant trip-wire ($3 24h DD), $10 session DD kill, $50
REM exposure cap, and Decision Quality governor monitoring rank
REM monotonicity per cell. Operator acknowledges Layer 12 residual risk
REM is held by these downstream guardrails.
set SIGN_FLIP_COMMIT=phase-11n-9-vv-d5629e26db

REM Opportunity Fabric Sprint 1 — exploration wallet. Hard-separates
REM R&D capital ($30) from production (~$335). When exploratory
REM variants (contrarian, deep_value) drop -$5 over a rolling 24h
REM window, they are disabled until operator reset. Momentum keeps
REM trading. Default-OFF otherwise — uncomment to activate.
set SPOT_EXPLORATION_WALLET_USD=30
set SPOT_EXPLORATION_DD_KILL_USD=5
set SPOT_EXPLORATION_VARIANTS=contrarian,deep_value

REM Opportunity Fabric Sprint 3 — execution SLO promotion gate. Wilson
REM promotion now requires realized slippage < 10bp AND fill_rate > 95%%
REM in addition to the statistical rule. When stats pass but execution
REM is borderline, verdict becomes 'promote_statistical' (operator
REM signoff required) instead of auto-promote.
set SPOT_EXEC_SLO_GATE=1
set SPOT_EXEC_SLO_SLIPPAGE_BP=10
set SPOT_EXEC_SLO_FILL_RATE=0.95

REM Opportunity Fabric Sprint 4 — fractal regime confirmation gate.
REM Require >=2 of 3 scales (1m/5m/1h) to agree on regime sign before
REM admission. Disagreement blocks admission when gate is ON.
set SPOT_FRACTAL_REGIME_GATE=1

REM Opportunity Fabric Sprint 5 — passive liquidity inference gate.
REM Composite score of thinness + spread + imbalance + own-flow realized
REM slippage + fill_rate. Score in [0, 1]: 0=tradeable, 1=avoid. Gate
REM rejects when score >= SPOT_LIQ_ABORT_SCORE. PASSIVE ONLY — never
REM places probe orders. Conservative activation (threshold=0.80) to
REM avoid blocking too much in opening hours; tighten to 0.75 later.
set SPOT_LIQ_INFERENCE_GATE=1
set SPOT_LIQ_ABORT_SCORE=0.80

REM Opportunity Fabric Sprint 7 — policy bank tiering. Metadata layer
REM assigning each variant to conservative/exploratory/baseline.
REM Exploratory variants (contrarian, deep_value) route through the
REM exploration wallet — if the wallet's 24h PnL breaches the DD kill,
REM those variants are blocked at admission with
REM reason=exploration_wallet_disabled. No env needed; default
REM assignments in policy_bank.DEFAULT_TIERS are:
REM   contrarian, deep_value -> exploratory
REM   momentum               -> conservative
REM   mean_reversion, control -> baseline (paper-only)

REM Research thresholds stay advisory (11n-2): halt verdicts are tagged
REM but never flip tier toggles unless you opt in.
REM  set SPOT_RESEARCH_ENFORCE_HALT=1

REM Safety kill-switch: SET TRADE_DRY_RUN=1 OR SPOT_DRY_RUN=1 to stop
REM ALL order placement (executor + sweeper + engine entries) without
REM touching individual auto flags.
REM  set TRADE_DRY_RUN=1

echo [restart] launching uvicorn on :8080 with all auto flags ON ...
start "claw247-api" /min cmd /k "cd /d %ROOT%\openclaw_v1 && python -m uvicorn server:app --host 0.0.0.0 --port 8080"

echo [restart] waiting for /spot_aggro/build ...
set "_WAITED=0"
:wait_api
timeout /t 1 /nobreak >nul
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8080/spot_aggro/build > "%TEMP%\_spot_http.txt"
set /p _CODE=<"%TEMP%\_spot_http.txt"
if "%_CODE%"=="200" goto api_up
set /a _WAITED+=1
if %_WAITED% LSS 30 goto wait_api
echo [restart] API did not come up within 30s — check the console window.
goto end

:api_up
echo [restart] API up. Probing build tag ...
curl -s http://127.0.0.1:8080/spot_aggro/build
echo.
echo [restart] Done. Refresh the dashboard. The build pill should show the new tag.

:end
endlocal
