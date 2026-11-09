"""grep / glob 的搜索行为与剪枝（「项目过大后搜索特别慢」优化后的回归护栏）。

覆盖点：
- 噪音目录（node_modules/构建产物/.ga/.ga-dev/.trace）在遍历前就被剪掉，
  不进入逐个读取——这是此前扫描近 2GB 卡死的根因。
- 受保护目录（.git 等）不返回。
- 二进制后缀跳过；真实文本仍能命中；context / include / path 子目录照常工作。
- glob `**` 语义与 docstring 一致，且同样跳过噪音目录。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock

from game_agent.agents.tools import native


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "app.py").write_text("def main():\n    return NEEDLE\n", encoding="utf-8")
    (ws / "src" / "util.ts").write_text("// NEEDLE in ts\nexport const x = 1\n", encoding="utf-8")
    (ws / "README.md").write_text("# NEEDLE title\n", encoding="utf-8")

    # 噪音目录：里面有 NEEDLE，但绝不能被扫到
    for noise in ("node_modules", ".ga-dev", ".ga", ".trace", "dist", "build"):
        d = ws / noise
        d.mkdir(parents=True)
        (d / "junk.txt").write_text("NEEDLE should never surface\n", encoding="utf-8")

    # 受保护目录：命中也不能返回
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("NEEDLE protected\n", encoding="utf-8")

    # 二进制后缀：直接跳过（即使内容是纯文本也按后缀剪掉）
    (ws / "blob.dll").write_text("NEEDLE binary-ext\n", encoding="utf-8")
    # 含 NUL 的无后缀文件：靠 NUL 探测判为二进制
    (ws / "rawdata").write_bytes(b"NEEDLE\x00\x00binary")
    return ws


def _ctx(ws: Path) -> MagicMock:
    t = MagicMock()
    t.workspace = ws
    return t


def test_grep_skips_noise_and_protected(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    out = native._grep_sync(ws, re.compile("NEEDLE"), 100)
    files = {ln.split(":", 1)[0] for ln in out.splitlines() if ":" in ln}
    assert "src/app.py" in files
    assert "src/util.ts" in files
    assert "README.md" in files
    # 噪音 / 受保护 / 二进制 一律不出现
    assert not any(f.startswith(("node_modules/", ".ga-dev/", ".ga/", ".trace/",
                                 "dist/", "build/", ".git/")) for f in files)
    assert "blob.dll" not in files
    assert "rawdata" not in files


def test_grep_context_include_and_subdir(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    # context=1：命中行上下各带 1 行（命中行用 `:`，上下文行用 `-`）
    out = native._grep_sync(ws, re.compile("NEEDLE"), 100, context=1)
    assert "src/app.py:1-    def main():" in out      # 上一行（上下文）
    assert "src/app.py:2:     return NEEDLE" in out  # 命中行
    # include 只搜 *.py
    out_py = native._grep_sync(ws, re.compile("NEEDLE"), 100, include="*.py")
    assert "src/app.py" in out_py and "src/util.ts" not in out_py
    # path 限定到单文件
    out_one = native._grep_sync(ws, re.compile("NEEDLE"), 100, root=ws / "README.md")
    assert "README.md" in out_one and "src/app.py" not in out_one


def test_grep_limit_caps_results(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(20):
        (ws / f"f{i}.txt").write_text("HIT\n", encoding="utf-8")
    out = native._grep_sync(ws, re.compile("HIT"), 5)
    assert "已达到上限 5 条" in out
    assert len([ln for ln in out.splitlines() if ln.startswith("f")]) == 5


def test_glob_double_star_and_prune(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    t = _ctx(ws)
    out = asyncio.run(native.glob({"pattern": "**/*.py"}, t))
    assert "src/app.py" in out.splitlines()
    # 噪音目录里的文件不出现
    out_any = asyncio.run(native.glob({"pattern": "**/*.txt"}, t))
    assert "junk.txt" not in out_any
    # 无匹配
    assert asyncio.run(native.glob({"pattern": "**/*.zzz"}, t)) == "(无匹配)"
