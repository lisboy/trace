"""Windows 启动脚本（.cmd / .ps1）的编码回归测试。

cmd.exe 是按**控制台代码页**读 .cmd 字节的（中文 Windows = 936/GBK），它不认 UTF-8。
一旦这些文件被编辑器按 UTF-8 存过，含中文的行就会在 cp936 控制台上显示成「锟斤拷」，
更糟的是被替换成 U+FFFD 的字节会吃掉行尾 \\r，两条 echo 粘成一行、后半截被当成命令执行
（实测报「'xxx' 不是内部或外部命令」）。PowerShell 5.1 读无 BOM 的 .ps1 同理按 ANSI，
所以带中文的 .ps1 必须带 BOM。这些都没法靠运行时兜住，只能在文件层面钉住。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# 用户/发行包里会双击启动的脚本（packaging/*/payload 是构建中间产物，由构建脚本复制，不在此列）
CMD_FILES = [
    "start.cmd",
    "stop.cmd",
    "restart.cmd",
    "package.cmd",
    "scripts/dev.cmd",
    "packaging/exe/start.cmd",
    "packaging/exe/stop.cmd",
    "packaging/windows-lite/start.cmd",
    "packaging/windows-lite/stop.cmd",
    "packaging/windows-lite/setup.cmd",
]


def _has_fffd(raw: bytes) -> bool:
    return b"\xef\xbf\xbd" in raw  # 落盘成 UTF-8 的 U+FFFD


@pytest.mark.parametrize("rel", CMD_FILES)
def test_cmd_file_is_gbk_decodable_and_has_no_replacement_char(rel: str) -> None:
    """每个 .cmd 都必须能被 GBK 严格解码，且不含 U+FFFD（乱码已被写死）。"""
    path = ROOT / rel
    assert path.exists(), f"{rel} 不存在"
    raw = path.read_bytes()

    assert not _has_fffd(raw), (
        f"{rel} 里含 U+FFFD（字节 EF BF BD）：文件被按 UTF-8 存坏过，"
        "中文会在 cmd 控制台显示成「锟斤拷」。请用 GBK 重新保存。"
    )
    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError as exc:  # noqa: TRY003
        raise AssertionError(
            f"{rel} 不能用 GBK 严格解码（{exc}）：cmd.exe 在中文 Windows 上按 cp936 读字节，"
            "非 GBK 内容会显示为乱码。请用 GBK 保存该文件。"
        ) from exc

    # 行尾必须是 CRLF（只有 LF 的话，某些编辑器/工具会再改一次编码）
    assert b"\r\n" in raw, f"{rel} 应使用 CRLF 行尾"
    lone_lf = raw.count(b"\n") - raw.count(b"\r\n")
    assert lone_lf == 0, f"{rel} 有 {lone_lf} 个裸 LF，应为纯 CRLF"


@pytest.mark.parametrize("rel", CMD_FILES)
def test_cmd_file_has_no_utf8_bom(rel: str) -> None:
    """带 BOM 的 .cmd 会让首行变成「@echo off」——BOM 不被识别，直接当命令执行并报错。"""
    raw = (ROOT / rel).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 带了 UTF-8 BOM，cmd 会报错"


def test_run_ps1_is_utf8_with_bom() -> None:
    """PowerShell 5.1 读无 BOM 的 .ps1 会按 ANSI（GBK）解码，含中文注释就变乱码。"""
    raw = (ROOT / "run.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "run.ps1 缺少 UTF-8 BOM"
    # 去掉 BOM 后必须能按 UTF-8 严格解，且不能有替换符
    body = raw[3:]
    assert not _has_fffd(body), "run.ps1 含 U+FFFD，注释已损坏"
    body.decode("utf-8")  # 解不开就直接抛


def test_shell_scripts_are_utf8_without_bom() -> None:
    """.sh 由 bash 读，Linux 那边按 UTF-8，不能带 BOM（#! 必须是文件最前面几个字节）。"""
    for rel in ("start.sh", "deploy/unix/start.sh", "deploy/unix/stop.sh", "deploy/unix/backup.sh"):
        path = ROOT / rel
        if not path.exists():
            continue
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 不能带 BOM（会破坏 shebang）"
        assert not _has_fffd(raw), f"{rel} 含 U+FFFD"
        raw.decode("utf-8")
