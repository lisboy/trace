@echo off
REM 循迹（Trace） - stop launcher.
REM
REM 双击 stop.cmd        -> 停止本服务（顺带停掉本地 ComfyUI / 语音，释放显存）
REM   stop.cmd app       -> 只停本服务，不动 ComfyUI / 语音
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0"

REM 判断服务到底在不在跑（pid 文件 + 默认端口），给个明确提示再动手
set "APP_ONLY="
if /i "%~1"=="app" set "APP_ONLY=--app-only"

echo ============================================================
echo   循迹 Trace - 停止服务
echo ------------------------------------------------------------
if defined APP_ONLY (
  echo   将停止：本服务（ComfyUI / 语音保持不动）
) else (
  echo   将停止：本服务 + 本地 ComfyUI + 本地语音（一起释放显存）
)
echo ============================================================
echo.
set "GO="
set /p "GO=确认停止？输入 y 回车（其它任意键取消）："
if /i not "%GO%"=="y" goto cancel
echo.

if not exist .venv goto nopy
call .venv\Scripts\activate.bat
if defined APP_ONLY (
  python "%ROOT%scripts\stop_all.py" --app-only
) else (
  python "%ROOT%scripts\stop_all.py"
)
if errorlevel 1 goto failed
echo.
echo [trace] 已停止。可以关掉这个窗口了（start.cmd 那个窗口也会随之结束）。
pause
goto :eof

:failed
echo.
echo [trace] 停止过程有异常（见上方输出）。若提示端口仍被占用，请手动结束该 PID。
pause
goto :eof

:nopy
echo [trace] 没找到 .venv，无法运行停止脚本。
echo         可直接在任务管理器结束 python.exe，或重启电脑。
pause
goto :eof

:cancel
echo 已取消，未做任何改动。
ping -n 3 127.0.0.1 >nul
goto :eof
