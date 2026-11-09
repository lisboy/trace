@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Trace

set "VPY=%~dp0.venv\Scripts\python.exe"

REM ---- first run: no venv yet -> run setup automatically ----
if not exist "%VPY%" (
  echo [start] first run detected, setting up runtime automatically ...
  call "%~dp0setup.cmd"
  if not exist "%VPY%" (
    echo [x] setup did not complete. Please double-click setup.cmd and try again.
    pause
    exit /b 1
  )
)

REM ---- .env must exist ----
if not exist ".env" (
  if exist "env.template" (
    copy /y "env.template" ".env" >nul
    echo [!] .env was missing, created from env.template - opening Notepad now.
    echo     Fill in your LLM settings - base url / key / model - SAVE and close it,
    echo     then pick [1] below to start.
    notepad ".env"
  ) else (
    echo [x] .env is missing.
    pause
    exit /b 1
  )
)
if not exist ".env" (
  echo [x] .env is still missing.
  pause
  exit /b 1
)

if not defined GA_MODE set GA_MODE=lite

REM Bundled build: seed the demo account on first launch (demo@local / demo1234).
REM Delete GA_SEED_DEMO from .env to fall back to the /setup bootstrap-token flow.
set GA_SEED_DEMO=1

REM The server opens the browser only after it is truly ready and the real port is
REM known (default 18888; auto-shifts if busy or inside a Windows reserved range).
set GA_OPEN_BROWSER=1

if not exist "logs" mkdir "logs" 2>nul

:menu
cls
echo ============================================
echo   Trace
echo ============================================
echo   [1] Start server      default, http://127.0.0.1:18888/
echo   [2] Reset a password  forgot your login
echo   [Q] Quit
echo ============================================
set "SEL="
set /p "SEL=Choose 1 / 2 / Q : "
if not defined SEL set "SEL=1"
if "%SEL%"=="1" goto run
if "%SEL%"=="2" goto reset
if /i "%SEL%"=="R" goto reset
if /i "%SEL%"=="Q" goto quit
goto menu

:reset
echo.
echo --------------------------------------------
echo   Reset a password (no old password needed)
echo --------------------------------------------
"%VPY%" -m game_agent.cli reset-password
echo.
pause
goto menu

:run
echo ============================================
echo   Starting Trace ...
echo   Default http://127.0.0.1:18888/  opens automatically once ready
echo   If 18888 is busy/reserved it auto-shifts; the real port is in the log below.
echo   To stop: double-click stop.cmd  or press Ctrl+C here.
echo ============================================
"%VPY%" -m game_agent.main
echo.
echo [server exited] code=%errorlevel%
pause
goto menu

:quit
endlocal
exit /b 0
