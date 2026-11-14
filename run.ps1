# 循迹（Trace）一键启动（lite 模式 + 真实 LLM，数据落本地 SQLite）
# 用法：powershell -ExecutionPolicy Bypass -File run.ps1
# 必须先设置 GA_DATA_DIR，否则会落到 C:\Users\<你>\.trace（另一套数据目录，曾因此出过幻影脏文件）

Set-Location $PSScriptRoot
$env:GA_MODE = "lite"
$env:GA_SEED_DEMO = "1"
$env:GA_DATA_DIR = (Join-Path $PSScriptRoot ".ga-dev")

.venv\Scripts\python.exe -m game_agent.main
