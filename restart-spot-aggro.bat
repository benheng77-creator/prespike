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
REM Contrarian re-enabled alongside deep_value + momentum. Per-variant $25
REM A/B cap + $3 24h auto-disable trip-wire + 40-exit Wilson promotion.
set SPOT_LIVE_VARIANTS=contrarian,deep_value,momentum
set SPOT_LIVE_PER_VARIANT_CAP_USD=25
set SPOT_VARIANT_DD_KILL_USD=3
set SPOT_PROMO_N_EXITS=40

REM Isolated CDV panel visibility (phase-ss).
set FEATURE_CONTRARIAN_DEEPVALUE_PANEL=1

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
