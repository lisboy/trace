@echo off
REM 循迹（Trace） - lite mode one-click launcher (Windows single machine / small team).
REM
REM 用法：
REM   双击 start.cmd        -> 菜单（启动 / 查账号 / 重置密码 / 停止 / 退出）
REM   start.cmd run         -> 直接启动（不弹菜单，供快捷方式/开机自启调用）
REM   start.cmd users       -> 列出所有账号后退出
REM   start.cmd resetpass   -> 重置某个账号的密码后退出
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0"
set "ROOT=%~dp0"

if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate.bat
if not exist .env ( copy .env.example .env >nul )

REM ---- 解析子命令；双击（无参数）进菜单 ----
set "ACTION=%~1"
if "%ACTION%"=="" goto menu
if /i "%ACTION%"=="run" goto run
if /i "%ACTION%"=="users" goto users
if /i "%ACTION%"=="resetpass" goto resetpass
if /i "%ACTION%"=="stop" goto dobatchstop
goto menu

:menu
cls
echo ============================================================
echo   循迹 Trace
echo ------------------------------------------------------------
echo   [1] 启动服务（就绪后自动打开浏览器）
echo   [2] 查看已有账号（忘记用哪个邮箱登录时）
echo   [3] 重置账号密码（忘记密码时，不需要旧密码）
echo   [4] 停止服务（关掉正在运行的本服务）
echo   [0] 退出
echo ============================================================
echo.
set "CHOICE="
set /p "CHOICE=请输入数字后回车："
if "%CHOICE%"=="1" goto run
if "%CHOICE%"=="2" goto users
if "%CHOICE%"=="3" goto resetpass
if "%CHOICE%"=="4" goto dobatchstop
if "%CHOICE%"=="0" goto end
goto menu

:run
REM 确保本地 MySQL 已启动
call :ensure_mysql
echo.
echo [trace] 正在启动服务，就绪后会自动打开浏览器...
echo         端口默认 18888；若被占用或落在系统保留段会自动顺延，日志会打印实际端口。
echo         关闭本窗口 = 停止服务。
echo.
set GA_MODE=lite
set GA_SEED_DEMO=1
set GA_OPEN_BROWSER=1
python -m game_agent.main
echo.
echo [trace] 服务已停止。
pause
goto :eof

:users
echo.
python "%ROOT%scripts\reset_password.py" --list
pause
goto :eof

:resetpass
echo.
python "%ROOT%scripts\reset_password.py"
goto menu

:dobatchstop
REM 调起同目录的 stop.cmd（它会再确认一次，然后停服务）
if exist "%ROOT%stop.cmd" (
  call "%ROOT%stop.cmd"
) else (
  echo [trace] 未找到 stop.cmd，改用内置停止逻辑。
  python "%ROOT%scripts\stop_all.py"
  pause
)
goto menu

:ensure_mysql
if not exist "scripts\ensure_mysql.ps1" goto :eof
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\ensure_mysql.ps1"
if errorlevel 2 echo [mysql] WARNING: MySQL could not be started automatically.
if errorlevel 2 echo [mysql] Open an elevated prompt and run:  net start MySQL80
goto :eof

:end
popd
endlocal
