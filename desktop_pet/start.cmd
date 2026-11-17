@echo off
chcp 65001 >nul
rem 循迹桌宠启动脚本（Windows）
rem 首次运行会自动装依赖；之后直接起窗。
cd /d "%~dp0"

if not exist "node_modules\electron\dist\electron.exe" (
  echo [pet] 未检测到 Electron，正在安装依赖……
  call npm install
  if errorlevel 1 (
    echo [pet] 依赖安装失败，请检查网络或 npm 镜像后重试。
    pause
    exit /b 1
  )
)

echo [pet] 启动循迹桌宠……右键宠物可打开菜单，菜单里可退出。
start "" "node_modules\electron\dist\electron.exe" .
