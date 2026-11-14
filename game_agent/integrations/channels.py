"""消息通道：绑定关系、凭证、外发派发。

通道能力矩阵（决定界面显示什么、后端能做什么）：

| 通道     | 外发（任务完成提醒）                     | 入站（在 IM 里回复继续对话）        |
|----------|------------------------------------------|--------------------------------------|
| 钉钉     | ✅ 自定义机器人 webhook（群）/ 工作通知（需企业应用）| ✅ 支持：企业应用机器人接收消息回调 |
| 企业微信 | ✅ 群机器人 webhook / 应用消息            | ✅ 支持：应用「接收消息」回调        |
| 微信个人 | ❌ 无官方开放接口（仅可选第三方网关）      | ⚠️ 仅当自备第三方网关（如 wechaty）  |
| Webhook  | ✅ 自定义 URL                            | ❌                                   |

实测提醒：钉钉「自定义机器人」的 webhook 只能在群里发消息、且必须配置加签或关键词；
单聊推送要走企业内部应用的「工作通知」(agentId + appKey/appSecret)。所以绑定钉钉时
优先存会话级 sessionWebhook（用户在群里 @ 机器人后拿到的临时地址），没有就退回应用凭证。

外发一律是「尽力而为」：失败只落 channel_messages.error，绝不影响 run 收尾。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote_plus, urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from game_agent.db.models import ChannelLink, ChannelMessage, Message, Project, Run, Session, User
from game_agent.logging_setup import get_logger

log = get_logger("integrations.channels")

# 通道标识 → 中文名 / 能力
CHANNELS: dict[str, dict[str, Any]] = {
    "dingtalk": {
        "label": "钉钉", "can_push": True, "can_inbound": True,
        "note": "填群机器人 Webhook 即可收任务提醒（零公网、免扫码）；"
                "想用单聊「工作通知」再填企业内部应用的 AppKey/AppSecret/AgentId。",
    },
    "wecom": {
        "label": "企业微信", "can_push": True, "can_inbound": True,
        "note": "填群机器人 Webhook 即可收提醒（零公网、免扫码）；"
                "定向发给某个人需再填企业 ID / 应用 Secret / AgentId。",
    },
    "wechat": {
        "label": "微信", "can_push": False, "can_inbound": True,
        "note": "个人微信没有官方开放接口，平台无法直接把提醒推到个人微信；"
                "只能自备第三方网关（如 wechaty）后填它的地址转发。",
    },
    "webhook": {
        "label": "自定义 Webhook", "can_push": True, "can_inbound": False,
        "note": "任意 POST JSON 的地址（自建服务、飞书、Slack 等），只做外发提醒。",
    },
}

_MASK = "******"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid_or_none(v):  # type: ignore[no-untyped-def]
    """Uuid 列必须吃 uuid.UUID（吃字符串会在参数绑定时 AttributeError: 'str' has no 'hex'）。"""
    if v is None:
        return None
    if isinstance(v, uuid.UUID):
        return v
    try:
        return uuid.UUID(str(v))
    except (ValueError, AttributeError, TypeError):
        return None


def channel_meta(key: str) -> dict[str, Any]:
    return CHANNELS.get(key, {"label": key, "can_push": False, "can_inbound": False,
                             "note": "未知通道"})


def all_channels() -> list[dict[str, Any]]:
    return [{"key": k, **v} for k, v in CHANNELS.items()]


# ------------------------------------------------------------------ 绑定关系
# 注：扫码绑定（一次性令牌 ChannelLinkCode / issue_code / resolve_code / consume_code）已移除。
# 绑定改为「直接填群机器人 Webhook 或应用凭证」，不再需要公网地址与二维码。
# ChannelLinkCode 表模型保留，仅为兼容老库结构，不再有写入路径。
async def upsert_link(sf: async_sessionmaker, *, team_id, user_id, channel: str,
                      external_id: str, display_name: str | None = None,
                      credentials: dict | None = None,
                      default_project_id=None, inbound_enabled: bool | None = None) -> ChannelLink:
    """建立/更新绑定。同一 (channel, external_id) 全局唯一：换人绑定时旧绑定置 revoked，
    避免「同一个人被两个账号同时推送」。"""
    if channel not in CHANNELS:
        raise ValueError(f"未知通道：{channel}")
    async with sf() as s:
        # (channel, external_id) 有唯一约束，所以「同一标识只能有一行」：
        # 换人绑定时不能新建行（会撞唯一约束），而是把已有行改归当前用户。
        others = (await s.execute(select(ChannelLink).where(
            ChannelLink.channel == channel,
            ChannelLink.external_id == external_id))).scalars().all()
        link = next((o for o in others if str(o.user_id) == str(user_id)), None)
        if link is None and others:
            link = others[0]
            link.user_id = _uuid_or_none(user_id) or link.user_id
            link.team_id = _uuid_or_none(team_id) or link.team_id
            # 换了主人就不该继承前一个人的推送凭证（否则提醒会发到别人群里）
            link.credentials_json = {}
        for o in others:
            if o is not link:
                o.status = "revoked"
                o.unlinked_at = _now()
        if link is None:
            link = ChannelLink(team_id=team_id, user_id=user_id, channel=channel,
                               external_id=external_id)
            s.add(link)
        link.display_name = display_name or link.display_name
        link.status = "active"
        link.unlinked_at = None
        if link.linked_at is None:
            link.linked_at = _now()
        if credentials:
            link.credentials_json = {**(link.credentials_json or {}), **credentials}
        if default_project_id is not None:
            link.default_project_id = default_project_id
        if inbound_enabled is not None:
            link.inbound_enabled = inbound_enabled
        elif channel_meta(channel)["can_inbound"]:
            link.inbound_enabled = True
        await s.commit()
        await s.refresh(link)
        return link


async def unlink(sf: async_sessionmaker, team_id, link_id) -> bool:
    async with sf() as s:
        link = await s.get(ChannelLink, _uuid_or_none(link_id))
        if link is None or str(link.team_id) != str(team_id):
            return False
        link.status = "unlinked"
        link.unlinked_at = _now()
        await s.commit()
        return True


async def active_links(sf: async_sessionmaker, team_id, channel: str | None = None) -> list[ChannelLink]:
    async with sf() as s:
        q = select(ChannelLink).where(ChannelLink.team_id == team_id,
                                      ChannelLink.status == "active")
        if channel:
            q = q.where(ChannelLink.channel == channel)
        return list((await s.execute(q)).scalars().all())


async def find_by_external(sf: async_sessionmaker, channel: str, external_id: str) -> Optional[ChannelLink]:
    async with sf() as s:
        return await s.scalar(select(ChannelLink).where(
            ChannelLink.channel == channel,
            ChannelLink.external_id == external_id,
            ChannelLink.status == "active"))


# ------------------------------------------------------------------ 发送实现
def _dingtalk_sign(secret: str) -> dict[str, str]:
    """按钉钉群机器人「加签」口径算 timestamp + sign，返回**未做 URL 编码**的裸串。

    踩过的坑（2026-11-14，线上表现为「测试消息能收到、任务完成收不到」）：
    这里原本是 `quote_plus(b64encode(...))` 先手工编码一次，调用处又 `urlencode()` 编码
    第二次，sign 进到 URL 里成了 `%252B...`。钉钉服务端只解码一次，拿到的还是带 % 的串，
    验签必然对不上，回 `errcode 310000 机器人发送签名不匹配`。
    迷惑点在于「发测试消息」那条链路当场能成功（那一刻 cred 里还没填加签密钥），
    只有任务跑完的自动提醒静默失败，只在 channel_messages 里留一条 failed。
    所以这里只产出裸 base64，编码统一交给调用处的 urlencode 做一次。
    """
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return {"timestamp": ts, "sign": __import__("base64").b64encode(h).decode("ascii")}


async def _post_json(url: str, payload: dict, timeout: float = 8.0) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cli:
            r = await cli.post(url, json=payload)
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {(r.text or '')[:200]}"
        # 钉钉/企微都是 200 + errcode!=0 表示失败
        try:
            j = r.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应按成功算（自建 webhook 常见）
            return True, ""
        code = j.get("errcode", j.get("code", 0))
        if code not in (0, None):
            return False, f"{code}: {j.get('errmsg') or j.get('msg') or j}"
        return True, ""
    except Exception as e:  # noqa: BLE001 - 网络异常一律记失败原因
        return False, f"{type(e).__name__}: {e}"


async def _send_dingtalk(link: ChannelLink, title: str, body: str, app_cfg: dict) -> tuple[bool, str]:
    cred = dict(link.credentials_json or {})
    # ① 会话级 sessionWebhook / 群机器人 webhook（钉钉给出的地址，1.5h 过期或长期有效）
    hook = cred.get("session_webhook") or cred.get("webhook")
    if hook:
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"### {title}\n\n{body}"},
            "at": {"isAtAll": False},
        }
        url = hook
        # 群机器人若用「加签」安全设置，必须带 timestamp+sign，否则被拒（errcode 310000）
        sign_secret = cred.get("sign_secret") or cred.get("webhook_secret")
        if sign_secret:
            qs = urlencode(_dingtalk_sign(sign_secret))
            url = f"{hook}{'&' if '?' in hook else '?'}{qs}"
        ok, err = await _post_json(url, payload)
        if ok:
            return True, ""
        # 兜底：机器人后台若选的是「自定义关键词」而不是「加签」，多带 sign 反而被判错。
        # 验签失败时用原始地址再发一次，两种安全设置都能通（否则用户只能收到测试消息、
        # 永远收不到自动提醒，且没有明显线索）。
        if sign_secret and ("310000" in err or "sign" in err.lower()):
            ok2, err2 = await _post_json(hook, payload)
            if ok2:
                log.warning("dingtalk 验签被拒，已退回不带签名的地址重发成功")
                return True, ""
            err = f"{err} / 去掉签名重试：{err2}"
        err_session = err
    else:
        err_session = "未配置群机器人 Webhook"
    # ② 企业内部应用「工作通知」：需要 appKey/appSecret/agentId（可来自全局配置）
    key = cred.get("app_key") or app_cfg.get("app_key")
    sec = cred.get("app_secret") or app_cfg.get("app_secret")
    agent_id = cred.get("agent_id") or app_cfg.get("agent_id")
    if not (key and sec and agent_id):
        return False, (f"未配置钉钉推送凭证（{err_session}）；请在设置里填「群机器人 Webhook」，"
                       f"或填 AppKey/AppSecret/AgentId 走工作通知")
    try:
        async with httpx.AsyncClient(timeout=8.0) as cli:
            r = await cli.get("https://oapi.dingtalk.com/gettoken",
                              params={"appkey": key, "appsecret": sec})
            tok = (r.json() or {}).get("access_token")
        if not tok:
            return False, f"取 access_token 失败：{(r.text or '')[:160]}"
        async with httpx.AsyncClient(timeout=8.0) as cli:
            r2 = await cli.post(
                f"https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2?access_token={tok}",
                data={"agent_id": agent_id, "userid_list": link.external_id,
                      "msg": json.dumps({"msgtype": "markdown",
                                         "markdown": {"title": title,
                                                      "text": f"### {title}\n\n{body}"}},
                                        ensure_ascii=False)})
        j = r2.json() if r2.content else {}
        if (j.get("errcode") or 0) == 0:
            return True, ""
        return False, f"{j.get('errcode')}: {j.get('errmsg')}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


async def _send_wecom(link: ChannelLink, title: str, body: str, app_cfg: dict) -> tuple[bool, str]:
    cred = dict(link.credentials_json or {})
    # 定向发给某个人：有 external_id 且不是群机器人占位标识时，优先用应用消息（能 @ 到人）
    target = (link.external_id or "")
    is_group = target.startswith("group-") or "@" in target or not target
    corp_id = cred.get("corp_id") or app_cfg.get("corp_id")
    secret = cred.get("corp_secret") or app_cfg.get("corp_secret")
    agent_id = cred.get("agent_id") or app_cfg.get("agent_id")
    if not is_group and corp_id and secret and agent_id:
        try:
            async with httpx.AsyncClient(timeout=8.0) as cli:
                r = await cli.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                                  params={"corpid": corp_id, "corpsecret": secret})
                tok = (r.json() or {}).get("access_token")
            if not tok:
                return False, f"取 access_token 失败：{(r.text or '')[:160]}"
            return await _post_json(
                f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={tok}",
                {"touser": target, "msgtype": "markdown", "agentid": int(agent_id),
                 "markdown": {"content": f"## {title}\n{body}"}})
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
    # 群机器人 webhook（零公网、免扫码，最常用）
    hook = cred.get("webhook") or cred.get("group_webhook")
    if hook:
        return await _post_json(hook, {"msgtype": "markdown",
                                       "markdown": {"content": f"## {title}\n{body}"}})
    if not (corp_id and secret and agent_id):
        return False, "未配置企微推送凭证；请填「群机器人 Webhook」，或填企业ID/应用Secret/AgentId 走应用消息"
    return False, "企业微信应用消息需要指定接收人（外部标识不能为空）"


async def _send_webhook(link: ChannelLink, title: str, body: str, app_cfg: dict) -> tuple[bool, str]:
    url = (link.credentials_json or {}).get("webhook") or (link.credentials_json or {}).get("url")
    if not url:
        return False, "未配置 webhook 地址"
    return await _post_json(url, {"title": title, "text": body,
                                  "channel": "trace", "text_type": "markdown"})


async def _send_thirdparty_wechat(link: ChannelLink, title: str, body: str, app_cfg: dict) -> tuple[bool, str]:
    """微信个人号：只能通过用户自备的第三方网关（wechaty 服务化后的 HTTP 接口等）。"""
    url = (link.credentials_json or {}).get("gateway_url")
    if not url:
        return False, "微信个人号没有官方推送接口；如已自建网关，请在绑定里填 gateway_url"
    return await _post_json(url, {"to": link.external_id, "title": title, "text": body})


_SENDERS = {
    "dingtalk": _send_dingtalk,
    "wecom": _send_wecom,
    "wechat": _send_thirdparty_wechat,
    "webhook": _send_webhook,
}


async def send_to_link(sf: async_sessionmaker, link: ChannelLink, *, kind: str,
                       title: str, body: str, run_id=None, session_id=None,
                       app_cfg: dict | None = None) -> ChannelMessage:
    """向单个绑定发一条消息，并落 channel_messages 记录（成功/失败原因都留）。"""
    app_cfg = app_cfg or {}
    sender = _SENDERS.get(link.channel)
    if sender is None:
        ok, err = False, f"未知通道 {link.channel}"
    else:
        ok, err = await sender(link, title, body, app_cfg)
    async with sf() as s:
        rec = ChannelMessage(team_id=link.team_id, link_id=link.id, channel=link.channel,
                            run_id=_uuid_or_none(run_id), session_id=_uuid_or_none(session_id),
                            kind=kind,
                            title=title[:200], body=body[:4000],
                            status="sent" if ok else "failed",
                            error=(err or "")[:2000] or None,
                            sent_at=_now() if ok else None)
        s.add(rec)
        if ok:
            l = await s.get(ChannelLink, link.id)
            if l is not None:
                l.last_notified_at = _now()
        await s.commit()
        await s.refresh(rec)
        if not ok:
            log.warning("channel send failed: %s %s", link.channel, err)
        return rec


# ------------------------------------------------------------------ 通知内容
# 单条提醒的正文上限。钉钉 markdown 单条能收的长度远大于历史最长的汇报（库内 300 条
# 汇报里最长 6098 字），所以 20000 正常永不触发；留这个数只为防「模型吐 10 万字」把
# 通道打爆，且真触发时**必须在正文里写明省略了多少**——静默腰斩过一次就够（2026-11-14）。
NOTICE_SUMMARY_LIMIT = 20000
NOTICE_BODY_LIMIT = 20000


def _short(text: str, limit: int = 500, *, note: str | None = None) -> str:
    """截断字符串。note 给了就在尾部追加显式说明（用于长正文，不能静默截断）。"""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[:limit].rstrip() + (note or "…")


def _cap_body(body: str, limit: int = NOTICE_BODY_LIMIT) -> str:
    """整条提醒的兜底上限：超长时保留头尾并在中间写明省略，绝不静默截断。"""
    b = body or ""
    if len(b) <= limit:
        return b
    head_n = int(limit * 0.7)
    tail_n = limit - head_n
    omitted = len(b) - head_n - tail_n
    return (b[:head_n].rstrip()
            + f"\n\n…（正文过长，此处省略约 {omitted} 字，完整汇报见客户端本会话）\n\n"
            + b[-tail_n:].lstrip())


async def compose_run_notice(sf: async_sessionmaker, run_id: str, status: str,
                             session_id: str | None = None) -> tuple[str, str, str]:
    """按 run 的落库结果拼通知标题+正文，返回 (kind, title, body)。

    正文要求（2026-11-14 用户明确要求）：
    1. **说清来源**：这是哪个项目、哪条会话的任务 —— 群里可能同时挂多个项目，
       只写「任务完成」用户根本分不清是哪件事。
    2. **把汇报正文发过去**：`Run.final_summary` 就是用户在本界面里看到的那段最终汇报，
       整段带上去，别截断——提醒的价值就在于「不在电脑前也能读完结论」。
    3. 会话里的用户提问也带一条摘要，方便判断这次是什么任务。
    """
    async with sf() as s:
        run = await s.get(Run, _uuid_or_none(run_id)) if run_id else None
        sess = await s.get(Session, _uuid_or_none(run.session_id)) if (run and run.session_id) else None
        project = await s.get(Project, run.project_id) if (run and run.project_id) else None
        user = await s.get(User, run.triggered_by) if (run and run.triggered_by) else None
        user_msg = None
        if run is not None and getattr(run, "trigger_msg_id", None):
            user_msg = await s.get(Message, run.trigger_msg_id)
    sess_title = (sess.title if sess else None) or "未命名会话"
    project_name = (project.name if project else None) or "未归属项目"
    who = (getattr(user, "display_name", None) or getattr(user, "email", "") or "") if user else ""
    # 汇报正文原样带上（用户要的「把汇报发过来」）。上限 20000 只是防刷屏的兜底，
    # 一旦真的触发就必须在正文里显式写明被截，不能再出现「悄悄短了」。
    raw_summary = getattr(run, "final_summary", "") if run else ""
    summary = _short(raw_summary, NOTICE_SUMMARY_LIMIT,
                     note=f"…（以下省略，汇报共 {len(raw_summary or '')} 字，完整内容见客户端本会话）")
    if len((raw_summary or "").strip()) > NOTICE_SUMMARY_LIMIT:
        log.warning("run notice summary truncated: run=%s raw_len=%s limit=%s",
                    run_id, len(raw_summary or ""), NOTICE_SUMMARY_LIMIT)
    ask = _short(getattr(user_msg, "content", "") if user_msg else "", 200)
    kind = {"succeeded": "done", "failed": "failed", "blocked": "question",
            "cancelled": "failed"}.get(status, "done")
    labels = {"succeeded": "✅ 任务完成", "failed": "❌ 任务失败",
              "blocked": "❓ 需要你回答", "cancelled": "⏹ 任务已取消"}
    title = f"{labels.get(status, '任务状态更新')} · {project_name} / {sess_title}"
    # 来源块放最前面：先让人知道「哪个项目、哪条会话、谁让跑的」
    lines = [f"**项目**：{project_name}", f"**会话**：{sess_title}"]
    if who:
        lines.append(f"**发起人**：{who}")
    if ask:
        lines.append(f"**任务**：{ask}")
    if summary:
        lines.append(f"**汇报**：\n{summary}")
    if run is not None and getattr(run, "error", None):
        lines.append(f"**错误**：{_short(run.error, 800)}")
    if run is not None and getattr(run, "session_id", None):
        # 带上会话标识，方便直接在界面对上号（也可作为后续「回复继续对话」的锚点）
        lines.append(f"**会话ID**：`{run.session_id}`")
    return kind, title, _cap_body("\n\n".join(lines))


async def notify_run_finished(sf: async_sessionmaker, *, run_id: str, team_id, status: str,
                              session_id: str | None = None,
                              app_cfg: dict | None = None) -> list[ChannelMessage]:
    """run 收尾时统一入口：给该团队下所有 active 且开启对应开关的绑定发通知。

    绝不抛异常——调用点在编排器收尾路径上，推送失败不能改写 run 状态。
    """
    try:
        kind, title, base_body = await compose_run_notice(sf, run_id, status, session_id)
        links = await active_links(sf, team_id)
        out: list[ChannelMessage] = []
        for link in links:
            if kind == "done" and not link.notify_done:
                continue
            if kind == "question" and not link.notify_question:
                continue
            if kind == "failed" and not link.notify_failed:
                continue
            # 只有真的能在 IM 里回复的绑定才提示「回复继续对话」，
            # 否则等于替用户造一个「回了没反应」的 bug 报告。
            body = base_body
            meta = channel_meta(link.channel)
            if link.inbound_enabled and meta.get("can_inbound") and link.channel != "wechat":
                body = f"{base_body}\n\n（可以在{meta['label']}里直接回复这条消息继续对话）"
            out.append(await send_to_link(sf, link, kind=kind, title=title, body=body,
                                          run_id=run_id, session_id=session_id,
                                          app_cfg=app_cfg))
        return out
    except Exception:  # noqa: BLE001 - 通知失败绝不影响 run
        log.exception("notify_run_finished failed")
        return []


def mask_secret(v: str | None) -> str:
    if not v:
        return ""
    return _MASK
