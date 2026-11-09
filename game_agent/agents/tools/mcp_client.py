"""MCP 桥接（Model Context Protocol）：项目级配置 → 动态工具（命名 mcp_<server>_<tool>），
白名单与 spec 在 run 启动时合并。

配置来源优先级：
1. 数据库 mcp_servers 表（配置界面 CRUD 管理，项目级）
2. 工作区 .trace/mcp.json 文件（兼容手工配置，与 Claude/Cursor 生态同格式）：
{
  "mcpServers": {
    "fs":      {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-fs", "./"]},
    "remote":  {"url": "https://example.com/mcp"}
  }
}

连接生命周期：进程级懒连接 + 保活（AsyncExitStack），异常时丢弃会话下次重连。
"""
from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from game_agent.logging_setup import get_logger

log = get_logger("mcp")

_MANAGERS: dict[str, "McpManager"] = {}

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


async def get_mcp_manager(project_id, workspace) -> Optional["McpManager"]:
    """按项目取 MCP 管理器：DB 配置优先，无则回退 .trace/mcp.json；都没有返回 None。
    结果按 (project_id, 文件指纹) 缓存——同一项目多次 run 不重复加载。"""
    from game_agent.db.models import McpServer
    from game_agent.db.session import SessionLocal
    from sqlalchemy import select

    servers: dict[str, dict] = {}
    try:
        async with SessionLocal() as s:
            rows = (await s.execute(
                select(McpServer).where(
                    McpServer.project_id == _to_uuid(project_id),
                    McpServer.enabled.is_(True),
                ).order_by(McpServer.created_at))).scalars().all()
        for m in rows:
            if _NAME_RE.match(m.name or ""):
                servers[m.name] = {
                    "command": m.command or "", "args": json.loads(m.args_json or "[]"),
                    "env": json.loads(m.env_json or "{}"), "url": m.url or "",
                }
    except Exception:  # noqa: BLE001 - DB 不可用回退文件配置
        log.exception("读取 MCP 配置失败，尝试 .trace/mcp.json")

    if not servers:
        cfg_path = Path(workspace) / ".trace" / "mcp.json"
        if cfg_path.is_file():
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                servers = {k: v for k, v in (raw.get("mcpServers") or {}).items()
                           if _NAME_RE.match(k)}
            except Exception:  # noqa: BLE001
                log.warning("mcp.json 解析失败", extra={"path": str(cfg_path)})

    if not servers:
        return None
    key = str(project_id or workspace)
    # 配置有变化（界面改过）则重建 manager；旧连接随旧实例交给 GC
    if key not in _MANAGERS or _MANAGERS[key].servers != servers:
        _MANAGERS[key] = McpManager(servers)
    return _MANAGERS[key]


def _to_uuid(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, uuid.UUID):
        return v
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        return v


class McpManager:
    """管理一个工作区声明的全部 MCP 服务器：list_tools / call。"""

    def __init__(self, servers: dict[str, dict]) -> None:
        self.servers = servers
        # server -> (session, exit_stack)
        self._sessions: dict[str, tuple[Any, AsyncExitStack]] = {}

    async def _get_session(self, server: str):  # type: ignore[no-untyped-def]
        if server in self._sessions:
            return self._sessions[server][0]
        cfg = self.servers[server] or {}
        from mcp import ClientSession, StdioServerParameters
        stack = AsyncExitStack()
        try:
            if cfg.get("url"):
                from mcp.client.streamable_http import streamablehttp_client
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(cfg["url"]))
            else:
                params = StdioServerParameters(
                    command=cfg["command"],
                    args=[str(a) for a in (cfg.get("args") or [])],
                    env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()} or None,
                )
                from mcp.client.stdio import stdio_client
                read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[server] = (session, stack)
            return session
        except Exception:
            await stack.aclose()
            raise

    async def list_tools(self) -> list[dict]:
        """返回全部可用动态工具：name=mcp_<server>_<tool>、description、schema。"""
        out: list[dict] = []
        for server in self.servers:
            try:
                session = await self._get_session(server)
                res = await session.list_tools()
            except Exception as e:  # noqa: BLE001 - 单个 server 挂了不影响其他
                log.warning("mcp server %s 不可用: %s", server, e)
                self._drop(server)
                continue
            for tl in res.tools or []:
                out.append({
                    "name": f"mcp_{server}_{tl.name}",
                    "server": server,
                    "tool": tl.name,
                    "description": f"[MCP:{server}] {tl.description or tl.name}",
                    "schema": tl.inputSchema or {"type": "object", "properties": {}},
                })
        return out

    async def call(self, prefixed_name: str, args: dict) -> str:
        server, tool = self._split(prefixed_name)
        try:
            session = await self._get_session(server)
            res = await session.call_tool(tool, args or {})
        except Exception as e:  # noqa: BLE001 - 连接坏掉丢弃重连
            self._drop(server)
            raise RuntimeError(f"MCP {server} 调用失败（已重置连接，可重试）：{e}") from e
        texts: list[str] = []
        for c in (res.content or []):
            t = getattr(c, "type", "")
            if t == "text":
                texts.append(c.text)
            elif t == "image":
                texts.append("(返回了一张图片，已忽略——如需查看请让对应工具落盘)")
            else:
                texts.append(str(c))
        out = "\n".join(x for x in texts if x) or "(空结果)"
        if getattr(res, "isError", False):
            raise RuntimeError(out[:2000])
        return out

    def _split(self, prefixed: str) -> tuple[str, str]:
        for server in self.servers:
            p = f"mcp_{server}_"
            if prefixed.startswith(p):
                return server, prefixed[len(p):]
        raise RuntimeError(f"未知 MCP 工具：{prefixed}")

    def _drop(self, server: str) -> None:
        pair = self._sessions.pop(server, None)
        if pair is not None:
            try:
                pair[1].__exit__  # noqa: B018 - 确保 stack 存在
            except Exception:  # noqa: BLE001
                pass
            # 异步关闭交由事件循环 best-effort
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                loop.create_task(pair[1].aclose())
            except Exception:  # noqa: BLE001
                pass
