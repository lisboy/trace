"""本地 ComfyUI 客户端必须绕过系统代理的回归测试。

2026-11-06 用户报「软件提示探测模型失败」，根因：Windows 上 httpx 默认 trust_env=True
会读取 Internet 设置(LAN 代理)，本机开着代理(127.0.0.1:12334)时，发往 127.0.0.1:8588 的
请求被转给代理 → ConnectError「All connection attempts failed」→ 界面「探测 ComfyUI 可用
模型失败」。出图/视频链路漏了 trust_env=False（列表/探活早已有），这里锁死不许再漏。

只约束「本地服务」客户端（ComfyUI）。访问外部 LLM/素材源的客户端保持默认（允许走代理），
不在此约束范围内。
"""
from __future__ import annotations

import ast
from pathlib import Path

COMFY = Path(__file__).resolve().parents[1] / "game_agent" / "assets" / "comfyui.py"


def _async_client_calls(path: Path) -> list[tuple[int, bool]]:
    """返回文件里所有 httpx.AsyncClient(...) 调用的 (行号, 是否带 trust_env=False)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # 匹配 httpx.AsyncClient(...)
        if not (isinstance(func, ast.Attribute) and func.attr == "AsyncClient"):
            continue
        has = any(
            isinstance(kw, ast.keyword) and kw.arg == "trust_env"
            and isinstance(kw.value, ast.Constant) and kw.value.value is False
            for kw in node.keywords
        )
        out.append((node.lineno, has))
    return out


def test_all_comfyui_clients_bypass_system_proxy():
    calls = _async_client_calls(COMFY)
    assert calls, "未找到任何 httpx.AsyncClient 调用，测试可能失效"
    offenders = [ln for ln, has in calls if not has]
    assert not offenders, (
        f"comfyui.py 这些行的 httpx.AsyncClient 缺少 trust_env=False（本地 ComfyUI 会被"
        f"系统代理拦截导致『探测模型失败』）：行 {offenders}")
