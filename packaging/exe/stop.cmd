@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Trace stop

REM Prefer the pid file the app writes at startup (<DATA_DIR>\game_agent.pid).
REM Fall back to matching the exe name only if the pid file is missing.

set "PIDFILE="
if defined GA_DATA_DIR (
  set "PIDFILE=%GA_DATA_DIR%\game_agent.pid"
) else (
  set "PIDFILE=%USERPROFILE%\.trace\game_agent.pid"
)

if exist "%PIDFILE%" (
  set /p GPID=<"%PIDFILE%"
  echo [stop] pid file: %PIDFILE%  pid=%GPID%
  if defined GPID (
    taskkill /PID %GPID% /F >nul 2>nul
    if errorlevel 1 (
      echo [stop] process %GPID% not running ^(already stopped^).
    ) else (
      echo [stop] stopped Trace ^(pid %GPID%^).
    )
  )
  del /q "%PIDFILE%" >nul 2>nul
  goto done
)

echo [stop] pid file not found, stopping Trace.exe by name ...
taskkill /IM Trace.exe /F >nul 2>nul
if errorlevel 1 (
  echo [stop] no Trace.exe process found.
) else (
  echo [stop] stopped Trace.exe.
)

:done
echo [stop] done.
timeout /t 2 >nul
endlocal
