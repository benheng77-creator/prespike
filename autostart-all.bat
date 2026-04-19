@echo off
REM ============================================================
REM TradeBot full auto-start: API + Ops Dashboard + Control Panel
REM Runs at every Windows logon via scheduled task TradeBotAutoStart
REM ============================================================

setlocal enableextensions
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

REM --- 1. API on port 8080 ---
start "claw247-api" /min cmd /k "cd /d %ROOT%\openclaw_v1 && python -m uvicorn server:app --host 0.0.0.0 --port 8080"

REM --- 2. Ops dashboard static site on port 5173 ---
start "claw247-dashboard" /min cmd /k "cd /d %ROOT%\web\ops && python -m http.server 5173 --bind 0.0.0.0"

REM --- 3. OpenClaw Control Panel (node + cloudflared) ---
if exist "%ROOT%\deploy\openclaw-panel\start.cmd" (
  start "openclaw-panel-launcher" /min cmd /c "%ROOT%\deploy\openclaw-panel\start.cmd"
)

REM --- 4. Wait for API, then auto-arm the spot engine ---
REM    Poll /apex/spot_aggro/status up to 30s. Once reachable, POST /start
REM    so the trading engine is live without manual intervention.
REM    Token read from %ROOT%\.env via a tiny Python one-liner to avoid
REM    hard-coding secrets in this .bat.
echo [tradebot] waiting for API on port 8080 ...
set "_WAITED=0"
:wait_api
timeout /t 1 /nobreak >nul
set /a _WAITED+=1
powershell -NoProfile -Command "try { $null = Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8080/apex/spot_aggro/status' -TimeoutSec 2; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  if %_WAITED% lss 30 goto wait_api
  echo [tradebot] API did not come up in 30s — skipping auto-arm.
  goto open_browser
)

echo [tradebot] API reachable. Arming spot engine ...
for /f "usebackq tokens=*" %%T in (`python -c "import os; from pathlib import Path; [os.environ.update({k.strip():v.strip()}) for line in Path(r'%ROOT%\.env').read_text().splitlines() if line and not line.startswith('#') and '=' in line for k,v in [line.split('=',1)]]; print(os.environ.get('OPS_ADMIN_TOKEN',''))"`) do set "OPS_TOK=%%T"
if "%OPS_TOK%"=="" (
  echo [tradebot] OPS_ADMIN_TOKEN not found in .env — skipping auto-arm.
) else (
  powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Method POST -Uri 'http://localhost:8080/apex/spot_aggro/start' -Headers @{'x-ops-token'=$env:OPS_TOK} -TimeoutSec 10; Write-Host '[tradebot] arm result:' $r.StatusCode $r.Content } catch { Write-Host '[tradebot] auto-arm failed:' $_.Exception.Message }"
)

:open_browser
echo [tradebot] Opening dashboard ...
start "" "http://localhost:5173/"

exit /b 0
