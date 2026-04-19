@echo off
REM ============================================================
REM Remove the OpenClaw Control Panel auto-start task.
REM Does NOT stop the panel if it is currently running - use stop.cmd.
REM ============================================================

setlocal enableextensions

set "C_OK=[32m"
set "C_WARN=[33m"
set "C_DIM=[90m"
set "C_RESET=[0m"

set "TASK_NAME=OpenClawControlPanel"

echo.
echo Removing auto-start entry "%TASK_NAME%" ...
schtasks /Delete /F /TN "%TASK_NAME%" >nul 2>nul
if errorlevel 1 (
  echo   %C_WARN%Nothing to remove.%C_RESET% Auto-start was not installed.
) else (
  echo   %C_OK%Removed.%C_RESET% The panel will no longer auto-start at logon.
)
echo.
echo %C_DIM%Note: if the panel is currently running, use stop.cmd to stop it.%C_RESET%
echo.
pause
exit /b 0
