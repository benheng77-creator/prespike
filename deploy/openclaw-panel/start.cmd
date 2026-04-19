@echo off
REM ============================================================
REM OpenClaw Control Panel — Windows launcher.
REM
REM Double-click to start. Will:
REM   1. Verify Node 22+ is installed (download link if missing).
REM   2. Download cloudflared.exe on first run if not already present.
REM   3. Start the panel server in the background.
REM   4. Start a Cloudflare Quick Tunnel pointing at the panel.
REM   5. Capture the *.trycloudflare.com URL and save it to state\tunnel.url.
REM   6. Print the full panel URL (including the auth token) for the user.
REM
REM This script does NOT run on the setup PC by default — it is meant to be
REM copied to the target PC inside the deploy\openclaw-panel folder and
REM launched there.
REM ============================================================

setlocal enableextensions enabledelayedexpansion

set "BUNDLE_ROOT=%~dp0"
if "%BUNDLE_ROOT:~-1%"=="\" set "BUNDLE_ROOT=%BUNDLE_ROOT:~0,-1%"

set "STATE_DIR=%BUNDLE_ROOT%\state"
set "BIN_DIR=%BUNDLE_ROOT%\bin"
set "CLOUDFLARED=%BIN_DIR%\cloudflared.exe"
set "TUNNEL_URL_FILE=%STATE_DIR%\tunnel.url"
set "TUNNEL_LOG_FILE=%STATE_DIR%\tunnel.log"
set "PANEL_LOG_FILE=%STATE_DIR%\panel.log"
set "TOKEN_FILE=%STATE_DIR%\token.txt"
set "PORT=%OPENCLAW_PANEL_PORT%"
if "%PORT%"=="" set "PORT=8787"

if not exist "%STATE_DIR%" mkdir "%STATE_DIR%"
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"

echo.
echo === OpenClaw Control Panel ===
echo Bundle root: %BUNDLE_ROOT%
echo State dir:   %STATE_DIR%
echo Port:        %PORT%
echo.

REM ---- 1. Check Node.js ----
where node >nul 2>nul
if errorlevel 1 (
  echo [error] Node.js is not installed.
  echo Install Node 22 LTS from https://nodejs.org/  then re-run this script.
  pause
  exit /b 1
)

for /f "tokens=*" %%v in ('node --version') do set "NODE_VERSION=%%v"
echo Node version: %NODE_VERSION%

REM ---- 2. Download cloudflared if missing ----
if not exist "%CLOUDFLARED%" (
  echo Downloading cloudflared.exe ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '%CLOUDFLARED%'"
  if errorlevel 1 (
    echo [error] cloudflared download failed. Check internet access and try again.
    pause
    exit /b 1
  )
)

REM ---- 3. Start panel ----
echo Starting panel on http://127.0.0.1:%PORT% ...
start "openclaw-panel" /min cmd /c "node ""%BUNDLE_ROOT%\panel\server.mjs"" >> ""%PANEL_LOG_FILE%"" 2>&1"

REM Give the panel a moment to write the token and bind the port.
timeout /t 2 /nobreak >nul

REM ---- 4. Start cloudflared Quick Tunnel ----
echo Starting Cloudflare Quick Tunnel ...
del /q "%TUNNEL_URL_FILE%" 2>nul
start "openclaw-tunnel" /min cmd /c """%CLOUDFLARED%"" tunnel --no-autoupdate --url http://127.0.0.1:%PORT% >> ""%TUNNEL_LOG_FILE%"" 2>&1"

REM ---- 5. Capture URL ----
echo Waiting for tunnel URL ...
set "TRY=0"
:wait_url
set /a TRY+=1
if %TRY% gtr 30 (
  echo [warn] Tunnel URL did not appear after 30 seconds. Check %TUNNEL_LOG_FILE%.
  goto print_local
)
timeout /t 1 /nobreak >nul
findstr /r /c:"https://[a-z0-9-]*\.trycloudflare\.com" "%TUNNEL_LOG_FILE%" >nul 2>nul
if errorlevel 1 goto wait_url

powershell -NoProfile -Command ^
  "$m = Select-String -Path '%TUNNEL_LOG_FILE%' -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1; if ($m) { ($m.Matches[0].Value) | Out-File -Encoding ascii '%TUNNEL_URL_FILE%' }"

if not exist "%TUNNEL_URL_FILE%" goto print_local

set /p TUNNEL_URL=<"%TUNNEL_URL_FILE%"
set /p PANEL_TOKEN=<"%TOKEN_FILE%"

echo.
echo ============================================================
echo  Public panel URL:
echo    %TUNNEL_URL%/p/%PANEL_TOKEN%/
echo  Local panel URL:
echo    http://127.0.0.1:%PORT%/p/%PANEL_TOKEN%/
echo ============================================================
echo.
echo Token + URL also saved in %STATE_DIR%.
echo Press any key to close this window. The panel will keep running.
pause >nul
exit /b 0

:print_local
if exist "%TOKEN_FILE%" (
  set /p PANEL_TOKEN=<"%TOKEN_FILE%"
  echo Local panel URL: http://127.0.0.1:%PORT%/p/!PANEL_TOKEN!/
)
echo Tunnel URL not yet ready. See %TUNNEL_LOG_FILE%.
pause
exit /b 0
