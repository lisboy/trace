"""消息通道 HTTP 接口（设置页 + 通道回调用）。

路由分组：
  GET  /api/channels                     通道清单 + 当前绑定状态 + 能力档位
  POST /api/channels/{channel}/bind      直接填凭证完成绑定（webhook / 应用凭证），**不再需要扫码**
  POST /api/channels/{id}/unlink         解绑
  PATCH /api/channels/{id}               通知开关 / 默认项目 / 是否允许 IM 回复
  POST /api/channels/{id}/test           发一条测试消息（让用户当场确认通道通了）
  GET  /api/channels/messages            最近的推送记录（排障用）
  POST /api/channels/{channel}/callback  通道入站回调（钉钉/企微/个微，可选能力）

绑定方式已从「扫码」改为「直接填地址/凭证」：扫码要求手机能访问本机地址（需公网），
而「任务完成提醒」本身是纯出站推送，零公网就能用。故 /code 路由与二维码能力一并移除。

配置（全局应用凭证）读 settings 表的 ``channels_cfg`` 键，界面在同一个面板里填。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from game_agent.api.deps import RequestContext, _u, require
from game_agent.api.state import state
from game_agent.auth.rbac import PERM_VIEW
from game_agent.db.models import ChannelLink, ChannelMessage, Settings
from game_agent.db.session import SessionLocal
from game_agent.integrations import channels as ch
from game_agent.integrations import inbound
from game_agent.logging_setup import get_logger

router = APIRouter(prefix="/api/channels", tags=["channels"])
log = get_logger("api.channels")

CFG_KEY = "channels_cfg"


def _sf():
    return state.session_factory or SessionLocal


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _app_cfg() -> dict:
    """全局通道凭证（钉钉/企微应用级 appKey、secret 等）。"""
    async with _sf()() as s:
        row = await s.get(Settings, CFG_KEY)
        return dict(row.value_json or {}) if row else {}


async def _save_app_cfg(cfg: dict) -> dict:
    async with _sf()() as s:
        row = await s.get(Settings, CFG_KEY)
        if row is None:
            row = Settings(key=CFG_KEY, value_json=cfg)
            s.add(row)
        else:
            row.value_json = cfg
            row.updated_at = _now()
        await s.commit()
    return cfg


def _link_dict(l: ChannelLink) -> dict:
    meta = ch.channel_meta(l.channel)
    cred = dict(l.credentials_json or {})
    # 凭证只回显「有没有配」，绝不回明文（前端只需要知道配没配）
    return {
        "id": str(l.id), "channel": l.channel, "channel_label": meta["label"],
        "external_id": l.external_id, "display_name": l.display_name,
        "status": l.status, "can_push": meta["can_push"], "can_inbound": meta["can_inbound"],
        "notify_done": l.notify_done, "notify_question": l.notify_question,
        "notify_failed": l.notify_failed, "inbound_enabled": l.inbound_enabled,
        "default_project_id": str(l.default_project_id) if l.default_project_id else None,
        "last_session_id": str(l.last_session_id) if l.last_session_id else None,
        "credentials_ready": {k: bool(v) for k, v in cred.items() if k != "session_webhook_expire"},
        "linked_at": str(l.linked_at) if l.linked_at else None,
        "last_notified_at": str(l.last_notified_at) if l.last_notified_at else None,
    }


@router.get("")
async def list_channels(ctx: RequestContext = Depends(require(PERM_VIEW))):
    """通道清单 + 本团队绑定情况 + 全局凭证是否就绪。"""
    cfg = await _app_cfg()
    async with _sf()() as s:
        rows = (await s.execute(select(ChannelLink).where(
            ChannelLink.team_id == _u(ctx.team_id),
            ChannelLink.status == "active").order_by(ChannelLink.created_at.asc()))).scalars().all()
    return {
        "channels": ch.all_channels(),
        "links": [_link_dict(r) for r in rows],
        "app_config": {
            "dingtalk_app_key": bool(cfg.get("dingtalk", {}).get("app_key")),
            "dingtalk_app_secret": bool(cfg.get("dingtalk", {}).get("app_secret")),
            "dingtalk_agent_id": bool(cfg.get("dingtalk", {}).get("agent_id")),
            "wecom_corp_id": bool(cfg.get("wecom", {}).get("corp_id")),
            "wecom_corp_secret": bool(cfg.get("wecom", {}).get("corp_secret")),
            "wecom_agent_id": bool(cfg.get("wecom", {}).get("agent_id")),
            "public_base_url": cfg.get("public_base_url") or "",
            "callback_base": cfg.get("public_base_url") or "",
        },
    }


class AppCfgIn(BaseModel):
    dingtalk_app_key: str | None = None
    dingtalk_app_secret: str | None = None
    dingtalk_agent_id: str | None = None
    wecom_corp_id: str | None = None
    wecom_corp_secret: str | None = None
    wecom_agent_id: str | None = None
    public_base_url: str | None = None


@router.post("/app-config")
async def save_app_config(body: AppCfgIn, ctx: RequestContext = Depends(require(PERM_VIEW))):
    cfg = await _app_cfg()
    dt = dict(cfg.get("dingtalk") or {})
    wc = dict(cfg.get("wecom") or {})
    if body.dingtalk_app_key is not None:
        dt["app_key"] = body.dingtalk_app_key.strip()
    if body.dingtalk_app_secret is not None:
        dt["app_secret"] = body.dingtalk_app_secret.strip()
    if body.dingtalk_agent_id is not None:
        dt["agent_id"] = body.dingtalk_agent_id.strip()
    if body.wecom_corp_id is not None:
        wc["corp_id"] = body.wecom_corp_id.strip()
    if body.wecom_corp_secret is not None:
        wc["corp_secret"] = body.wecom_corp_secret.strip()
    if body.wecom_agent_id is not None:
        wc["agent_id"] = body.wecom_agent_id.strip()
    cfg["dingtalk"] = dt
    cfg["wecom"] = wc
    if body.public_base_url is not None:
        cfg["public_base_url"] = body.public_base_url.strip().rstrip("/")
    await _save_app_cfg(cfg)
    return {"ok": True}


class BindIn(BaseModel):
    """直接绑定入参（已移除扫码，不再需要 token）。

    两类用法：
      · 群机器人：只填 webhook（钉钉/企微群机器人地址），external_id 缺省自动生成本机标识；
      · 定向推送 / 个人身份：填 webhook + external_id（钉钉 userid / 企微 userid），
        或走「工作通知/应用消息」用全局 app 凭证 + external_id。
    """
    webhook: str | None = None
    webhook_secret: str | None = None   # 钉钉群机器人「加签」密钥
    gateway_url: str | None = None      # 微信个人号：自备第三方网关地址
    external_id: str | None = None
    display_name: str | None = None
    default_project_id: str | None = None


async def _group_ext(sf, team_id, user_id, channel: str) -> str:
    """群机器人绑定用的占位标识。

    复用当前用户在该通道上已有的 group- 标识，避免「手滑点两次保存」变成两条
    绑定、同一个群收到两遍提醒；没有才新建。
    """
    async with sf() as s:
        row = await s.scalar(select(ChannelLink).where(
            ChannelLink.team_id == team_id,
            ChannelLink.user_id == user_id,
            ChannelLink.channel == channel,
            ChannelLink.status == "active",
            ChannelLink.external_id.like("group-%")).limit(1))
        if row is not None:
            return row.external_id
    return f"group-{uuid.uuid4().hex[:8]}"


@router.post("/{channel}/bind")
async def bind(channel: str, body: BindIn, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """直接填地址/凭证完成绑定（不需要扫码、不需要公网）。

    - 只填 webhook → 绑成「群提醒」，external_id 用 group-<随机> 占位，任务完成就往群里推；
    - 另填 external_id → 按该通道侧身份绑定，可走工作通知/应用消息定向发给这个人；
    - 微信个人号只能填 gateway_url（官方无接口）。
    """
    if channel not in ch.CHANNELS:
        raise HTTPException(status_code=404, detail="未知通道")
    sf = _sf()
    cred: dict = {}
    if body.webhook:
        cred["webhook"] = body.webhook.strip()
    if body.webhook_secret:
        cred["webhook_secret"] = body.webhook_secret.strip()
    if body.gateway_url:
        cred["gateway_url"] = body.gateway_url.strip()
    ext = (body.external_id or "").strip()
    meta = ch.channel_meta(channel)

    # 微信个人号：没有官方接口，只能填第三方网关
    if channel == "wechat":
        if not cred.get("gateway_url"):
            raise HTTPException(
                status_code=400,
                detail="个人微信没有官方推送接口：平台无法直接提醒到个人微信。"
                       "如已自建网关（如 wechaty 服务化），请填它的地址。")
        link = await ch.upsert_link(
            sf, team_id=_u(ctx.team_id), user_id=_u(ctx.user_id), channel="wechat",
            external_id=ext or f"wx-{str(ctx.user_id)[:8]}",
            display_name=body.display_name or "微信（第三方网关）",
            credentials=cred,
            default_project_id=_u(body.default_project_id) if body.default_project_id else None)
        return {"ok": True, "link": _link_dict(link)}

    # 其余通道至少要有一种推送手段：webhook 地址，或已配置的全局应用凭证
    cfg = await _app_cfg()
    app_ready = bool(channel in ("dingtalk", "wecom") and (cfg.get(channel) or {}).get(
        "app_key" if channel == "dingtalk" else "corp_id"))
    if not cred.get("webhook") and not app_ready:
        hint = ("请填写群机器人 Webhook 地址（在钉钉群里加自定义机器人即可获取）"
                if channel == "dingtalk" else
                "请填写群机器人 Webhook 地址（在企业微信群加机器人即可获取）")
        raise HTTPException(status_code=400, detail=hint)

    # 群机器人绑定：没有具体接收人身份，用占位标识避免和「定向绑定」互相顶掉
    if not ext:
        ext = await _group_ext(sf, _u(ctx.team_id), _u(ctx.user_id), channel)
    link = await ch.upsert_link(
        sf, team_id=_u(ctx.team_id), user_id=_u(ctx.user_id), channel=channel,
        external_id=ext,
        display_name=body.display_name or ("群提醒" if ext.startswith("group-") else ext),
        credentials=cred or None,
        default_project_id=_u(body.default_project_id) if body.default_project_id else None,
        inbound_enabled=False)
    return {"ok": True, "link": _link_dict(link),
            "message": f"已关联{meta['label']}，任务完成会推送到这里"}


@router.post("/{link_id}/unlink")
async def unlink(link_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    ok = await ch.unlink(_sf(), _u(ctx.team_id), _u(link_id))
    if not ok:
        raise HTTPException(status_code=404, detail="绑定不存在")
    return {"ok": True}


class LinkPatch(BaseModel):
    notify_done: bool | None = None
    notify_question: bool | None = None
    notify_failed: bool | None = None
    inbound_enabled: bool | None = None
    default_project_id: str | None = None


@router.patch("/{link_id}")
async def patch_link(link_id: str, body: LinkPatch, ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with _sf()() as s:
        link = await s.get(ChannelLink, _u(link_id))
        if link is None or str(link.team_id) != str(_u(ctx.team_id)):
            raise HTTPException(status_code=404, detail="绑定不存在")
        for f in ("notify_done", "notify_question", "notify_failed", "inbound_enabled"):
            v = getattr(body, f)
            if v is not None:
                setattr(link, f, v)
        if body.default_project_id is not None:
            link.default_project_id = _u(body.default_project_id) if body.default_project_id else None
        await s.commit()
        await s.refresh(link)
        return {"ok": True, "link": _link_dict(link)}


@router.post("/{link_id}/test")
async def send_test(link_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """发一条测试消息：让用户当场验证通道是否真的通（而不是等下一次任务跑完）。"""
    sf = _sf()
    async with sf() as s:
        link = await s.get(ChannelLink, _u(link_id))
        if link is None or str(link.team_id) != str(_u(ctx.team_id)):
            raise HTTPException(status_code=404, detail="绑定不存在")
    cfg = await _app_cfg()
    rec = await ch.send_to_link(
        sf, link, kind="test", title="循迹通道自检",
        body="这是一条测试消息。收到它说明任务完成提醒已经能推到这里了。",
        app_cfg={**cfg.get(link.channel, {}), **cfg})
    return {"ok": rec.status == "sent", "status": rec.status, "error": rec.error}


@router.get("/messages")
async def list_messages(limit: int = 30, ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with _sf()() as s:
        rows = (await s.execute(select(ChannelMessage).where(
            ChannelMessage.team_id == _u(ctx.team_id))
            .order_by(ChannelMessage.created_at.desc()).limit(max(1, min(limit, 100))))).scalars().all()
    return {"items": [{
        "id": str(r.id), "channel": r.channel, "kind": r.kind, "title": r.title,
        # 正文回**全文**：设置页「查看推送内容」看到的必须和钉钉实际收到的完全一致，
        # 否则排查时只能靠猜（2026-11-14 用户被"界面短一截"绕过一圈）。
        # 渲染高度由前端 <pre> 的 max-h 控制，不影响列表加载。
        "body": r.body or "", "status": r.status, "error": r.error,
        "created_at": str(r.created_at), "sent_at": str(r.sent_at) if r.sent_at else None,
        "run_id": str(r.run_id) if r.run_id else None,
    } for r in rows]}


@router.post("/{channel}/callback")
async def callback(channel: str, request: Request):
    """通道入站回调（无需登录：通道服务器直接打进来，靠签名 + 一次性令牌鉴权）。

    钉钉/企微要求回调必须「快速 200」否则会重试并提示超时，所以这里所有异常都
    收敛成 200 + 说明文本，把错误写日志。
    """
    if channel not in inbound.HANDLERS:
        raise HTTPException(status_code=404, detail="该通道不支持入站回调")
    if channel == "webhook":
        raise HTTPException(status_code=400, detail="webhook 通道只出不进")
    body: dict = {}
    try:
        raw = await request.body()
        if raw:
            body = json.loads(raw.decode("utf-8", "ignore"))
        if not isinstance(body, dict):
            body = {"text": str(body)}
    except Exception:  # noqa: BLE001
        body = {}
    for k, v in request.query_params.items():
        body.setdefault(k, v)
    headers = {k.lower(): v for k, v in request.headers.items()}
    cfg = await _app_cfg()
    app_cfg = {**(cfg.get(channel) or {}), "app_secret": (cfg.get("dingtalk") or {}).get("app_secret")}
    try:
        handler = inbound.HANDLERS[channel]
        if channel == "wechat":
            r = await handler(_sf(), body, state_obj=state)
        else:
            r = await handler(_sf(), body, headers, app_cfg, state_obj=state)
        log.info("channel callback: %s %s", channel, r)
        return {"ok": bool(r.get("ok")), **{k: v for k, v in r.items() if k != "ok"}}
    except Exception as e:  # noqa: BLE001 - 回调异常也要 200，否则通道侧疯狂重试
        log.exception("channel callback failed: %s", channel)
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@router.get("/{channel}/callback-info")
async def callback_info(channel: str, request: Request, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """给界面显示「回调地址填什么」（钉钉机器人 / 企微应用的接收地址）。"""
    cfg = await _app_cfg()
    base = (cfg.get("public_base_url") or "").rstrip("/")
    host = request.headers.get("host") or "127.0.0.1:18888"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    local = f"{scheme}://{host}"
    return {
        "channel": channel,
        "callback_url": f"{base or local}/api/channels/{channel}/callback",
        "public_ready": bool(base),
        "app_config_ready": bool((cfg.get(channel) or {})),
    }
