<#
packaging/windows-lite/build-package.ps1
------------------------------------------------------------
在【开发机】上运行：把项目打成 Windows lite 单机部署包（一个 .zip）。

用法（在仓库根目录）：
  powershell -ExecutionPolicy Bypass -File packaging\windows-lite\build-package.ps1

可选参数：
  -OutDir  输出目录（默认 %USERPROFILE%\Desktop）
  -IncludeVenv  同时把 .venv 打进去（体积很大，仅在目标机无法联网/装依赖时才用）
  -NoBuild  不重建前端（直接用现有的 web\dist）

产物：Trace-lite-win64-<版本>-<时间>.zip
     解压后：setup.cmd / start.cmd / stop.cmd / 程序文件 / env.template / README.txt
目标机需要的内置依赖：无（若带了 .venv 则 Python 也不用装）。
目标机无需 PowerShell：双击 setup.cmd 安装、双击 start.cmd 启动、双击 stop.cmd 停止。
#>
param(
  [string]$OutDir = [Environment]::GetFolderPath('Desktop'),
  [switch]$IncludeVenv,
  [switch]$NoBuild
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# 仓库根（本脚本位于 packaging/windows-lite/，向上一层两次）
$Here     = $PSScriptRoot
$Root     = (Resolve-Path (Join-Path $Here '..\..')).Path
$Stage    = Join-Path $Here 'payload'
$PkgName  = 'Trace-lite-win64'
Write-Host "[build] 仓库根: $Root"

function Assert-Path($p, $what) {
  if (-not (Test-Path $p)) { throw "缺少必需项：$what ($p)" }
}

# ---------- 0. 前置检查 ----------
Assert-Path (Join-Path $Root 'game_agent') '后端代码 game_agent/'
Assert-Path (Join-Path $Root 'requirements.txt') 'requirements.txt'
Assert-Path (Join-Path $Root 'pyproject.toml') 'pyproject.toml'
Assert-Path (Join-Path $Root 'examples\cardgame') '示例工程 examples/cardgame/'
if (-not $NoBuild) { Assert-Path (Join-Path $Root 'frontend\package.json') '前端源码 frontend/（-NoBuild 可跳过）' }

# ---------- 1. 前端：构建 web\dist ----------
$DistDir = Join-Path $Root 'web\dist'
if (-not $NoBuild) {
  Write-Host "[build] 构建前端（npm ci + build）…"
  Push-Location (Join-Path $Root 'frontend')
  try {
    if (-not (Test-Path 'node_modules')) { npm ci } else { Write-Host '[build] 已有 node_modules，跳过 npm ci' }
    npm run build
  } finally { Pop-Location }
}
Assert-Path (Join-Path $DistDir 'index.html') 'web\dist\index.html（前端未构建；去掉 -NoBuild 或先 npm run build）'
Write-Host "[build] 前端 dist 就绪: $DistDir"

# ---------- 2. 校验 requirements（真实依赖 = requirements.txt + mcp） ----------
$ReqSrc = Get-Content (Join-Path $Root 'requirements.txt') -Encoding UTF8
if (-not ($ReqSrc -match '(?m)^\s*mcp\b')) {
  Write-Host "[build] 注意：requirements.txt 未列 mcp；安装器会额外补装 mcp（MCP 工具需要）。"
}

# ---------- 3. 清空并重建 payload ----------
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Stage | Out-Null

function Copy-Filtered($src, $dst) {
  # 复制目录，排除缓存/虚拟环境/密钥/日志/数据
  $excludeDirs  = @('__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
                    '.venv', 'venv', 'node_modules', '.git', 'run_snapshots')
  $excludeFiles = @('*.pyc', '*.pyo', '*.log', '*.db', '*.sqlite', '*.sqlite3', '.env')
  New-Item -ItemType Directory -Path $dst -Force | Out-Null
  $args = @($src, $dst, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP')
  if ($excludeDirs.Count)  { $args += '/XD'; $args += $excludeDirs }
  if ($excludeFiles.Count) { $args += '/XF'; $args += $excludeFiles }
  & robocopy @args | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy 失败 ($LASTEXITCODE): $src -> $dst" }
}
function Copy-IncludeOnly($src, $dst) {
  # 只复制指定文件名（用于 requirements/pyproject 等散文件）
  Copy-Item -LiteralPath $src -Destination $dst -Force
}

Write-Host '[build] 复制后端代码…'
Copy-Filtered (Join-Path $Root 'game_agent') (Join-Path $Stage 'game_agent')

Write-Host '[build] 复制前端 dist…'
Copy-Filtered $DistDir (Join-Path $Stage 'web\dist')

Write-Host '[build] 复制示例工程…'
Copy-Filtered (Join-Path $Root 'examples') (Join-Path $Stage 'examples')

foreach ($f in @('requirements.txt','pyproject.toml','README.md')) {
  $p = Join-Path $Root $f
  if (Test-Path $p) { Copy-IncludeOnly $p (Join-Path $Stage $f) }
}

# 可选：把本机 .venv 一起打进去（离线目标机）
if ($IncludeVenv) {
  $venv = Join-Path $Root '.venv'
  Assert-Path $venv '.venv（要 -IncludeVenv 但本机没有）'
  Write-Host '[build] 复制 .venv（体积大，稍等）…'
  Copy-Filtered $venv (Join-Path $Stage '.venv')
}

# ---------- 4. 不打包 .env，改放 env.template ----------
$envEx = Join-Path $Root '.env.example'
if (Test-Path $envEx) { Copy-IncludeOnly $envEx (Join-Path $Stage 'env.template') } else {
  throw '缺少 .env.example，无法生成 env.template'
}

# ---------- 5. 生成安装/启动/停止脚本 + README ----------
Copy-IncludeOnly (Join-Path $Here 'setup.cmd')   (Join-Path $Stage 'setup.cmd')
Copy-IncludeOnly (Join-Path $Here 'start.cmd')   (Join-Path $Stage 'start.cmd')
Copy-IncludeOnly (Join-Path $Here 'stop.cmd')    (Join-Path $Stage 'stop.cmd')
Copy-IncludeOnly (Join-Path $Here 'README.txt')  (Join-Path $Stage 'README.txt')

# 版本号
$ver = '0.3.1'
try {
  $m = Select-String -Path (Join-Path $Root 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
  if ($m -and $m.Matches[0].Groups[1].Value) { $ver = $m.Matches[0].Groups[1].Value }
} catch {}

# ---------- 6. 打 zip ----------
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$zip = Join-Path $OutDir ("{0}-{1}-{2}.zip" -f $PkgName, $ver, $stamp)
if (Test-Path $zip) { Remove-Item $zip -Force }
Write-Host "[build] 打包 -> $zip"
Compress-Archive -Path (Join-Path $Stage '*') -DestinationPath $zip -CompressionLevel Optimal

$size = '{0:N1} MB' -f ((Get-Item $zip).Length / 1MB)
Write-Host ''
Write-Host '================ 打包完成 ================'
Write-Host ("  产物: {0}" -f $zip)
Write-Host ("  版本: {0}   大小: {1}" -f $ver, $size)
Write-Host '  目标机步骤：解压 -> 双击 setup.cmd（自动装依赖）-> 改 .env -> 双击 start.cmd'
Write-Host '========================================='
