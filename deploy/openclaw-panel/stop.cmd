@echo off
REM ============================================================
REM Stop the OpenClaw panel and its cloudflared tunnel.
REM Reports clearly whether anything was actually stopped.
REM ============================================================

setlocal enableextensions

set "C_OK=[32m"
set "C_WARN=[33m"
set "C_ERR=[31m"
set "C_DIM=[90m"
set "C_RESET=[0m"

set "TUNNEL_STOPPED=0"
set "PANEL_STOPPED=0"

echo.
echo Stopping OpenClaw Control Panel ...
echo.

taskkill /FI "WINDOWTITLE eq openclaw-tunnel*" /T /F >nul 2>nul
if not errorlevel 1 set "TUNNEL_STOPPED=1"

taskkill /FI "WINDOWTITLE eq openclaw-panel*" /T /F >nul 2>nul
if not errorlevel 1 set "PANEL_STOPPED=1"

if "%TUNNEL_STOPPED%"=="1" (
  echo   %C_OK%stopped%C_RESET% cloudflared tunnel
) else (
  echo   %C_DIM%n/a    %C_RESET% cloudflared tunnel was not running
)

if "%PANEL_STOPPED%"=="1" (
  echo   %C_OK%stopped%C_RESET% panel server
) else (
  echo   %C_DIM%n/a    %C_RESET% panel server was not running
)

if "%TUNNEL_STOPPED%"=="0" if "%PANEL_STOPPED%"=="0" (
  echo.
  echo   %C_WARN%Nothing to stop. The panel was not running.%C_RESET%
  echo.
  timeout /t 2 /nobreak >nul
  exit /b 0
)

echo.
echo   %C_OK%Done.%C_RESET% Run start.cmd to launch again.
echo.
timeout /t 2 /nobreak >nul
exit /b 0
