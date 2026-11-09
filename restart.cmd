@echo off
REM Trace - one-click backend RESTART (pick up code changes).
REM Double-click this file. Or: restart.cmd
REM
REM What it does:
REM   1) stop the currently running backend (whole start.cmd process tree,
REM      browser and ComfyUI are left untouched)
REM   2) launch a fresh backend in a new console window (same as start.cmd run)
REM   3) wait until http://127.0.0.1:18888/health answers, then report
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0"
set "ROOT=%~dp0"

if not exist .venv (
  echo [restart] .venv not found. Run start.cmd first.
  pause
  goto :eof
)
call .venv\Scripts\activate.bat
if not exist .env ( copy .env.example .env >nul )

echo ============================================================
echo   Trace - restart backend
echo ------------------------------------------------------------
echo   1) stop current backend
echo   2) start a fresh one in a new window
echo   3) wait for /health
echo ============================================================
echo.

echo [restart] stopping current backend ^(if any^)...
python "%ROOT%scripts\restart_app.py" stop
if errorlevel 1 (
  echo [restart] WARNING: could not fully stop the old backend.
  echo           A leftover python.exe may still hold the port.
)

echo [restart] launching a fresh backend in a new window...
start "Trace backend" cmd /c ""%ROOT%start.cmd" run"

echo [restart] waiting for http://127.0.0.1:18888/health ...
python "%ROOT%scripts\restart_app.py" wait --timeout 150
if errorlevel 1 (
  echo.
  echo [restart] Backend did NOT become ready in time.
  echo           Look at the "Trace backend" window for the real error.
) else (
  echo.
  echo [restart] Done. The new backend is running; code changes are live.
)
echo.
pause
popd
endlocal
