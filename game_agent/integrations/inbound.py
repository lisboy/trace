"""入站回调：把 IM 里的消息变成平台里的一次对话。

两条链路：

1. **扫码绑定**
   二维码内容是一段带一次性 token 的链接。用户在钉钉/企微里扫码（或把链接发给机器人）后，
   通道把「绑定事件」推给 ``/api/channels/{channel}/callback``。我们校验 token、
   取出通道侧身份（unionId/userid），落一条 channel_links 绑定，并立刻回一条欢迎消息。
   这也是唯一不需要用户手填 ID 就能把账号挂上来的方式（钉钉扫码登录 / 企微静默授权）。

2. **回复消息 = 继续对话**
   绑定且 inbound_enabled 的通道，用户直接给机器人发文字，就等于在平台上开一个新话题：
   落一条 user 消息 → 建 run → 入队（与 ``POST /api/projects/{id}/sessions/{sid}/messages``
   完全同一条路径）。同一个链接复用 last_session_id，所以「再问一句」是接着聊而不是新开会话。

为什么入站要自己建 run 而不是回调前端：IM 用户此刻不在浏览器前，任务必须由服务端跑完，
结果再推到 IM 里。

签名校验：钉钉用 ``X-DingTalk-Signature``（HMAC-SHA256 + 应用 appSecret，需在机器人后台
填「加密 aes_key/token」的加解密另算，这里支持最常用的「验签 + 明文 JSON」模式）。
企微用 msg_signature = sha1(sort(token, timestamp, nonce, echostr))。
两者都要求在全局配置里填了对应 Secret 才启用强制校验；没填时放行但记 warning
（本地自测/内网穿透场景常见），并在接口返回里明确告知「未验签」。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from game_agent.db.models import Message, Project, Run, Session
from game_agent.integrations import channels as ch
from game_agent.logging_setup import get_logger

log = get_logger("integrations.inbound")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _u(v):  # type: ignore[no-untyped-def]
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


# ------------------------------------------------------------------ 验签
def verify_dingtalk(headers: dict, app_secret: str | None) -> tuple[bool, str]:
    """钉钉回调验签：sign = base64(HMAC-SHA256(timestamp+"\n"+app_secret, app_secret))。

    没配 app_secret 时返回 (True, "unverified")，由调用方在响应里如实标注。
    """
    if not app_secret:
        return True, "unverified"
    ts = headers.get("timestamp") or headers.get("x-dingtalk-timestamp") or ""
    sign = headers.get("sign") or headers.get("x-dingtalk-signature") or ""
    if not ts or not sign:
        return False, "missing signature headers"
    string_to_sign = f"{ts}\n{app_secret}"
    h = hmac.new(app_secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    want = base64.b64encode(h).decode()
    return hmac.compare_digest(want, sign), "" if hmac.compare_digest(want, sign) else "signature mismatch"


def wecom_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """企微回调验签：sha1(sorted([token, timestamp, nonce, encrypt]) 拼接)。"""
    parts = sorted([token or "", timestamp or "", nonce or "", encrypt or ""])
    return hashlib.sha1("".join(parts).encode()).hexdigest()


# ------------------------------------------------------------------ 绑定落地
# 注：扫码绑定（bind_from_code + 一次性令牌）已移除。绑定改为设置页直接填群机器人
# Webhook / 应用凭证（见 api/routers/channels.py 的 bind），不需要公网地址与二维码。


# ------------------------------------------------------------------ 回复 → 新 run
async def start_run_from_message(sf: async_sessionmaker, *, link, text: str,
                                 project_id=None, session_id=None) -> dict:
    """把 IM 里的一条消息落成 user 消息 + 建 run（复用平台的消息入队路径）。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "message": "空消息"}
    async with sf() as s:
        sess = None
        if session_id:
            sess = await s.get(Session, _u(session_id))
        if sess is None and link.last_session_id:
            sess = await s.get(Session, link.last_session_id)
        if sess is not None and sess.status != "active":
            sess = None
        pid = project_id or link.default_project_id
        if sess is None:
            if pid is None:
                pid = await s.scalar(select(Project.id).where(
                    Project.team_id == link.team_id,
                    Project.archived_at.is_(None)).limit(1))
            if pid is None:
                return {"ok": False, "message": "还没有可用的项目，请先在网页端建一个项目"}
            title = text[:40].replace("\n", " ")
            sess = Session(team_id=link.team_id, project_id=pid,
                           title=f"[{ch.channel_meta(link.channel)['label']}] {title}",
                           created_by=link.user_id)
            s.add(sess)
            await s.flush()
        msg = Message(session_id=sess.id, run_id=None, author_type="user",
                      author_id=link.user_id, content=text,
                      meta_json={"source": link.channel, "via": "inbound"})
        s.add(msg)
        await s.flush()
        await s.refresh(msg)
        run = Run(team_id=link.team_id, project_id=sess.project_id, session_id=sess.id,
                  triggered_by=link.user_id, trigger_msg_id=msg.id, run_type="modify",
                  options_json={"caps": {"test": True, "web": True, "media": True},
                                "source_channel": link.channel,
                                "permission_scope": sess.permission_scope or "workspace"},
                  status="queued")
        s.add(run)
        # 记住这条会话：下次在同一个 IM 里发消息就接着聊
        from game_agent.db.models import ChannelLink
        l = await s.get(ChannelLink, link.id)
        if l is not None:
            l.last_session_id = sess.id
        await s.commit()
        await s.refresh(run)
        return {"ok": True, "run_id": str(run.id), "session_id": str(sess.id)}


async def answer_pending_question(sf: async_sessionmaker, *, link, text: str,
                                  state_obj=None) -> Optional[dict]:
    """IM 里回复时，如果该会话正卡在「等你回答」上，就把这条当答案续跑。

    优先走「同 run 内暂停」的 future（与 web 端作答完全一致），拿不到就走
    落消息 + 新建 run 的回退路径。
    """
    async with sf() as s:
        if not link.last_session_id:
            return None
        from game_agent.db.models import RunQuestion
        q = await s.scalar(select(RunQuestion).where(
            RunQuestion.session_id == link.last_session_id,
            RunQuestion.status == "pending").order_by(
            RunQuestion.created_at.desc()).limit(1))
        if q is None:
            return None
        opts = q.options_json or []
        idx: int | None = None
        t = (text or "").strip()
        # 「1」/「A」这类选择也要认，否则群里回个数字等于答非所问
        if t.isdigit() and 1 <= int(t) <= len(opts):
            idx = int(t) - 1
        elif len(t) == 1 and t.isalpha() and len(opts):
            i = ord(t.upper()) - ord("A")
            if 0 <= i < len(opts):
                idx = i
        q.status = "answered" if idx is not None else "answered"
        q.answer_index = idx
        q.answer_text = (str(opts[idx].get("label") or "") if idx is not None else t)
        q.answered_at = _now()
        msg = Message(session_id=q.session_id, run_id=q.run_id, author_type="user",
                      author_id=link.user_id, content=q.answer_text or t,
                      meta_json={"kind": "answer", "question_id": str(q.id),
                                 "option_index": idx, "source": link.channel})
        s.add(msg)
        await s.flush()
        await s.refresh(msg)
        run_id = str(q.run_id) if q.run_id else None
        session_id = str(q.session_id)
        await s.commit()
        qid = str(q.id)
        answer = q.answer_text or t
    # 唤醒在途挂起的 run
    fut = None
    try:
        orch = getattr(state_obj, "orchestrator", None) if state_obj else None
        if orch is not None:
            fut = orch.take_question(qid)
    except Exception:  # noqa: BLE001
        fut = None
    if fut is not None:
        try:
            fut.set_result(answer)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "inline": True, "run_id": run_id, "session_id": session_id}
    queue = getattr(getattr(state_obj, "services", None), "queue", None) if state_obj else None
    if queue is not None and run_id:
        await queue.enqueue(run_id, priority=0)
    return {"ok": True, "inline": False, "run_id": run_id, "session_id": session_id}


# ------------------------------------------------------------------ 各通道回调解释
def _dingtalk_identity(payload: dict) -> tuple[str, str | None, dict]:
    """从钉钉回调里挖出 (身份标识, 昵称, 可复用的凭证)。"""
    sender = payload.get("senderStaffId") or payload.get("senderId") or ""
    nick = payload.get("senderNick") or ""
    cred: dict[str, Any] = {}
    if payload.get("sessionWebhook"):
        cred["session_webhook"] = payload["sessionWebhook"]
    if payload.get("sessionWebhookExpiredTime"):
        cred["session_webhook_expire"] = payload["sessionWebhookExpiredTime"]
    return str(sender), (nick or None), cred


async def handle_dingtalk(sf: async_sessionmaker, payload: dict, headers: dict,
                          app_cfg: dict, state_obj=None) -> dict:
    ok, why = verify_dingtalk(headers, app_cfg.get("app_secret"))
    if not ok:
        return {"ok": False, "message": f"验签失败：{why}"}
    external_id, nick, cred = _dingtalk_identity(payload)
    text = (payload.get("text") or {}).get("content") or payload.get("content") or ""
    text = str(text).strip()
    token = payload.get("link_token") or payload.get("token") or ""
    # 绑定事件：钉钉扫码后打开的 H5/机器人消息会把一次性 token 带过来
    if token:
        r = await bind_from_code(sf, channel="dingtalk", token=str(token),
                                 external_id=external_id, display_name=nick, credentials=cred)
        return {"ok": r["ok"], "message": r["message"], "verified": why}
    link = await ch.find_by_external(sf, "dingtalk", external_id)
    if link is None:
        return {"ok": False, "verified": why,
                "message": "未绑定。请在网页端「设置 → 消息通道」扫码关联钉钉后再发消息。"}
    if not link.inbound_enabled:
        return {"ok": False, "verified": why, "message": "该绑定未开启「可回复」"}
    answered = await answer_pending_question(sf, link=link, text=text, state_obj=state_obj)
    if answered:
        return {"ok": True, "verified": why, "answered_question": True, **answered}
    r = await start_run_from_message(sf, link=link, text=text)
    return {"ok": r["ok"], "verified": why, **r}


async def handle_wecom(sf: async_sessionmaker, payload: dict, headers: dict,
                       app_cfg: dict, state_obj=None) -> dict:
    ext = str(payload.get("FromUserName") or payload.get("from_user") or "")
    nick = payload.get("from_name") or None
    text = payload.get("Content") or payload.get("text") or ""
    token = payload.get("link_token") or ""
    if token:
        r = await bind_from_code(sf, channel="wecom", token=str(token),
                                 external_id=ext, display_name=nick)
        return {"ok": r["ok"], "message": r["message"]}
    link = await ch.find_by_external(sf, "wecom", ext)
    if link is None:
        return {"ok": False, "message": "未绑定。请在网页端扫码关联企业微信。"}
    answered = await answer_pending_question(sf, link=link, text=str(text), state_obj=state_obj)
    if answered:
        return {"ok": True, "answered_question": True, **answered}
    return await start_run_from_message(sf, link=link, text=str(text))


async def handle_wechat(sf: async_sessionmaker, payload: dict, state_obj=None) -> dict:
    """个人微信：只支持「用户自备网关」推过来，平台侧不做官方对接。"""
    ext = str(payload.get("openid") or payload.get("FromUserName") or "")
    text = str(payload.get("text") or payload.get("Content") or "")
    token = payload.get("link_token") or ""
    if token:
        r = await bind_from_code(sf, channel="wechat", token=str(token),
                                 external_id=ext, display_name=payload.get("nickname"))
        return {"ok": r["ok"], "message": r["message"]}
    link = await ch.find_by_external(sf, "wechat", ext)
    if link is None:
        return {"ok": False, "message": "未绑定。微信个人号需要自备网关并完成扫码绑定。"}
    answered = await answer_pending_question(sf, link=link, text=text, state_obj=state_obj)
    if answered:
        return {"ok": True, "answered_question": True, **answered}
    return await start_run_from_message(sf, link=link, text=text)


HANDLERS = {
    "dingtalk": handle_dingtalk,
    "wecom": handle_wecom,
    "wechat": handle_wechat,
}
