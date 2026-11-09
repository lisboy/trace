# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 Trace 打成单文件 Trace.exe（Windows）。

用法（仓库根目录，用项目 .venv 里的 python）：
  .venv\Scripts\python.exe -m PyInstaller packaging\exe\Trace.spec --noconfirm

产物：dist\Trace.exe（单文件，自带 Python 运行时与全部依赖）。
目标机无需安装 Python / 依赖；仍需一个 .env（放在 exe 同目录）来配置 LLM。
"""
import os
import sys
from pathlib import Path

# 本 spec 位于 packaging/exe/，仓库根在上两级
ROOT = Path(SPECPATH).resolve().parent.parent  # type: ignore[name-defined]

datas = [
    (str(ROOT / "web" / "dist"), "web/dist"),          # 前端产物（main._frontend_dir 靠 __file__.parent.parent 定位）
    (str(ROOT / "examples"), "examples"),              # 示例工程（bootstrap 靠 __file__.parent.parent 定位）
]

hiddenimports = [
    "aiosqlite",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "websockets",
    "websockets.legacy",
    "watchfiles",
    "httptools",
    "multipart",
    "python_multipart",
    "email.mime.multipart",
    "argon2",
    "openpyxl",
    "docx",
    "pypdf",
    "psutil",
    "yaml",
    "dotenv",
    # 项目里用字符串动态 import 的模块，静态分析抓不到
    "game_agent.api.app",
    "game_agent.llm.anthropic_compat",
    "game_agent.llm.openai_compat",
    "game_agent.api.routers.auth",
    "game_agent.api.routers.projects",
    "game_agent.api.routers.sessions",
    "game_agent.api.routers.runs",
    "game_agent.api.routers.artifacts",
    "game_agent.api.routers.imagegen",
    "game_agent.api.routers.voicegen",
    "game_agent.api.routers.providers",
    "game_agent.api.routers.memory",
    "game_agent.api.routers.config",
    "game_agent.api.routers.skills",
    "game_agent.api.routers.prefs",
    "game_agent.api.routers.questions",
    "game_agent.api.routers.system",
    "game_agent.api.routers.ws",
    "game_agent.core.lite.container",
    "game_agent.orchestration.orchestrator",
    "game_agent.auth.service",
    "game_agent.bootstrap",
    "game_agent.db.migrate",
    # 动态 import（字符串引用），静态分析抓不到：run_command 里 grep 命令改写会用
    "game_agent.agents.tools.grep",
    "game_agent.agents.tools.native",
    "game_agent.agents.tools.service",
    # Trace.exe --reset-password 的 CLI 入口（launcher 里动态 import）
    "game_agent.cli",
]

# 这些包体积大 / 与运行无关，明确排除，减小体积并避免打包报错
excludes = [
    "tkinter", "matplotlib", "numpy", "pandas", "scipy",
    "pytest", "IPython", "notebook", "jupyter",
    "PyQt5", "PySide2", "PySide6",
]

a = Analysis(
    [str(ROOT / "packaging" / "exe" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Trace",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    # 内嵌到 exe 的图标（资源管理器/任务栏显示的就是它）
    icon=str(ROOT / "packaging" / "exe" / "Trace.ico"),
)
