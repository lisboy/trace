@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Trace setup

echo ============================================
echo   Trace - one-click setup
echo ============================================
echo.
echo This will prepare the runtime (Python venv + dependencies).
echo Just wait; the first run downloads packages and may take a few minutes.
echo.

REM ---- 1. locate Python 3.12+ ----
set "PYEXE="
call :check python
if not defined PYEXE call :check python3
if not defined PYEXE call :check py -3.12
if not defined PYEXE call :check py -3
if not defined PYEXE goto no_python

echo [install] Python: %PYEXE%
for /f "delims=" %%v in ('%PYEXE% -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" 2^>nul') do set "PYVER=%%v"
echo [install] version: %PYVER%
echo.

REM ---- 2. create venv ----
if not exist ".venv\Scripts\python.exe" (
  echo [install] creating virtual env .venv ...
  "%PYEXE%" -m venv .venv
  if errorlevel 1 goto venv_fail
) else (
  echo [install] reusing existing .venv
)
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" goto venv_fail

REM optional first arg = pip index mirror, e.g.  setup.cmd https://pypi.tuna.tsinghua.edu.cn/simple
set "PIP_ARGS="
if not "%~1"=="" set "PIP_ARGS=-i %~1"

if defined GA_SETUP_SKIP_DEPS (
  echo [install] GA_SETUP_SKIP_DEPS set, skip dependency install
  goto env_step
)

echo [install] upgrading pip ...
"%VPY%" -m pip install --upgrade pip %PIP_ARGS%
echo.
echo [install] installing requirements ...
"%VPY%" -m pip install %PIP_ARGS% -r requirements.txt
if errorlevel 1 goto deps_fail
echo.
echo [install] installing mcp (MCP tools) ...
"%VPY%" -m pip install %PIP_ARGS% mcp
if errorlevel 1 echo [!] mcp not installed; MCP tools disabled, everything else works.
echo.

:env_step
REM ---- 3. .env ----
if exist ".env" (
  echo [install] .env already exists, keep it.
) else (
  if not exist "env.template" goto no_template
  copy /y "env.template" ".env" >nul
  echo [install] created .env from env.template
)

echo.
echo ================= DONE =================
echo   1^) open  .env  with Notepad and set:
echo        GA_LLM_MODE=real
echo        OPENAI_API_KEY=your-key
echo        OPENAI_BASE_URL=https://your-endpoint/v1
echo        GA_LLM_MODEL=model-name
echo   2^) double-click  start.cmd  to run
echo   3^) open http://127.0.0.1:18888/setup to create the first owner
echo      (default port 18888; if busy it auto-shifts, real port prints at startup)
echo   Stop service: double-click stop.cmd
echo =======================================
echo.
pause
exit /b 0

:no_python
echo [x] Python 3.12+ was not found on this computer.
echo.
echo     Please install Python 3.12 or newer:
echo       https://www.python.org/downloads/
echo     During installation, TICK the box  "Add python.exe to PATH".
echo     Then double-click setup.cmd again.
echo.
pause
exit /b 1

:venv_fail
echo [x] Failed to create the virtual environment .venv
echo     Try installing Python from python.org (3.12+) and run setup.cmd again.
echo.
pause
exit /b 1

:deps_fail
echo [x] Dependency installation failed.
echo     - Check your network, or use a mirror:
echo         setup.cmd https://pypi.tuna.tsinghua.edu.cn/simple
echo     - Behind a proxy? set HTTPS_PROXY then run setup.cmd again.
echo.
pause
exit /b 1

:no_template
echo [x] env.template is missing; the package looks incomplete.
echo.
pause
exit /b 1

REM ---- helper: set PYEXE if given command is Python >= 3.12 ----
REM Store sys.executable (an absolute python.exe path) so later quoting is safe
REM even when the launcher was "py -3.12".
:check
set "CHK="
for /f "delims=" %%v in ('%* -c "import sys;print('.'.join(map(str,sys.version_info[:2])))" 2^>nul') do set "CHK=%%v"
if not defined CHK exit /b 0
for /f "tokens=1,2 delims=." %%a in ("%CHK%") do (
  if %%a GEQ 3 if %%b GEQ 12 (
    for /f "delims=" %%e in ('%* -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%e"
  )
)
exit /b 0
