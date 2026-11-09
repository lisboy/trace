@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Trace stop

REM Prefer the pid file written by the app at startup (<DATA_DIR>\game_agent.pid).
REM Fall back to a PowerShell lookup only if the pid file is missing.

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

echo [stop] pid file not found, searching for python -m game_agent.main ...
where powershell >nul 2>nul
if not errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*game_agent.main*' -and $_.Name -like 'python*' }; if (-not $p) { Write-Host '[stop] no Trace process found.' } else { foreach ($x in $p) { Write-Host ('[stop] killing PID ' + $x.ProcessId); Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue } }"
) else (
  echo [stop] No pid file and PowerShell is unavailable; nothing to do.
  echo        If a server is still running, close its window or use Task Manager.
)

:done
echo [stop] done.
timeout /t 2 >nul
endlocal
