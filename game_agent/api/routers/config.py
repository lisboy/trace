"""团队配置口（设计 §4.1 / §7.2 / §8）：智能体配置、模型档位、成员管理、用量、审计。"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select, text

from game_agent.config import DATABASE_URL, LLM_USER_AGENT
from game_agent.llm.openai_compat import (
    _new_llm_client, _proxy_env_disabled, _system_proxy,
)
from game_agent.api.deps import require
from game_agent.auth.rbac import (
    PERM_AGENT_CONFIG, PERM_MEMBER, PERM_USAGE_VIEW, PERM_VIEW,
)
from game_agent.core.context import RequestContext
from game_agent.agents.roles import ROLE_PROMPTS, ROLE_TOOLS
from game_agent.db.models import (
    Agent, AuditLog, LlmCall, LlmProvider, ModelProfile, Project, Run,
    Session, TeamMember, UsageLedger, User,
)
from game_agent.db.session import SessionLocal
from game_agent.orchestration import policy as policy_mod

router = APIRouter(prefix="/api", tags=["config"])

ROLES = ["main"]


class LlmModelsIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    provider_id: str | None = None  # 已配好的供应商：直接用它存着的 base_url/key/UA
    protocol: str = ""  # 空=按供应商/自动探测；anthropic 时优先 /v1/models + x-api-key


# 当前真正适配的协议。google/ollama 尚未实现，收口掉避免用户以为能用。
_SUPPORTED_PROTOCOLS = ("openai", "anthropic")


def _model_url_candidates(base: str) -> list[str]:
    """模型列表候选地址：先按用户填的原样，再补 /v1（多数中转站/官方都在 /v1 下）。

    用户经常只填根域名（如 https://ai.jxbw.com），真实接口却在 https://ai.jxbw.com/v1；
    这里把两种情况都试一遍，省掉「地址少个 /v1 就报不是 JSON」的无效排查。"""
    b = (base or "").strip().rstrip("/")
    if not b:
        return []
    cands = [f"{b}/models"]
    if not b.endswith("/v1"):
        cands.append(f"{b}/v1/models")
    seen: set[str] = set()
    out: list[str] = []
    for u in cands:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _models_headers(auth: str, key: str, ua: str) -> dict:
    headers: dict[str, str] = {"Accept": "application/json"}
    if ua:
        headers["User-Agent"] = ua
    if key:
        if auth == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
    return headers


def _extract_model_ids(data: dict) -> list[str]:
    """从各家 /models 响应里取模型 id：OpenAI/Anthropic 用 data[]，Google 用 models[]。"""
    items = data.get("data")
    if not isinstance(items, list):
        items = data.get("models")
    if not isinstance(items, list):
        return []
    ids: set[str] = set()
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name")
        if isinstance(mid, str) and mid:
            ids.add(mid[len("models/"):] if mid.startswith("models/") else mid)
    return sorted(ids)


@router.post("/llm/models")
async def list_llm_models(body: LlmModelsIn, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """代理拉取模型列表（服务端发起，避开浏览器 CORS 与 key 泄露）。

    地址容错：先试 {base}/models、再试 {base}/v1/models；鉴权先 Bearer，遇 401/403 再试
    x-api-key（Anthropic 风格），命中即用。命中的 base 会回填到供应商，之后对话也走同一
    条可用地址。全部失败时逐条列出「试过哪个地址、返回什么」，而不是只丢一句「不是 JSON」。"""
    ua = LLM_USER_AGENT
    protocol = (body.protocol or "").strip().lower()
    if body.provider_id:
        async with SessionLocal() as s:
            pv = await s.get(LlmProvider, _u(body.provider_id))
            if pv is None or str(pv.team_id) != ctx.team_id:
                raise HTTPException(status_code=404, detail="供应商不存在")
            base = (body.base_url or pv.base_url or "").strip().rstrip("/")
            key = "".join((body.api_key or pv.api_key or "").split())
            ua = pv.user_agent or LLM_USER_AGENT
            protocol = protocol or (pv.protocol or "")
            provider_row = pv
    else:
        provider_row = None
        base = (body.base_url or "").strip().rstrip("/")
        key = "".join((body.api_key or "").split())  # 去掉空白/换行，避免粘贴带入不可见字符
    if not base:
        raise HTTPException(status_code=400, detail="Base URL 必填")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Base URL 需以 http(s):// 开头")

    attempts: list[str] = []
    ids: list[str] = []
    used_url = ""
    saw_waf = False
    conn_fail = 0
    statuses: dict[int, str] = {}
    # 协议已知就先只用对应鉴权，避免多打一轮；协议未知（自动探测）两种都试。
    if protocol == "anthropic":
        auths = ["anthropic"] + (["bearer"] if key else [])
    elif protocol == "openai":
        auths = ["bearer"] + (["anthropic"] if key else [])
    else:
        auths = ["bearer"] + (["anthropic"] if key else [])

    async def _probe(client: httpx.AsyncClient) -> dict:
        p_attempts: list[str] = []
        p_ids: list[str] = []
        p_used = ""
        p_waf = False
        p_conn = 0
        p_status: dict[int, str] = {}
        for url in _model_url_candidates(base):
            if p_ids:
                break
            for auth in auths:
                try:
                    r = await client.get(url, headers=_models_headers(auth, key, ua))
                except Exception as e:  # noqa: BLE001
                    p_conn += 1
                    p_attempts.append(f"· {url}（{auth}）→ 连接失败：{e}")
                    break  # 同一地址换鉴权也不会通，直接换下一个地址
                ct = (r.headers.get("content-type") or "").split(";")[0].strip()
                p_status[r.status_code] = r.text[:200].replace("\n", " ")
                if r.status_code != 200:
                    snippet = r.text[:140].replace("\n", " ")
                    p_attempts.append(f"· {url}（{auth}）→ HTTP {r.status_code}"
                                      f"{(' ' + ct) if ct else ''}：{snippet}")
                    if r.status_code in (401, 403):
                        if "html" in ct:
                            p_waf = True
                        continue  # 可能只是鉴权方式不对，换 x-api-key 再试
                    break  # 404/5xx 等：换地址
                try:
                    data = r.json()
                except Exception:  # noqa: BLE001
                    p_attempts.append(f"· {url}（{auth}）→ HTTP 200 但不是 JSON（{ct or '未知类型'}）")
                    break
                got = _extract_model_ids(data if isinstance(data, dict) else {})
                if not got:
                    p_attempts.append(f"· {url}（{auth}）→ HTTP 200 但没有模型列表")
                    break
                p_ids, p_used = got, url
                break
        return {"attempts": p_attempts, "ids": p_ids, "used_url": p_used,
                "saw_waf": p_waf, "conn_fail": p_conn, "statuses": p_status}

    # 代理自动模式（与真实对话一致）：系统配了代理先走代理，连不上自动改直连再试；
    # 没配代理就直接直连。只连不通（纯连接失败、拿不到任何 HTTP 响应）才降级，
    # 拿到 401/403/404 说明网络是通的，不降级（那是 key/地址问题，换网也白搭）。
    proxy = None if _proxy_env_disabled() else await asyncio.to_thread(_system_proxy)
    async with _new_llm_client(proxy, ua, timeout=15, follow_redirects=True) as client:
        res = await _probe(client)
    if proxy and not res["ids"] and res["conn_fail"] and not res["statuses"]:
        async with _new_llm_client(None, ua, timeout=15, follow_redirects=True) as client:
            res2 = await _probe(client)
        if res2["ids"] or res2["statuses"]:
            res2["attempts"].insert(0, f"· 经系统代理 {proxy} 连不上，已自动改直连重试")
        res = res2
    attempts = res["attempts"]
    ids = res["ids"]
    used_url = res["used_url"]
    saw_waf = res["saw_waf"]
    conn_fail = res["conn_fail"]
    statuses = res["statuses"]

    if not ids:
        if conn_fail and not statuses:
            hint = ("连不上这个地址（域名解析失败或网络不通）：请确认 Base URL 拼写正确"
                    "（例如 ai.xxx.com 其实是 api.xxx.com？），以及本机/服务器能否访问该域名。")
        elif saw_waf:
            hint = ("返回的是 Cloudflare 一类人机验证页（HTML，不是 JSON）：在该供应商的"
                    "「User-Agent」里填浏览器 UA 后重试，或改用厂商提供的 API 域名。")
        elif 401 in statuses:
            hint = (f"Key 不被接受（共 {len(key)} 字符）：可能无效/未兑换/无该渠道权限。"
                    "可先「+ 添加模型」手填模型名直接保存；对话若报同样错误，说明 Key 确实无权。")
        elif 403 in statuses:
            hint = ("服务器认得请求但拒绝了这个账户（常见于订阅过期 / 分组的 Key 无 /models 权限）。"
                    "可先「+ 添加模型」手填模型名直接保存，或在厂商后台确认套餐与分组。")
        else:
            hint = ("确认 Base URL 是接口地址（一般以 /v1 结尾，可访问 /v1/models）；"
                    "也可以先「+ 添加模型」手填模型名直接保存，不影响对话使用。")
        raise HTTPException(status_code=502, detail="拉取模型失败，尝试结果：\n"
                                                  + "\n".join(attempts) + "\n" + hint)

    used_base = used_url[:-len("/models")] if used_url.endswith("/models") else base
    merged: list[dict] = []
    if provider_row is not None:
        async with SessionLocal() as s:
            pv = await s.get(LlmProvider, provider_row.id)
            if pv is not None:
                # 合并而不是覆盖：用户已设置的上下文窗口要保留；新拉到的模型补 0（未设置）
                old = {m["name"]: m for m in _norm_models(pv.models_json)}
                for mid in ids:
                    merged.append(old.get(mid) or {"name": mid, "context_window": 0})
                pv.models_json = merged
                if used_base and used_base != (pv.base_url or "").rstrip("/"):
                    pv.base_url = used_base  # 记下真正可用的地址，对话走同一条
                await s.commit()
    if not merged:
        merged = [{"name": mid, "context_window": 0} for mid in ids]
    return {"models": merged, "base_url": used_base, "requested_base": base}


# ---------------------------------------------------------------- LLM 供应商
class ProviderModelIn(BaseModel):
    """供应商下的一个可加载模型：模型名 + 上下文窗口（tokens，0=未设置）+ 是否支持视觉输入。

    vision 是「这个模型能不能看图」的档位开关：关掉时适配层会把附图替换成一句提示
    （见 llm/openai_compat.NO_VISION_NOTE），模型看不到画面。以前这个字段只在后端
    存在、前端没有任何入口，用户对着「图看不到」只能猜——现在每个模型可单独勾选。"""
    name: str
    context_window: int = 0
    vision: bool = False


class LlmProviderIn(BaseModel):
    key: str
    label: str = ""
    protocol: str = "openai"  # openai | anthropic | google | ollama
    base_url: str | None = None
    api_key: str | None = None
    user_agent: str | None = None
    enabled: bool = True
    models: list[ProviderModelIn] | None = None  # 模型目录（新建时一并保存）


class LlmProviderPatch(BaseModel):
    label: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # None=保持原值；""=清空；其它=替换（GET 不回传明文，故必须显式给）
    user_agent: str | None = None
    enabled: bool | None = None
    models: list[ProviderModelIn] | None = None  # 模型目录（覆盖）


def _norm_models(raw: list | None) -> list[dict]:
    """统一模型目录格式：兼容存量字符串列表与 {name, context_window[, vision]} 对象列表。"""
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw or []:
        if isinstance(item, str):
            name, window, vision = item.strip(), 0, False
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("id") or "").strip()
            try:
                window = int(item.get("context_window") or 0)
            except (TypeError, ValueError):
                window = 0
            vision = bool(item.get("vision"))
        else:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "context_window": max(window, 0), "vision": vision})
    return out


def _key_hint(k: str | None) -> tuple[bool, str | None]:
    """密钥只回摘要，绝不回明文：这个接口是登录态可读的，不能把凭据发给浏览器。"""
    if not k:
        return False, None
    if len(k) <= 12:
        return True, f"共 {len(k)} 位"
    return True, f"{k[:6]}…{k[-4]}（共 {len(k)} 位）"


def _provider_out(p: LlmProvider, bound: int = 0) -> dict:
    has, hint = _key_hint(p.api_key)
    return {"id": str(p.id), "key": p.key, "label": p.label, "protocol": p.protocol,
            "base_url": p.base_url, "has_key": has, "key_hint": hint,
            "user_agent": p.user_agent, "enabled": bool(p.enabled),
            "models": _norm_models(p.models_json), "bound_profiles": bound}


@router.get("/llm-providers")
async def list_llm_providers(ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with SessionLocal() as s:
        rows = (await s.execute(select(LlmProvider).where(
            LlmProvider.team_id == _u(ctx.team_id)).order_by(LlmProvider.created_at)
        )).scalars().all()
        counts = dict((await s.execute(
            select(ModelProfile.provider_id, func.count())
            .where(ModelProfile.team_id == _u(ctx.team_id))
            .group_by(ModelProfile.provider_id))).all())
        return {"providers": [_provider_out(p, int(counts.get(p.id, 0) or 0)) for p in rows]}


@router.post("/llm-providers")
async def create_llm_provider(body: LlmProviderIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    key = (body.key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="标识必填")
    if body.protocol not in _SUPPORTED_PROTOCOLS:
        raise HTTPException(
            status_code=400,
            detail=(f"暂不支持「{body.protocol}」协议。当前支持：openai（OpenAI 兼容 /v1/chat/completions）、"
                    "anthropic（Anthropic /v1/messages）。google/ollama 尚未适配。"))
    url = (body.base_url or "").strip() or None
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Base URL 需以 http(s):// 开头")
    async with SessionLocal() as s:
        dup = await s.scalar(select(LlmProvider).where(
            LlmProvider.team_id == _u(ctx.team_id), LlmProvider.key == key))
        if dup:
            raise HTTPException(status_code=409, detail="同标识的供应商已存在")
        p = LlmProvider(team_id=_u(ctx.team_id), key=key,
                        label=(body.label or "").strip() or url or key,
                        protocol=body.protocol, base_url=url,
                        api_key=(body.api_key or "").strip() or None,
                        user_agent=(body.user_agent or "").strip() or None,
                        enabled=bool(body.enabled),
                        models_json=_norm_models([m.model_dump() for m in body.models])
                        if body.models is not None else [])
        s.add(p)
        await s.commit()
        await s.refresh(p)
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="provider.add", target=p.key,
                       detail_json={"protocol": p.protocol, "base_url": p.base_url}))
        await s.commit()
        return {"id": str(p.id)}


@router.put("/llm-providers/{provider_id}")
async def update_llm_provider(provider_id: str, body: LlmProviderPatch,
                              ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """改供应商：只动显式提交的字段（api_key 省略=不动），避免掩码回填把密钥擦成空。"""
    async with SessionLocal() as s:
        p = await s.get(LlmProvider, _u(provider_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="不存在")
        if body.label is not None:
            p.label = body.label.strip() or p.label
        if body.protocol is not None:
            if body.protocol not in _SUPPORTED_PROTOCOLS:
                raise HTTPException(
                    status_code=400,
                    detail=(f"暂不支持「{body.protocol}」协议。当前支持：openai（OpenAI 兼容 /v1/chat/completions）、"
                            "anthropic（Anthropic /v1/messages）。google/ollama 尚未适配。"))
            p.protocol = body.protocol
        if body.base_url is not None:
            url = body.base_url.strip() or None
            if url and not url.startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="Base URL 需以 http(s):// 开头")
            if url != p.base_url:
                p.models_json = []  # 换地址后旧列表不再可信
            p.base_url = url
        if body.api_key is not None:
            k = "".join(body.api_key.split())
            if k != (p.api_key or ""):
                p.models_json = []  # 换 Key 后模型可见范围可能不同，强制重新拉取
            p.api_key = k or None
        if body.user_agent is not None:
            p.user_agent = body.user_agent.strip() or None
        if body.enabled is not None:
            p.enabled = bool(body.enabled)
        if body.models is not None:
            # 模型目录是「供应商 → 可加载模型」的唯一来源：保存后把同一供应商下
            # 已存在档位的上下文窗口 / 视觉开关同步过去，否则档位还按旧值猜。
            models = _norm_models([m.model_dump() for m in body.models])
            p.models_json = models
            windows = {m["name"]: m["context_window"] for m in models}
            visions = {m["name"]: m["vision"] for m in models}
            profs = (await s.execute(select(ModelProfile).where(
                ModelProfile.team_id == _u(ctx.team_id),
                ModelProfile.provider_id == p.id,
                ModelProfile.kind == "llm"))).scalars().all()
            for prof in profs:
                win = windows.get(prof.model)
                if win is None:
                    continue
                params = dict(prof.params_json or {})
                if win:
                    params["context_window"] = win
                elif "context_window" in params:
                    params.pop("context_window", None)
                # vision 只要模型目录里有这一项就同步（True/False 都写），保证关掉能生效
                params["vision"] = bool(visions.get(prof.model))
                prof.params_json = params
        await s.commit()
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="provider.update", target=p.key,
                       detail_json={"fields": sorted(k for k, v in body.model_dump().items()
                                                     if v is not None)}))
        await s.commit()
        return {"ok": True}


@router.delete("/llm-providers/{provider_id}")
async def delete_llm_provider(provider_id: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """删除供应商：连同它模型目录下自动生成的对话档位一起清理。

    供应商模型目录是加模型的唯一入口；对话档位只是输入框选择器为了记住「选过哪个
    模型」自动生成的内部记录，用户看不到也不需要管理，不能反过来卡住供应商删除。
    级联删除该供应商下的 llm 档位并解绑引用它们的智能体；媒体档位不绑供应商，不受
    影响。历史 run 的 options_json 只存 key，删除后按「跟随默认」处理，不会报错。
    """
    async with SessionLocal() as s:
        p = await s.get(LlmProvider, _u(provider_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="不存在")
        profs = (await s.execute(select(ModelProfile).where(
            ModelProfile.team_id == _u(ctx.team_id),
            ModelProfile.provider_id == p.id,
            ModelProfile.kind == "llm"))).scalars().all()
        keys = [pr.key for pr in profs]
        if keys:
            for a in (await s.execute(select(Agent).where(
                    Agent.team_id == _u(ctx.team_id),
                    Agent.model_profile_key.in_(keys)))).scalars().all():
                a.model_profile_key = None
            for pr in profs:
                await s.delete(pr)
        await s.delete(p)
        await s.commit()
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="provider.delete", target=p.key,
                       detail_json={"deleted_profiles": keys}))
        await s.commit()
        return {"ok": True, "deleted_profiles": len(keys)}


# ---------------------------------------------------------------- 模型档位
class ModelProfileIn(BaseModel):
    key: str
    label: str
    kind: str = "llm"  # llm | image | audio
    provider: str = "openai"
    model: str = ""
    provider_id: str | None = None  # 对话档位绑定的 LLM 供应商
    base_url: str | None = None
    api_key_ref: str | None = None
    params_json: dict = {}


class ModelProfilePatch(BaseModel):
    label: str | None = None
    model: str | None = None
    provider_id: str | None = None  # ""=解绑供应商
    base_url: str | None = None
    api_key: str | None = None  # None=不动；""=清空；其它=替换（媒体档位的 inline key）
    params_json: dict | None = None  # 合并覆盖（不含 api_key）


@router.get("/model-profiles")
async def list_profiles(ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with SessionLocal() as s:
        rows = (await s.execute(select(ModelProfile).where(
            ModelProfile.team_id == _u(ctx.team_id)).order_by(ModelProfile.created_at))).scalars().all()
        provs = {p.id: p for p in (await s.execute(select(LlmProvider).where(
            LlmProvider.team_id == _u(ctx.team_id)))).scalars().all()}
        # 角色绑定关系：配置页要显示"这个档位正被谁用"，删档位前得看得见
        binds: dict[str, list[str]] = {}
        for a in (await s.execute(select(Agent).where(
                Agent.team_id == _u(ctx.team_id)))).scalars().all():
            if a.model_profile_key:
                binds.setdefault(a.model_profile_key, []).append(a.key)
            pj = a.params_json or {}
            for f in ("image_profile", "audio_profile"):
                if pj.get(f):
                    binds.setdefault(pj[f], []).append(a.key)
        out = []
        for p in rows:
            params = dict(p.params_json or {})
            inline_key = params.pop("api_key", None) or p.api_key_ref
            has, hint = _key_hint(inline_key)
            pv = provs.get(p.provider_id) if p.provider_id else None
            out.append({"id": str(p.id), "key": p.key, "label": p.label,
                        "kind": p.kind or "llm", "provider": p.provider, "model": p.model,
                        "provider_id": str(p.provider_id) if p.provider_id else None,
                        "provider_label": pv.label if pv else None,
                        "base_url": p.base_url, "has_key": has, "key_hint": hint,
                        "params_json": params, "bound_roles": binds.get(p.key, [])})
        return {"profiles": out}


class ModelProfileAutoIn(BaseModel):
    provider_id: str
    model: str


def _auto_key(key: str, model: str) -> str:
    """生成档位 key：供应商标识 + 清洗后的模型名，限长并去尾部分隔符。"""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in (model or ""))
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-").lower()
    return (f"{key}-{cleaned}"[:60]).strip("-") or f"{key}-model"


@router.post("/model-profiles/auto")
async def auto_profile(body: ModelProfileAutoIn, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """选「供应商 → 模型」时复用/创建对话档位：同供应商+同模型直接复用，否则自动建档位。
    这样输入框旁就能直接选模型，不必先进设置页手工建档位。"""
    model = (body.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="模型名必填")
    async with SessionLocal() as s:
        pv = await s.get(LlmProvider, _u(body.provider_id))
        if pv is None or str(pv.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="供应商不存在")
        if not pv.enabled:
            raise HTTPException(status_code=400, detail="该供应商已停用")
        team = _u(ctx.team_id)
        existing = (await s.execute(select(ModelProfile).where(
            ModelProfile.team_id == team, ModelProfile.provider_id == pv.id,
            ModelProfile.model == model, ModelProfile.kind == "llm"))).scalars().first()
        if existing is not None:
            return {"key": existing.key, "created": False}
        key = _auto_key(pv.key, model)
        n = 2
        while await s.scalar(select(ModelProfile.id).where(
                ModelProfile.team_id == team, ModelProfile.key == key)):
            key = f"{_auto_key(pv.key, model)}-{n}"
            n += 1
        # 供应商模型目录里配了窗口/视觉就直接带进档位，选模型即生效
        pm = next((m for m in _norm_models(pv.models_json) if m["name"] == model), None)
        win = pm["context_window"] if pm else 0
        vision = bool(pm and pm.get("vision"))
        params: dict = {}
        if win:
            params["context_window"] = win
        if vision:
            params["vision"] = True
        p = ModelProfile(team_id=team, key=key, label=model, kind="llm",
                         provider=pv.protocol or "openai", model=model,
                         provider_id=pv.id, params_json=params)
        s.add(p)
        await s.commit()
        s.add(AuditLog(team_id=team, actor_id=_u(ctx.user_id),
                       action="profile.auto", target=key,
                       detail_json={"provider": pv.key, "model": model}))
        await s.commit()
        return {"key": key, "created": True}


@router.post("/model-profiles")
async def create_profile(body: ModelProfileIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    if not body.key.strip():
        raise HTTPException(status_code=400, detail="key 必填")
    if body.kind == "llm" and not body.model.strip():
        raise HTTPException(status_code=400, detail="key/model 必填")
    if body.kind not in ("llm", "image", "audio"):
        raise HTTPException(status_code=400, detail="kind 必须是 llm/image/audio")
    provider_id = None
    if body.provider_id:
        async with SessionLocal() as s:
            pv = await s.get(LlmProvider, _u(body.provider_id))
            if pv is None or str(pv.team_id) != ctx.team_id:
                raise HTTPException(status_code=404, detail="所选供应商不存在")
            if body.kind != "llm":
                raise HTTPException(status_code=400, detail="只有对话档位绑定 LLM 供应商")
            provider_id = pv.id
    async with SessionLocal() as s:
        exists = await s.scalar(select(ModelProfile).where(
            ModelProfile.team_id == _u(ctx.team_id), ModelProfile.key == body.key))
        if exists:
            raise HTTPException(status_code=409, detail="同名档位已存在")
        p = ModelProfile(team_id=_u(ctx.team_id), key=body.key.strip(), label=body.label or body.key,
                         kind=body.kind, provider=body.provider, model=body.model,
                         provider_id=provider_id,
                         base_url=body.base_url, api_key_ref=body.api_key_ref,
                         params_json=body.params_json)
        s.add(p)
        await s.commit()
        await s.refresh(p)
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="profile.add", target=p.key,
                       detail_json={"kind": p.kind, "model": p.model,
                                    "provider_id": str(provider_id) if provider_id else None}))
        await s.commit()
        return {"id": str(p.id)}


@router.put("/model-profiles/{profile_id}")
async def update_profile(profile_id: str, body: ModelProfilePatch,
                         ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """改档位：标识不可改（角色按 key 字符串绑定，改 key 会把绑定全部甩空）。"""
    async with SessionLocal() as s:
        p = await s.get(ModelProfile, _u(profile_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="不存在")
        if body.provider_id is not None:
            if body.provider_id == "":
                p.provider_id = None
            else:
                pv = await s.get(LlmProvider, _u(body.provider_id))
                if pv is None or str(pv.team_id) != ctx.team_id:
                    raise HTTPException(status_code=404, detail="所选供应商不存在")
                if p.kind != "llm":
                    raise HTTPException(status_code=400, detail="只有对话档位绑定 LLM 供应商")
                p.provider_id = pv.id
        if body.label is not None:
            p.label = body.label.strip() or p.label
        if body.model is not None:
            p.model = body.model.strip()
        if body.base_url is not None:
            p.base_url = body.base_url.strip() or None
        params = dict(p.params_json or {})
        if body.params_json is not None:
            params.update(body.params_json)
        if body.api_key is not None:
            k = "".join(body.api_key.split())
            if k:
                params["api_key"] = k
            else:
                params.pop("api_key", None)
                p.api_key_ref = None
        p.params_json = params
        if p.kind == "llm" and not p.model:
            raise HTTPException(status_code=400, detail="模型名不能为空")
        await s.commit()
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="profile.update", target=p.key,
                       detail_json={"model": p.model, "kind": p.kind}))
        await s.commit()
        return {"ok": True}


@router.delete("/model-profiles/{profile_id}")
async def delete_profile(profile_id: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    async with SessionLocal() as s:
        p = await s.get(ModelProfile, _u(profile_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="不存在")
        key = p.key
        # 删除档位时，清理仍绑定到它的智能体：否则角色 model_profile_key 会悬空，
        # _load_profile 找不到档位 → 静默回退到陈旧的环境变量（OPENAI_*），
        # 极易表现为莫名其妙的 HTTP 403（INVALID_API_KEY）。
        bound = (await s.execute(
            select(Agent).where(Agent.team_id == _u(ctx.team_id), Agent.model_profile_key == key)
        )).scalars().all()
        for a in bound:
            a.model_profile_key = None
        # 媒体档位（image/audio）也可能在 params_json 里引用该 key，一并解绑
        for a in (await s.execute(
            select(Agent).where(Agent.team_id == _u(ctx.team_id)))).scalars().all():
            pj = a.params_json or {}
            if pj.get("image_profile") == key or pj.get("audio_profile") == key:
                pj = {**pj, "image_profile": None if pj.get("image_profile") == key else pj.get("image_profile"),
                      "audio_profile": None if pj.get("audio_profile") == key else pj.get("audio_profile")}
                a.params_json = pj
        await s.delete(p)
        await s.commit()
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="profile.delete", target=key,
                       detail_json={"unbound_roles": [a.key for a in bound]}))
        await s.commit()
    return {"ok": True}


def _u(v):  # type: ignore[no-untyped-def]
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


# ---------------------------------------------------------------- 智能体配置
class AgentIn(BaseModel):
    system_prompt: str | None = None
    model_profile_key: str | None = None
    params_json: dict | None = None  # 媒体档位等扩展：image_profile / audio_profile


@router.get("/agents")
async def list_agents(ctx: RequestContext = Depends(require(PERM_VIEW))):
    async with SessionLocal() as s:
        rows = (await s.execute(select(Agent).where(Agent.team_id == _u(ctx.team_id)))).scalars().all()
        by_key = {a.key: a for a in rows}
        out = []
        for key in ROLES:
            row = by_key.get(key)
            out.append({
                "key": key,
                "system_prompt": row.system_prompt if row else ROLE_PROMPTS.get(key, ""),
                "model_profile_key": row.model_profile_key if row else None,
                "params_json": (row.params_json or {}) if row else {},
                "tools": ROLE_TOOLS.get(key, []),
                "is_default": row is None,
            })
        return {"agents": out}


@router.put("/agents/{key}")
async def update_agent(key: str, body: AgentIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    if key not in ROLES:
        raise HTTPException(status_code=404, detail="未知角色")
    async with SessionLocal() as s:
        row = await s.scalar(select(Agent).where(Agent.team_id == _u(ctx.team_id), Agent.key == key))
        if row is None:
            row = Agent(team_id=_u(ctx.team_id), key=key, system_prompt=ROLE_PROMPTS.get(key, ""),
                        model_profile_key=None, params_json={})
            s.add(row)
        if body.system_prompt is not None:
            row.system_prompt = body.system_prompt
            # 存过和出厂词不同的正文就打「自定义」标记：启动时的出厂词升级迁移据此跳过这一行
            pj = dict(row.params_json or {})
            if body.system_prompt != ROLE_PROMPTS.get(key, ""):
                pj["prompt_custom"] = True
            else:
                pj.pop("prompt_custom", None)
            row.params_json = pj
        if body.model_profile_key is not None:
            row.model_profile_key = body.model_profile_key or None
        if body.params_json is not None:
            # 合并更新（键级覆盖，不清空其它扩展参数）
            row.params_json = {**(row.params_json or {}), **body.params_json}
        await s.commit()
        changed = [k for k, v in {
            "system_prompt": body.system_prompt, "model_profile_key": body.model_profile_key,
            "params_json": body.params_json}.items() if v is not None]
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="agent.update", target=key,
                       detail_json={"fields": changed,
                                    "model_profile_key": row.model_profile_key}))
        await s.commit()
        return {"ok": True}


@router.post("/agents/{key}/reset")
async def reset_agent(key: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """恢复该角色出厂 prompt（删除团队覆盖，回落默认）。"""
    if key not in ROLES:
        raise HTTPException(status_code=404, detail="未知角色")
    async with SessionLocal() as s:
        await s.execute(delete(Agent).where(Agent.team_id == _u(ctx.team_id), Agent.key == key))
        await s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 助手行为：默认权限 / 指导项
class PolicyIn(BaseModel):
    permission_scope: str | None = None  # workspace|full
    guidance: list[dict] | None = None


@router.get("/assistant-policy")
async def get_assistant_policy(ctx: RequestContext = Depends(require(PERM_VIEW))):
    """设置 → 助手行为：新会话的默认权限作用域 + 指导项（含内置默认项）。"""
    pol = await policy_mod.load_policy()
    return {**pol, "defaults": policy_mod.DEFAULT_GUIDANCE}


@router.put("/assistant-policy")
async def put_assistant_policy(body: PolicyIn,
                               ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    if body.permission_scope is not None and \
            policy_mod.normalize_scope(body.permission_scope, "") != body.permission_scope:
        raise HTTPException(status_code=400, detail="permission_scope 只能为 workspace 或 full")
    cur = await policy_mod.load_policy()
    pol = await policy_mod.save_policy(
        body.permission_scope if body.permission_scope is not None else cur["permission_scope"],
        body.guidance if body.guidance is not None else cur["guidance"],
    )
    return {"ok": True, **pol}


# ---------------------------------------------------------------- 成员管理
class MemberIn(BaseModel):
    email: str
    role: str = "developer"


@router.get("/teams/{team_id}/members/detail")
async def members_detail(team_id: str, ctx: RequestContext = Depends(require(PERM_VIEW))):
    """成员 + 各自角色 + 近 30 天发起的 run 数（用量台账按团队记，拆不到个人头上）。"""
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(TeamMember, User)
            .join(User, User.id == TeamMember.user_id)
            .where(TeamMember.team_id == _u(team_id))
            .order_by(TeamMember.joined_at)
        )).all()
        # 每人近 30 天发起过多少 run：光有"角色"看不出谁在真正用这套系统
        # select 返回 (user, count, last) 三元组，不能直接 dict() —— 取前两列组对
        since = datetime.now(timezone.utc) - timedelta(days=30)
        act = {r[0]: (r[1], r[2]) for r in (await s.execute(
            select(Run.triggered_by, func.count(), func.max(Run.created_at))
            .where(Run.team_id == _u(team_id), Run.created_at >= since)
            .group_by(Run.triggered_by))).all()}
        return {"members": [{
            "user_id": str(tm.user_id), "email": u.email, "display_name": u.display_name,
            "role": tm.role, "joined_at": str(tm.joined_at),
            "is_self": str(u.id) == ctx.user_id,
            "runs_30d": int((act.get(tm.user_id) or (0, None))[0]),
            "last_run_at": str(act[tm.user_id][1]) if tm.user_id in act else None,
        } for tm, u in rows]}


class MemberRoleIn(BaseModel):
    role: str


@router.put("/teams/{team_id}/members/{user_id}")
async def update_member_role(team_id: str, user_id: str, body: MemberRoleIn,
                             ctx: RequestContext = Depends(require(PERM_MEMBER))):
    """改成员角色（owner 不可改：唯一 owner 被降级会让团队失去管理入口）。"""
    if body.role not in ("admin", "developer", "viewer"):
        raise HTTPException(status_code=400, detail="角色仅支持 admin/developer/viewer")
    async with SessionLocal() as s:
        tm = await s.get(TeamMember, (_u(team_id), _u(user_id)))
        if tm is None:
            raise HTTPException(status_code=404, detail="不是成员")
        if tm.role == "owner":
            raise HTTPException(status_code=400, detail="owner 角色不可改")
        tm.role = body.role
        await s.commit()
        s.add(AuditLog(team_id=_u(team_id), actor_id=_u(ctx.user_id),
                       action="member.role", target=str(user_id),
                       detail_json={"role": body.role}))
        await s.commit()
    return {"ok": True}


@router.post("/teams/{team_id}/members")
async def add_member(team_id: str, body: MemberIn, ctx: RequestContext = Depends(require(PERM_MEMBER))):
    if body.role not in ("admin", "developer", "viewer"):
        raise HTTPException(status_code=400, detail="角色仅支持 admin/developer/viewer")
    async with SessionLocal() as s:
        u = await s.scalar(select(User).where(User.email == body.email.strip().lower()))
        if u is None:
            raise HTTPException(status_code=404, detail="该邮箱尚未注册，请让对方先注册登录")
        tm = await s.get(TeamMember, (_u(team_id), u.id))
        if tm:
            raise HTTPException(status_code=409, detail="已是成员")
        s.add(TeamMember(team_id=_u(team_id), user_id=u.id, role=body.role, invited_by=_u(ctx.user_id)))
        await s.commit()
        s.add(AuditLog(team_id=_u(team_id), actor_id=_u(ctx.user_id), action="member.add",
                       target=u.email, detail_json={"role": body.role}))
        await s.commit()
    return {"ok": True}


@router.delete("/teams/{team_id}/members/{user_id}")
async def remove_member(team_id: str, user_id: str, ctx: RequestContext = Depends(require(PERM_MEMBER))):
    async with SessionLocal() as s:
        tm = await s.get(TeamMember, (_u(team_id), _u(user_id)))
        if tm is None:
            raise HTTPException(status_code=404, detail="不是成员")
        if tm.role == "owner":
            raise HTTPException(status_code=400, detail="不能移除 owner")
        await s.delete(tm)
        await s.commit()
        s.add(AuditLog(team_id=_u(team_id), actor_id=_u(ctx.user_id), action="member.remove",
                       target=str(user_id)))
        await s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 用量
def _local_offset_hours() -> int:
    """服务器所在时区相对 UTC 的整小时偏移（按本地日切统计用量，否则用户看到的"今天"差 8 天）。"""
    off = datetime.now().astimezone().utcoffset()
    return int(round((off.total_seconds() if off else 0) / 3600))


@router.get("/usage")
async def usage(days: int = Query(30, ge=1, le=365),
                agent: str | None = Query(None),
                model: str | None = Query(None),
                ctx: RequestContext = Depends(require(PERM_USAGE_VIEW))):
    """用量台账。agent / model 可选筛选（类型筛选）：排除哪类调用拖低整体命中率的干扰，
    比如只看项目组长自己的缓存命中率。facets 永远按未筛选的全集返回，避免选中后下拉只剩自己。"""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    tz_off = _local_offset_hours()
    is_sqlite = DATABASE_URL.startswith("sqlite")
    call_cond = [LlmCall.team_id == _u(ctx.team_id), LlmCall.created_at >= since]
    if agent:
        call_cond.append(LlmCall.agent_key == agent)
    if model:
        call_cond.append(LlmCall.model == model)
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(UsageLedger.kind, func.coalesce(func.sum(UsageLedger.amount), 0))
            .where(UsageLedger.team_id == _u(ctx.team_id), UsageLedger.created_at >= since)
            .group_by(UsageLedger.kind))).all()
        calls = (await s.execute(
            select(func.count(), func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
                   func.coalesce(func.sum(LlmCall.completion_tokens), 0),
                   func.coalesce(func.sum(LlmCall.latency_ms), 0),
                   func.coalesce(func.sum(LlmCall.cached_tokens), 0),
                   func.coalesce(func.sum(LlmCall.cache_reported), 0))
            .where(*call_cond))).one()
        # 按模型：调用数 / tokens / 总耗时 → 平均生成速度 + 缓存命中率
        by_model = (await s.execute(
            select(LlmCall.model, func.count(), func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
                   func.coalesce(func.sum(LlmCall.completion_tokens), 0),
                   func.coalesce(func.sum(LlmCall.latency_ms), 0),
                   func.coalesce(func.sum(LlmCall.cached_tokens), 0),
                   func.coalesce(func.sum(LlmCall.cache_reported), 0))
            .where(*call_cond)
            .group_by(LlmCall.model)
            .order_by(func.count().desc()))).all()
        # 按角色：调用数 + tokens + 命中率（哪个角色把整体命中率拖低了，在这里拆开看）
        by_agent = (await s.execute(
            select(LlmCall.agent_key, func.count(),
                   func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens), 0),
                   func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
                   func.coalesce(func.sum(LlmCall.cached_tokens), 0),
                   func.coalesce(func.sum(LlmCall.cache_reported), 0),
                   func.coalesce(func.sum(LlmCall.completion_tokens), 0),
                   func.coalesce(func.sum(LlmCall.latency_ms), 0))
            .where(*call_cond)
            .group_by(LlmCall.agent_key))).all()
        # 筛选下拉候选项：按未筛选的全集算
        facet_base = [LlmCall.team_id == _u(ctx.team_id), LlmCall.created_at >= since]
        agent_facets = [r[0] for r in (await s.execute(
            select(LlmCall.agent_key).where(*facet_base, LlmCall.agent_key.is_not(None))
            .group_by(LlmCall.agent_key)
            .order_by(func.sum(LlmCall.prompt_tokens).desc()))).all()]
        model_facets = [r[0] for r in (await s.execute(
            select(LlmCall.model).where(*facet_base, LlmCall.model.is_not(None))
            .group_by(LlmCall.model)
            .order_by(func.sum(LlmCall.prompt_tokens).desc()))).all()]
        # 按项目：经 run 关联聚合（无 run 的调用不计入）
        by_project = (await s.execute(
            select(Run.project_id, func.count(),
                   func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
                   func.coalesce(func.sum(LlmCall.completion_tokens), 0),
                   func.coalesce(func.sum(LlmCall.latency_ms), 0))
            .join(Run, Run.id == LlmCall.run_id)
            .where(*call_cond)
            .group_by(Run.project_id))).all()
        # 按天（本地日切）：调用数 + tokens，供趋势图
        day_expr = (func.date(func.datetime(LlmCall.created_at, f"{tz_off:+d} hours"))
                    if is_sqlite else
                    func.date(func.date_add(LlmCall.created_at,
                                            text(f"INTERVAL {tz_off} HOUR"))))
        by_day = (await s.execute(
            select(day_expr, func.count(),
                   func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens), 0),
                   func.coalesce(func.sum(LlmCall.completion_tokens), 0),
                   func.coalesce(func.sum(LlmCall.latency_ms), 0))
            .where(*call_cond)
            .group_by(day_expr).order_by(day_expr))).all()
        # 按会话：整体命中率被哪条会话拉低的一眼可见（只取用量前 50 条会话）
        by_session = (await s.execute(
            select(Session.id, Session.title, Project.name, func.count(),
                   func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
                   func.coalesce(func.sum(LlmCall.completion_tokens), 0),
                   func.coalesce(func.sum(LlmCall.latency_ms), 0),
                   func.coalesce(func.sum(LlmCall.cached_tokens), 0),
                   func.coalesce(func.sum(LlmCall.cache_reported), 0))
            .select_from(LlmCall)
            .join(Run, Run.id == LlmCall.run_id)
            .join(Session, Session.id == Run.session_id)
            .join(Project, Project.id == Session.project_id)
            .where(*call_cond)
            .group_by(Session.id, Session.title, Project.name)
            .order_by(func.sum(LlmCall.prompt_tokens).desc())
            .limit(50))).all()
        names = {str(r[0]): r[1] for r in (await s.execute(
            select(Project.id, Project.name).where(Project.team_id == _u(ctx.team_id)))).all()}
        # 模型名 → 档位/供应商：用量表里只有裸模型名，光看名字猜不出走的哪个端点
        prov_labels = {p.id: p.label for p in (await s.execute(
            select(LlmProvider).where(LlmProvider.team_id == _u(ctx.team_id)))).scalars().all()}
        prof_by_model: dict[str, dict] = {}
        for pr in (await s.execute(select(ModelProfile).where(
                ModelProfile.team_id == _u(ctx.team_id)))).scalars().all():
            if pr.model:
                prof_by_model.setdefault(pr.model, {
                    "profile": pr.label, "kind": pr.kind or "llm",
                    "provider": prov_labels.get(pr.provider_id) if pr.provider_id else None})

        def _rate(cached: int, prompt: int, reported: int):
            return round(cached / prompt * 100, 1) if prompt > 0 and reported > 0 else None

        total_ms = int(calls[3])
        completion = int(calls[2])
        prompt_total = int(calls[1])
        models = [{"model": m or "unknown", "calls": int(c), "prompt": int(p),
                   "completion": int(ct), "latency_ms": int(ms),
                   "cached": int(ca), "cache_hit_rate": _rate(int(ca), int(p), int(rep)),
                   "tokens_per_sec": round(ct / (ms / 1000), 1) if ms > 0 and ct > 0 else None,
                   **(prof_by_model.get(m) or {})}
                  for m, c, p, ct, ms, ca, rep in by_model]
        models.sort(key=lambda x: -(x["calls"] + x["prompt"] + x["completion"]))
        projects = [{"project_id": str(pid), "project": names.get(str(pid), str(pid)),
                     "calls": int(c), "prompt": int(p), "completion": int(ct),
                     "latency_ms": int(ms),
                     "tokens_per_sec": round(int(ct) / (int(ms) / 1000), 1)
                     if int(ms) > 0 and int(ct) > 0 else None}
                    for pid, c, p, ct, ms in by_project]
        projects.sort(key=lambda x: -(x["calls"] + x["prompt"] + x["completion"]))
        days_out = []
        for d, c, t, ct, ms in by_day:
            days_out.append({"date": str(d), "calls": int(c), "tokens": int(t),
                             "completion": int(ct),
                             "tokens_per_sec": round(int(ct) / (int(ms) / 1000), 1)
                             if int(ms) > 0 and int(ct) > 0 else None})
        sessions = [{"session_id": str(sid), "title": title or (pname or "未命名会话"),
                     "calls": int(c), "prompt": int(p), "completion": int(ct),
                     "latency_ms": int(ms), "cached": int(ca),
                     "cache_hit_rate": _rate(int(ca), int(p), int(rep)),
                     "tokens_per_sec": round(int(ct) / (int(ms) / 1000), 1)
                     if int(ms) > 0 and int(ct) > 0 else None}
                    for sid, title, pname, c, p, ct, ms, ca, rep in by_session]
        # run 成功率：光看 token 看不出这批钱花得值不值
        run_stats = (await s.execute(
            select(Run.status, func.count()).where(
                Run.team_id == _u(ctx.team_id), Run.created_at >= since)
            .group_by(Run.status))).all()
        return {
            "days": days,
            "tokens": {
                "prompt": prompt_total, "completion": completion, "calls": int(calls[0]),
                "latency_ms": total_ms,
                "cached": int(calls[4]),
                "cache_hit_rate": _rate(int(calls[4]), prompt_total, int(calls[5])),
                "tokens_per_sec": round(completion / (total_ms / 1000), 1)
                if total_ms > 0 and completion > 0 else None,
            },
            "runs": {k or "unknown": int(v) for k, v in run_stats},
            "ledger": {k: int(v) for k, v in rows},
            "by_model": models,
            "by_project": projects,
            "by_day": days_out,
            "by_session": sessions,
            "agent_facets": agent_facets,
            "model_facets": model_facets,
            "by_agent": {k or "unknown": {
                "calls": int(c), "tokens": int(t), "prompt": int(p), "cached": int(ca),
                "cache_hit_rate": _rate(int(ca), int(p), int(rep)),
                "tokens_per_sec": round(int(ct) / (int(ms) / 1000), 1)
                if int(ms) > 0 and int(ct) > 0 else None}
                for k, c, t, p, ca, rep, ct, ms in by_agent},
        }


# ---------------------------------------------------------------- 审计
@router.get("/audit")
async def audit(limit: int = Query(50, ge=1, le=500),
                q: str | None = Query(None, max_length=40),
                ctx: RequestContext = Depends(require(PERM_USAGE_VIEW))):
    """操作留痕。q 按动作/对象模糊过滤（配置类记录一多，光翻页找不到那次改了什么）。"""
    async with SessionLocal() as s:
        stmt = select(AuditLog).where(AuditLog.team_id == _u(ctx.team_id))
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(AuditLog.action.like(like), AuditLog.target.like(like)))
        rows = (await s.execute(stmt.order_by(AuditLog.created_at.desc()).limit(limit))).scalars().all()
        actors = {u.id: u.display_name for u in (await s.execute(
            select(User).where(User.id.in_([a.actor_id for a in rows if a.actor_id]))
        )).scalars().all()} if any(a.actor_id for a in rows) else {}
        return {"logs": [{
            "id": str(a.id), "action": a.action, "target": a.target,
            "actor": actors.get(a.actor_id) if a.actor_id else None,
            "ip": a.ip, "detail": a.detail_json, "created_at": str(a.created_at),
        } for a in rows]}
