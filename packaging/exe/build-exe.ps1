<#
packaging/exe/build-exe.ps1
------------------------------------------------------------
在【开发机】上把 Trace 打成单文件 Trace.exe 部署包（一个 .zip）。

用法（仓库根目录）：
  powershell -ExecutionPolicy Bypass -File packaging\exe\build-exe.ps1

可选参数：
  -OutDir    输出目录（默认 %USERPROFILE%\Desktop）
  -NoBuild   不重建前端（直接用现有 web\dist）

产物：Trace-win64-exe-<版本>-<时间>.zip
     内含：Trace.exe / start.cmd / stop.cmd / env.template / README.txt
目标机：无需安装 Python / 依赖，双击 start.cmd 即可运行。

前置：构建机需有可用的 .venv 且已安装 pyinstaller
     （pip install pyinstaller）。Python 建议 3.12/3.13。
#>
param(
  [string]$OutDir = [Environment]::GetFolderPath('Desktop'),
  [switch]$NoBuild
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Here = $PSScriptRoot
$Root = (Resolve-Path (Join-Path $Here '..\..')).Path
$Py   = Join-Path $Root '.venv\Scripts\python.exe'
Write-Host "[exe] 仓库根: $Root"

if (-not (Test-Path $Py)) { throw "缺少 .venv\Scripts\python.exe，请先在仓库根建好虚拟环境并安装依赖" }
& $Py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { throw "未安装 pyinstaller：请执行 .venv\Scripts\python.exe -m pip install pyinstaller" }

# ---------- 1. 前端 dist ----------
$DistDir = Join-Path $Root 'web\dist'
if (-not $NoBuild) {
  Write-Host '[exe] 构建前端（npm build）…'
  Push-Location (Join-Path $Root 'frontend')
  try {
    if (-not (Test-Path 'node_modules')) { npm ci }
    npm run build
  } finally { Pop-Location }
}
if (-not (Test-Path (Join-Path $DistDir 'index.html'))) {
  throw '缺少 web\dist\index.html（前端未构建；去掉 -NoBuild 或先 npm run build）'
}

# ---------- 2. PyInstaller ----------
Write-Host '[exe] 运行 PyInstaller（首次较慢，约 1~3 分钟）…'
$DistOut = Join-Path $Root 'dist_exe'
$WorkOut = Join-Path $Root 'build\pyinstaller'
& $Py -m PyInstaller (Join-Path $Here 'Trace.spec') --noconfirm --distpath $DistOut --workpath $WorkOut
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败' }
$Exe = Join-Path $DistOut 'Trace.exe'
if (-not (Test-Path $Exe)) { throw "未生成 $Exe" }

# ---------- 3. 组装 payload ----------
$Stage = Join-Path $Here 'payload'
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Stage | Out-Null
Copy-Item $Exe (Join-Path $Stage 'Trace.exe') -Force
foreach ($f in @('start.cmd','stop.cmd','README.txt')) {
  Copy-Item (Join-Path $Here $f) (Join-Path $Stage $f) -Force
}
$envEx = Join-Path $Root '.env.example'
if (-not (Test-Path $envEx)) { throw '缺少 .env.example，无法生成 env.template' }
Copy-Item $envEx (Join-Path $Stage 'env.template') -Force

# ---------- 4. 版本号 ----------
$ver = '0.3.1'
try {
  $m = Select-String -Path (Join-Path $Root 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
  if ($m -and $m.Matches[0].Groups[1].Value) { $ver = $m.Matches[0].Groups[1].Value }
} catch {}

# ---------- 5. 打 zip ----------
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$zip = Join-Path $OutDir ("Trace-win64-exe-{0}-{1}.zip" -f $ver, $stamp)
if (Test-Path $zip) { Remove-Item $zip -Force }
Write-Host "[exe] 打包 -> $zip"
Compress-Archive -Path (Join-Path $Stage '*') -DestinationPath $zip -CompressionLevel Optimal

$size = '{0:N1} MB' -f ((Get-Item $zip).Length / 1MB)
Write-Host ''
Write-Host '================ 打包完成 ================'
Write-Host ("  产物: {0}" -f $zip)
Write-Host ("  版本: {0}   包大小: {1}" -f $ver, $size)
Write-Host '  目标机步骤：解压 -> 双击 start.cmd（首次自动生成 .env）'
Write-Host '========================================='
