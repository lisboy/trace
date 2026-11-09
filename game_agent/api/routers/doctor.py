"""运行自检：一眼看清「这一轮到底会不会走真实大模型、还差什么」。

本程序只有一种运行方式：**永远调用真实大模型**。不存在 mock/离线演示模式。
所以这里的判断只剩一件事——**有没有配好可用的供应商**（档位绑定 or .env 全局兜底）。

把结论算给前端用：
  * 已绑定档位且能解析出 model/key/base_url → 就绪；
  * 没绑档位 → 回退 .env 全局模型（配了 OPENAI_API_KEY 也算就绪）；
  * 两者都没有 → 未就绪，明确提示「去设置 → 模型配置 添加供应商并绑定」。

只读，不发任何真实请求，不会额外花钱。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from game_agent.api.deps import _u, require
from game_agent.auth.rbac import PERM_VIEW
from game_agent.core.context import RequestContext

router = APIRouter(prefix="/api", tags=["doctor"])


async def llm_diagnostics(team_id: str) -> dict:
    """团队视角的 LLM 就绪度快照（供前端顶栏提示 / 设置页自检复用）。"""
    from game_agent.config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL, ROLE_MODELS

    issues: list[str] = []
    hints: list[str] = []

    # ---- 角色绑定档位 ----
    from game_agent.db.models import Agent, LlmProvider, ModelProfile
    from game_agent.db.session import SessionLocal

    bound_label = None
    bound_model = None
    profile_ok = False
    profile_issue = None
    any_provider = False
    async with SessionLocal() as s:
        any_provider = (await s.scalar(
            select(LlmProvider.id).where(LlmProvider.team_id == _u(team_id)).limit(1))) is not None
        agent = await s.scalar(select(Agent).where(
            Agent.team_id == _u(team_id), Agent.key == "main"))
        pkey = agent.model_profile_key if agent else None
        if not pkey:
            profile_issue = "未绑定模型档位：会回退到 .env 的全局兜底模型。"
        else:
            p = await s.scalar(select(ModelProfile).where(
                ModelProfile.team_id == _u(team_id), ModelProfile.key == pkey))
            if p is None:
                profile_issue = (f"绑定的模型档位「{pkey}」已不存在（被删但没解绑）："
                                 "运行时会静默回退到 .env 全局模型，极易 403。")
            else:
                params = p.params_json or {}
                base_url, api_key, prov_label, prov_enabled = p.base_url, None, None, True
                if p.provider_id:
                    pv = await s.get(LlmProvider, p.provider_id)
                    if pv is None:
                        profile_issue = f"档位「{pkey}」绑定的供应商已被删除。"
                    else:
                        prov_label = pv.label
                        prov_enabled = bool(pv.enabled)
                        base_url = base_url or pv.base_url
                        api_key = pv.api_key
                api_key = api_key or params.get("api_key") or p.api_key_ref
                bound_label = prov_label or p.provider or None
                bound_model = p.model
                if not prov_enabled:
                    profile_issue = f"档位「{pkey}」绑定的供应商「{prov_label}」已停用。"
                elif not p.model:
                    profile_issue = f"档位「{pkey}」没填模型名。"
                elif not api_key:
                    profile_issue = f"档位「{pkey}」没有可用的 API Key（供应商和档位都没配 Key）。"
                elif not base_url:
                    profile_issue = f"档位「{pkey}」没有可用的 Base URL。"
                else:
                    profile_ok = True

    fallback_has_key = bool(OPENAI_API_KEY)
    ready = bool(profile_ok or fallback_has_key)

    if profile_ok:
        real_model = bound_model
    else:
        real_model = ROLE_MODELS.get("main") or LLM_MODEL
        if profile_issue:
            issues.append(profile_issue)
        if not fallback_has_key:
            if any_provider:
                issues.append("而且 .env 全局兜底也没配 OPENAI_API_KEY —— 这一轮发不出任何真实请求。")
            else:
                issues.append("还没有配置任何大模型供应商（也没有 .env 全局 Key）—— 这一轮发不出任何真实请求。")
            hints.append("去「设置 → 模型配置」添加一个供应商（填好地址和 Key），再到「设置 → 智能体」把"
                         "「对话模型」绑到该档位即可；也可在聊天输入框右下角的模型选择器里直接选。")
        else:
            hints.append(f"本轮将回退到 .env 全局模型「{real_model}」（地址 {OPENAI_BASE_URL}）。")

    return {
        # ready = 这一轮确实能调到真实大模型
        "ready": ready,
        "configured": bool(profile_ok or fallback_has_key),
        "profile_ok": profile_ok,
        "profile_issue": profile_issue,
        "provider": bound_label,
        "model": real_model,
        "fallback_model": ROLE_MODELS.get("main") or LLM_MODEL,
        "fallback_has_key": fallback_has_key,
        "issues": issues,
        "hints": hints,
    }


@router.get("/doctor/llm")
async def doctor_llm(ctx: RequestContext = Depends(require(PERM_VIEW))):
    """这一轮走不走真实大模型、用的哪个模型/地址、还差什么配置。"""
    return await llm_diagnostics(ctx.team_id)
