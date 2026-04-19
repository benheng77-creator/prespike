@echo off
REM ============================================================
REM Install OpenClaw Control Panel as a Windows logon task.
REM Runs start.cmd automatically every time this Windows user
REM logs in. Safe to re-run; /F replaces any existing task.
REM ============================================================

setlocal enableextensions enabledelayedexpansion

set "C_OK=[32m"
set "C_WARN=[33m"
set "C_ERR=[31m"
set "C_STEP=[36m"
set "C_DIM=[90m"
set "C_RESET=[0m"

set "BUNDLE_ROOT=%~dp0"
if "%BUNDLE_ROOT:~-1%"=="\" set "BUNDLE_ROOT=%BUNDLE_ROOT:~0,-1%"

set "TASK_NAME=OpenClawControlPanel"
set "START_CMD=%BUNDLE_ROOT%\start.cmd"

title OpenClaw - Install Auto-Start
cls
echo.
echo %C_STEP%============================================================%C_RESET%
echo %C_STEP%       OpenClaw Control Panel - Auto-start Installer%C_RESET%
echo %C_STEP%============================================================%C_RESET%
echo.
echo This will make the panel launch automatically every time you
echo log into Windows on this PC.
echo.

if not exist "%START_CMD%" (
  echo   %C_ERR%start.cmd not found at:%C_RESET%
  echo     %START_CMD%
  echo.
  echo   Make sure you are running this from the same folder that
  echo   contains start.cmd. If you moved the folder, copy the whole
  echo   'openclaw-panel' folder to one place and run from there.
  echo.
  pause
  exit /b 1
)

echo %C_STEP%[1/2]%C_RESET% Registering scheduled task "%TASK_NAME%" ...
REM /F replaces any existing task silently. /RL LIMITED keeps it in the
REM user's own context - no UAC prompt at logon.
schtasks /Create /F /TN "%TASK_NAME%" /SC ONLOGON /RL LIMITED ^
  /TR "cmd /c \"\"%START_CMD%\"\"" >nul

if errorlevel 1 (
  echo   %C_ERR%Could not register the task.%C_RESET%
  echo.
  echo   What to try:
  echo     * Make sure you are signed into Windows as the same user
  echo       who will use the panel.
  echo     * If you are in a corporate environment, Group Policy may
  echo       block scheduled tasks - contact your IT administrator.
  echo.
  pause
  exit /b 1
)
echo   %C_OK%OK%C_RESET%

echo %C_STEP%[2/2]%C_RESET% Verifying the task is registered ...
schtasks /Query /TN "%TASK_NAME%" >nul 2>nul
if errorlevel 1 (
  echo   %C_WARN%Task was created but cannot be queried.%C_RESET%
  echo   It should still work at next logon.
) else (
  echo   %C_OK%OK%C_RESET% - task "%TASK_NAME%" is scheduled
)

echo.
echo %C_OK%============================================================%C_RESET%
echo %C_OK% Installed. The panel will auto-start on next Windows logon.%C_RESET%
echo %C_OK%============================================================%C_RESET%
echo.
echo  To start the panel now without logging out:
echo    %C_STEP%schtasks /Run /TN "%TASK_NAME%"%C_RESET%
echo.
echo  To remove auto-start:
echo    %C_STEP%uninstall-autostart.cmd%C_RESET%
echo.
pause
exit /b 0
