"""run 终态通知：把「这次为什么没有结果」写进会话消息流。

背景（bug「多点几下对话内容就空白了」的根因之一）：
有些路径只改了 `runs.status`，却没在会话里留下任何消息——
  * 用户取消排队中的 run（api/routers/runs.py cancel_run）
  * 服务重启清理僵尸 run（main.py 启动清理）
  * 看门狗把卡死的 run 标失败（main.py stale_watchdog）
这些 run 会话里只剩用户那一条消息，聊天页看起来就是「对话内容空白」，
用户完全不知道发生了什么。这里统一补一条 system 消息并广播 message_new，
保证任何终态都能在聊天流里看到原因（历史数据由前端兜底提示，见 Chat.tsx）。
"""
from __future__ import annotations

import logging
from typing import Any

from game_agent.core.abstractions import EventEnvelope

log = logging.getLogger(__name__)


async def post_session_notice(
    *,
    session_id: Any,
    run_id: Any,
    team_id: Any,
    project_id: Any,
    text: str,
) -> None:
    """在会话里落一条系统提示并广播（无会话/session 已删/写库失败都不抛错）。"""
    if not session_id:
        return
    # 延迟导入：main.py 启动阶段即调用，避免模块级循环 import
    from game_agent.api.state import state

    services = getattr(state, "services", None)
    if services is None:
        return
    body = (text or "").strip() or "本次执行已结束"
    try:
        msg = await services.repo.create_message(
            session_id=session_id, run_id=run_id,
            author_type="system", author_id=None, agent_key=None,
            content=body[:500], meta_json={})
    except Exception:  # noqa: BLE001 - 通知失败不能影响业务（取消/清理要照常完成）
        log.exception("写入 run 终态提示消息失败", extra={"run_id": str(run_id)})
        return
    try:
        await services.bus.emit(EventEnvelope(
            team_id=str(team_id) if team_id else "",
            project_id=str(project_id) if project_id else None,
            run_id=str(run_id) if run_id else None,
            type="message_new",
            payload={"session_id": str(session_id), "message_id": str(msg.id)},
        ))
    except Exception:  # noqa: BLE001
        log.exception("广播 run 终态提示消息失败", extra={"run_id": str(run_id)})
