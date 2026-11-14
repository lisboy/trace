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
# 旧 Trace.exe 若正在运行会锁住 dist_exe\Trace.exe，覆盖时报 WinError 5。
# 不自动杀进程（可能正跑着用户的任务），明确报错让用户先 stop.cmd。
if (Get-Process -Name 'Trace' -ErrorAction SilentlyContinue) {
  throw '检测到 Trace.exe 正在运行，它会锁住 dist_exe 产物。请先双击 stop.cmd 停止后再打包。'
}

Write-Host '[exe] 运行 PyInstaller（首次较慢，约 1~3 分钟）…'
$DistOut = Join-Path $Root 'dist_exe'
$WorkOut = Join-Path $Root 'build\pyinstaller'
# 输出到临时目录，成功后再复制到 dist_exe\Trace.exe：
# 避免 PyInstaller 自己删除旧产物时因文件被占用（杀软扫描 / 资源管理器预览）半路失败。
$DistTmp = Join-Path $Root 'build\pyinstaller_dist_tmp'
try {
  if (Test-Path $DistTmp) { Remove-Item $DistTmp -Recurse -Force -ErrorAction Stop }
} catch {
  $DistTmp = Join-Path $Root ('build\pyinstaller_dist_tmp_{0}' -f (Get-Date -Format 'HHmmss'))
}
& $Py -m PyInstaller (Join-Path $Here 'Trace.spec') --noconfirm --distpath $DistTmp --workpath $WorkOut
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败' }
$ExeNew = Join-Path $DistTmp 'Trace.exe'
if (-not (Test-Path $ExeNew)) { throw "未生成 $ExeNew" }

# 落盘到 dist_exe\Trace.exe：用 Move（同盘改名）而非 Copy。
# Copy 要读取源文件内容，会被杀软实时扫描短暂锁住而反复 WinError 5；
# Move 只改目录项、不读内容，基本不会失败。最后再加"旧文件改名挪开"兜底。
if (-not (Test-Path $DistOut)) { New-Item -ItemType Directory -Path $DistOut -Force | Out-Null }
$Exe = Join-Path $DistOut 'Trace.exe'
$moved = $false
for ($i = 0; $i -lt 5; $i++) {
  try {
    Move-Item -LiteralPath $ExeNew -Destination $Exe -Force -ErrorAction Stop
    $moved = $true; break
  } catch {
    Write-Host "[exe] 替换 dist_exe\Trace.exe 失败（第 $($i+1) 次）：$($_.Exception.Message)；2 秒后重试…"
    Start-Sleep -Seconds 2
  }
}
if (-not $moved) {
  # 兜底：旧文件改名挪开（改名不读内容、可绕过杀软读锁），再移入新文件
  try { if (Test-Path $Exe) { Move-Item -LiteralPath $Exe -Destination ($Exe + '.stale') -Force -ErrorAction Stop } } catch {}
  try { Move-Item -LiteralPath $ExeNew -Destination $Exe -Force -ErrorAction Stop; $moved = $true } catch {}
}
if (-not $moved) {
  throw '无法写入 dist_exe\Trace.exe（被占用）。请关闭正在运行的 Trace.exe / 杀毒扫描 / 资源管理器预览后重试。'
}

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

# 清理临时 dist 目录（失败不影响产物）
try { if (Test-Path $DistTmp) { Remove-Item $DistTmp -Recurse -Force -ErrorAction SilentlyContinue } } catch {}

$size = '{0:N1} MB' -f ((Get-Item $zip).Length / 1MB)
Write-Host ''
Write-Host '================ 打包完成 ================'
Write-Host ("  产物: {0}" -f $zip)
Write-Host ("  版本: {0}   包大小: {1}" -f $ver, $size)
Write-Host '  目标机步骤：解压 -> 双击 start.cmd（首次自动生成 .env）'
Write-Host '========================================='
