"""编排器：一次 run 的全链路（设计 §5 / §6 / §7）。

队列领取 → 申请写锁(fencing) → 任务树
(plan→implement→art→test→fix→test→report) → 实时事件 → 收尾变更报告。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from game_agent.core.registry import get_agent_config
from game_agent.agents.roles import ROLE_TOOLS
from game_agent.agents.runtime import AgentResult
from game_agent.orchestration.intent import (IntentResult, classify_intent,
                                             hint_for as intent_hint_for)
from game_agent.agents.tools.question import AskUserStop
from game_agent.config import (AUTO_CONTINUE_MAX, COMFYUI_BASE_URL, LOCK_TTL_S, LOCK_WAIT_S,
                               LLM_MODEL, MAX_CONCURRENT_RUNS, PLATFORM,
                               PROJECTS_ROOT, ROLE_MODELS, SAME_PROJECT_PARALLEL, SHELL_NAME,
                               default_python_bin)
from game_agent.core.abstractions import EventEnvelope, FencingLost
from game_agent.core.context import RunContext
from game_agent.core.lite.container import CoreServices
from game_agent.db.models import (LlmProvider, Message, ModelProfile, ProjectMemory, Run,
                                  RunQuestion, Session, User)
from game_agent.db.session import SessionLocal
from game_agent.logging_setup import get_logger
from game_agent.llm.base import Ctx, LlmAuthError
from game_agent.llm.context_window import DEFAULT_CONTEXT_WINDOW, estimate_tokens
from game_agent.llm.factory import build_llm
from game_agent.orchestration import caps as caps_mod
from game_agent.orchestration import policy as policy_mod
from game_agent.orchestration import change_report as change_report_mod
from game_agent.orchestration import envfacts
from game_agent.orchestration import project_memory as project_memory_mod
from game_agent.orchestration import project_notes
from game_agent.orchestration.tempfiles import sweep_run

log = get_logger("orchestration")

ROLE_LABELS = {
    "main": "助手",
    # 旧数据（历史消息里的 agent_key）统一显示成「助手」
    "planner": "助手", "artist": "助手", "voice": "助手",
}

# 开跑前注入的「会话历史」块最多多少 token。以前按窗口的一半算（32k 窗口 → 16k），
# 默认窗口提到 256k 后一半就是 128k，会把 run 自己的预算吃光；运行期已有全局摘要
# 压缩兜住长对话，这里没必要贪多。
SESSION_HISTORY_MAX_TOKENS = 12000

# 接口认的推理强度档位。空串 = 不传该字段（走端点默认），两者语义不同：
# 「设置 → 智能体」里清空推理等级就是回到端点默认，不能当成 0/'' 透传出去。
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")


def _norm_effort(v) -> str:  # type: ignore[no-untyped-def]
    """推理强度归一：只认 5 个档位，其它（None / '' / 'Default' / 脏值）一律视为「不指定」。"""
    s = str(v or "").strip().lower()
    return s if s in REASONING_EFFORTS else ""


def _clip_keep_tail(text: str, cap: int) -> str:
    """超长截断时保留**头 + 尾**，不砍尾。

    实测（kpgame 2026-10-29）：上一轮的收尾报告被截到 800 字，正好把「剩余工作两步：
    补 standee / dynamic」这一整段砍掉，用户点「继续」后下一轮又从零勘察了 9.7 分钟。
    报告的价值通常就在最后两段（结论与剩余项）。
    """
    if len(text) <= cap:
        return text
    head = cap * 2 // 3
    return text[:head] + "\n…（中间省略）…\n" + text[-(cap - head):]


def _merge_usage(total: dict, usage: dict | None) -> dict:
    """多次 run_agent（含纠正重跑）的累计用量合并。"""
    out = dict(total or {})
    u = dict(usage or {})
    for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "calls", "latency_ms"):
        out[k] = int(out.get(k) or 0) + int(u.get(k) or 0)
    return out


def _run_usage_json(usage: dict | None) -> dict:
    """把 agent 循环的累计用量映射成前端 RunPanel 认的字段。

    runs.usage_json 之前从来没人写（模型定义有、API 在读、编排器不写），界面上
    「N 次调用 · X tok」始终是空的，只能去 llm_transcripts 手工聚合才知道一次 run
    的 26 分钟生成 / 20 分钟出图各占多少。
    """
    u = dict(usage or {})
    calls = int(u.get("calls") or 0)
    prompt = int(u.get("prompt_tokens") or 0)
    comp = int(u.get("completion_tokens") or 0)
    cached = int(u.get("cached_tokens") or 0)
    ms = int(u.get("latency_ms") or 0)
    out = {"calls": calls, "prompt": prompt, "completion": comp,
           "cached": cached, "latency_ms": ms}
    if ms > 0 and comp > 0:
        out["tokens_per_sec"] = round(comp / (ms / 1000), 1)
    if prompt > 0:
        out["cache_hit_rate"] = round(cached * 100 / prompt)
    return out


# 写/生成类工具（用于「有没有落地」统计；不随轮次类型裁剪——工具选择交给模型）
WRITE_TOOLS = {
    "write_file", "edit_file", "run_command", "start_service",
    "generate_image", "generate_audio",
    # 旧版派发工具名（历史 run 轨迹里还会出现，统计仍需识别）
    "dispatch_art", "dispatch_audio",
    "edit_image", "tts",
    "generate_sfx", "generate_music", "upsert_voice_profile",
    "save_character_sheet", "design_voice",
}

# 项目结构扫描时忽略的噪音目录/文件
_NOISE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".idea", ".vscode", ".pytest_cache", "coverage", ".ga", ".trace",
}

# 「真的落地了」的动作（用于「只勘察不动手」守卫：读/查/看类不算落地）
LANDING_TOOLS = WRITE_TOOLS | {
    "delete_file", "save_character_sheet", "screenshot_page",
}

# 项目结构扫描时忽略的噪音目录/文件
_NOISE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".idea", ".vscode", ".pytest_cache", "coverage", ".ga", ".trace",
}

# 「真的落地了」的动作（用于「只勘察不动手」守卫：读/查/看类不算落地）
LANDING_TOOLS = WRITE_TOOLS | {
    "delete_file", "save_character_sheet", "screenshot_page",
}

# ---------- 运行能力开关（前端「运行选项」）----------
# 默认全开；关掉后既裁剪工具（硬约束），也在本轮提示里说清楚，避免模型绕路。
CAP_TEST_TOOLS = {"start_service", "stop_service", "service_logs", "screenshot_page"}
CAP_WEB_TOOLS = {"web_search", "web_fetch"}
CAP_MEDIA_TOOLS = {
    "generate_image", "edit_image", "save_character_sheet", "generate_audio",
    "list_voice_presets", "tts", "design_voice", "generate_sfx",
    "generate_music", "upsert_voice_profile",
}
CAP_DEFAULTS = {"test": True, "web": True, "media": True}


def normalize_caps(raw: object) -> dict[str, bool]:
    """把前端传来的 {test/web/media} 归一化；缺省一律 True（老会话/老客户端默认全开）。"""
    caps = dict(CAP_DEFAULTS)
    if isinstance(raw, dict):
        for k in caps:
            if k in raw:
                caps[k] = bool(raw[k])
    return caps


def caps_tool_filter(whitelist: list[str], caps: dict[str, bool]) -> list[str]:
    drop: set[str] = set()
    if not caps.get("test", True):
        drop |= CAP_TEST_TOOLS
    if not caps.get("web", True):
        drop |= CAP_WEB_TOOLS
    if not caps.get("media", True):
        drop |= CAP_MEDIA_TOOLS
    return [w for w in whitelist if w not in drop] if drop else whitelist


def caps_hint(caps: dict[str, bool]) -> str:
    """能力开关写进本轮提示：关掉的要说清「不做什么、怎么收尾」。"""
    parts: list[str] = []
    if caps.get("test", True):
        parts.append("【自动化测试已开启】起服务做运行时验证是**重手段，默认不做**："
                     "改完先跑 run_command 的语法/构建/单测级静态验证，过了就汇报；"
                     "只有用户明确要求看效果，或同一个问题多轮修改仍未解决时，"
                     "才 start_service + screenshot_page 实测。"
                     "若起服务换了几种方式仍起不来，就**停止测试**，把起不来的原因、已试过的方法、风险点写进汇报，不要无限重试。")
    else:
        parts.append("【本轮关闭了自动化测试】不要 start_service / service_logs / screenshot_page，"
                     "也不要跑 CDP、接口或端到端测试。改完做语法/构建级静态检查即可收尾，"
                     "汇报时明确写「未做运行时验证」。")
    if not caps.get("web", True):
        parts.append("【本轮关闭了联网】不要 web_search / web_fetch；缺外部资料就基于现有代码判断，"
                     "并把不确定的地方写进汇报。")
    if not caps.get("media", True):
        parts.append("【本轮关闭了出图与配音】不要调用 generate_image / edit_image / generate_audio / tts "
                     "等媒体工具；任务需要素材时先说明「需要开启出图/配音」再继续。")
    return "\n".join(parts)

# run_command 命中这些前缀视为「只读查看」，不算落地
_READONLY_CMD_PREFIXES = (
    "ls", "dir", "cat", "type", "head", "tail", "find", "grep", "rg", "echo", "pwd",
    "where", "which", "git status", "git log", "git diff", "git show", "ffprobe",
    "node -v", "npm -v", "python -V", "python --version", "pip --version", "pytest --version",
)

# 回复消息正文的硬上限（只作病态保护；正常报告几 K 字一律完整落库）。
_REPLY_MAX_CHARS = 20000

# 「这一轮要不要动手落地」不再按关键词猜：交给大模型推理（game_agent/orchestration/intent.py），
# 得到 IntentResult(intent=modify|survey, mode=fix|build)。execute_run 据此决定：
#   · intent=survey → 把本轮降级成只读诊断（run_type=analyze），收尾不逼落地、不判 failed；
#   · intent=modify → 启用「只勘察不动手」收尾纠正 + 过程刹车（mode=fix 走修 bug 刹车，
#     build 走创建/生成收敛守卫）。
# 关键词永远补不完中文表达（实测 2026-11-10「移除…导入…」两个动词都不在旧词表里，
# 守卫全程没启用 → 只勘察没落地，被用户投诉「我明确要求他改了」）。判定交给模型推理。


class Orchestrator:
    def __init__(self, services: CoreServices, worker_id: str,
                 session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.svcs = services
        self.worker_id = worker_id
        self._sf = session_factory
        self._test_reports: dict[str, Any] = {}
        self._summaries: dict[str, str] = {}
        # 子agent 自己的收尾结论（按 task_id 分）。不能和主agent 共用 _summaries：
        # 那个字典按 run_id 存，子agent 一调 report_to_user 就会把主agent 的最终汇报顶掉。
        self._sub_summaries: dict[str, str] = {}
        # 已落成聊天消息的回复（run_id, 正文）→ 幂等去重：同一段回复绝不重复落两条。
        self._posted_replies: set[tuple[str, str]] = set()
        # 结构扫描缓存：workspace -> 指纹对应的纯结构块（不含技能块）。
        # 指纹变了才重扫，避免每个 run 都全量递归列目录（P0 快赢，见 DESIGN-OPTIMIZATION §4.1）。
        self._structure_cache: dict[str, str] = {}
        # 「进程内提问暂停」：question_id -> Future。ask_user 工具提问后在此挂起当前 run，
        # 用户在 /questions/{id}/answer 作答时 set_result 唤醒——同一 run、同一轨迹、同一份
        # 待办清单接着跑，不再把提问当成「上一轮结束 + 新起一个 run」（用户 2026-11-10 要求）。
        # 仅同进程有效：服务重启/多进程 server 模式下拿不到 future，answer 端点自动回退到旧逻辑。
        self._pending_answers: dict[str, asyncio.Future] = {}

    # ---------- 「只勘察不动手」守卫 ----------
    @staticmethod
    def _needs_action(text: str) -> bool:
        """兼容壳：旧的关键词判定已废弃。真正的意图由模型推理（classify_intent）给出。

        保留该静态方法只为不打断外部引用；调用它等于「无法判断，按动手处理」（安全兜底）。
        新代码请不要再用它，改用 execute_run 里的意图推理结果（IntentResult）。
        """
        return bool((text or "").strip())

    @staticmethod
    def _cmd_is_readonly(cmd: str) -> bool:
        c = (cmd or "").strip().lower()
        return (not c) or any(c.startswith(p) for p in _READONLY_CMD_PREFIXES)

    async def _landed_actions(self, run_id: str) -> int:
        """本轮真正落地了几次**产品改动**（改产品代码/生成素材）。

        ⚠️ 过程文件不算：实测 run 27a8a2bf 写了 23 个 target/_probe_*.mjs、
        跑了 90 条命令（全是 node -e 读文件 / 查数据库 / 扫目录），产品代码一行没改，
        却被原来的「非只读命令也算落地」规则算成 48 次落地，守卫全程没触发。
        运行命令 = 验证手段，不是改动本身；只跑测试的请求由 _RUN_ONLY_HINTS 豁免守卫。
        """
        from game_agent.agents.tools import is_scratch_path
        try:
            events = await self.svcs.repo.get_events_since(run_id, 0)
        except Exception:  # noqa: BLE001
            return 1
        n = 0
        for e in events:
            if e.type != "tool_call":
                continue
            payload = e.payload_json or {}
            if isinstance(payload, str):
                continue
            tool = str(payload.get("tool") or "")
            args = payload.get("args") or {}
            # 参数没解析出来的调用不算落地：args 是空 {}、根本没有 path，若还按「非过程
            # 文件」计数，一次失败的 write_file 会被当成「改过产品代码」，掩盖「其实没落盘」。
            if payload.get("arguments_error"):
                continue
            if tool in ("write_file", "edit_file", "delete_file"):
                rel = str(args.get("path") or "").strip()
                if rel and not is_scratch_path(rel):
                    n += 1
            elif tool in ("generate_image", "generate_audio",
                          "dispatch_art", "dispatch_audio"):
                n += 1
        return n

    # ---------- 顶层 run 生命周期 ----------
    async def execute_run(self, run_id: str) -> None:
        repo = self.svcs.repo
        run = await repo.get_run(run_id)
        if run is None or run.status not in ("queued", "claimed"):
            return
        project = await repo.get_project(run.project_id)
        if project is None:
            await repo.update_run(run_id, status="failed", error="项目不存在")
            return
        workspace = PROJECTS_ROOT / project.repo_path
        # 输入框模型选择器：本轮显式选了档位/推理等级时优先用它（覆盖角色绑定），
        # 空值仍跟随角色绑定档位——保证「不改选择器 = 行为完全不变」。
        run_opts = dict(getattr(run, "options_json", None) or {})
        # 角色默认（设置 → 智能体）：绑定档位 + 默认推理等级。一次查库拿全，别拆两次。
        agent_key, agent_effort = await self._agent_defaults(run.team_id, "main")
        session_profile = await self._load_profile(
            run.team_id, run_opts.get("model_profile_key") or agent_key)
        # 推理等级：本轮显式选择 > 角色默认（输入框切模型会写回这里）> 档位自带
        effort = _norm_effort(run_opts.get("reasoning_effort")) or agent_effort
        if effort:
            session_profile = {**session_profile,
                               "params": {**(session_profile.get("params") or {}),
                                          "reasoning_effort": effort}}
        # 会话挂载技能（渐进式披露）：None=未指定（跟随项目默认）；名单=只注入这些
        session_skills: list[str] | None = None
        # 会话权限作用域（workspace/full）：run 选项优先，其次会话已存值；都没有走安全默认
        session_scope: str = ""
        try:
            async with self._sf() as s:
                sess = await s.get(Session, _u(run.session_id)) if run.session_id else None
                if sess is not None:
                    if sess.skills_json is not None:
                        session_skills = [str(x) for x in sess.skills_json][:12]
                    session_scope = str(getattr(sess, "permission_scope", "") or "")
        except Exception:  # noqa: BLE001 - 技能挂载读取失败不影响 run
            log.exception("read session skills failed")
        perm_scope = policy_mod.normalize_scope(
            run_opts.get("permission_scope") or session_scope or policy_mod.DEFAULT_SCOPE)
        # 「继续」续跑：不写任何消息，续跑说明/图片/附件随新 run 的 options 带进来。
        # 有新触发消息时走原来的消息路径；没有就把 options 里的续跑上下文当本轮需求。
        continue_brief = ""
        if not run.trigger_msg_id:
            continue_brief = str(run_opts.get("brief") or "").strip()
        if continue_brief:
            requirement, images = await self._load_context(
                run.team_id, None, session_profile,
                project_id=run.project_id, workspace=workspace,
                session_skills=session_skills,
                override_req=continue_brief,
                override_images=[str(u) for u in (run_opts.get("images") or [])],
                override_image_paths=[str(p) for p in (run_opts.get("image_paths") or [])],
                override_files=[dict(f) for f in (run_opts.get("files") or []) if isinstance(f, dict)],
            )
        else:
            requirement, images = await self._load_context(run.team_id, run.trigger_msg_id,
                                                           session_profile,
                                                           project_id=run.project_id,
                                                           workspace=workspace,
                                                           session_skills=session_skills)
        # 连续对话：注入同会话历史消息（排除当前触发消息），让模型带着上下文理解本轮诉求
        history = await self._history_block(run.session_id, run.trigger_msg_id)
        # 续跑前沿：上一轮停在哪、清单剩什么、已经产出哪些文件。会话历史里只有聊天正文
        #（还被截断过），plan_json 与产物从来不会传到下一轮——重新提需求等于冷启动。
        resume = await self._resume_block(run)
        context_block = "\n\n".join(x for x in (history, resume) if x)
        if context_block:
            requirement = (f"{context_block}\n\n---\n"
                           f"以下是用户的最新要求：\n{requirement}")
        # 意图预判（硬提示）：把「要图/要音频」这类素材需求前置成结构化指令，
        # 避免模型绕道 grep/glob 翻脚本、最后什么也没生成（实测会耗光轮次）
        latest = ""
        if run.trigger_msg_id:
            async with self._sf() as s:
                _m = await s.get(Message, run.trigger_msg_id)
                # 续跑消息（手动「继续」或系统自动续跑）的正文只是一句「继续完成任务目标」，
                # 不能拿它当「用户原话」来判定素材路由/多步计划——顺着续跑链回溯到最初那条
                # 真实需求的原话。手动继续的标记是 continue_from，自动续跑是 auto_continue_from。
                latest = await self._resolve_user_request(s, _m)
        elif continue_brief:
            # 无触发消息的续跑：从续跑说明里取回「上次请求原文」，用于素材意图/计划提示
            latest = self._brief_original_request(continue_brief)
        # 意图盘查（2026-11-14 用户拍板「保留判别、但不当闸门」）：
        # 结论**只用于两件事**——(1) 给模型一句软提示；(2) 决定「只勘察不动手」刹车开不开。
        # 明确**不做**的三件事（去掉了以前的硬闸门）：
        #   · 不把 run_type 降级成 analyze（判成 survey 也只提醒，不替模型决定「本轮不许改」）；
        #   · 不塞「本轮不要修改产品文件」这类禁手硬提示（用户说「检查一下然后改掉」不再被禁手）；
        #   · 不裁任何工具（caps.effective_whitelist 本就不按轮次类型裁剪）。
        # 唯一仍然照办的硬约束是**用户亲手点「仅分析」**（run_type 由前端写入，不是本层猜的）。
        inspect_only = run.run_type == "analyze"
        if inspect_only:
            intent = IntentResult(intent="survey", mode="build",
                                  reason="用户显式仅分析", decided_by="explicit")
        else:
            try:
                profile = await self._load_profile(
                    run.team_id, run_opts.get("model_profile_key")
                    or (await self._agent_profile_key(run.team_id, "main")))
            except Exception:  # noqa: BLE001 - 取档位失败就交给 factory 兜底
                profile = {}
            intent = await classify_intent(latest, profile=profile)
            # 软提示：拿不准/纯问答时只说一句「你自己判断」，绝不出现禁手词（见 intent.hint_for）
            soft = intent_hint_for(intent)
            if soft:
                requirement = f"{soft}\n\n---\n{requirement}"
        if inspect_only:
            # 用户亲手点了「仅分析」：那是显式指令，照办——只给结论，不动产品文件。
            requirement = f"{self._inspect_hint()}\n\n---\n{requirement}"
        else:
            hint = self._intent_hint(latest)
            ask_hint = self._ask_hint(requirement_text=latest, full=requirement)
            if hint:
                requirement = f"{hint}\n\n---\n{requirement}"
            elif ask_hint:
                requirement = f"{ask_hint}\n\n---\n{requirement}"
            else:
                plan_hint = self._plan_hint(latest)
                if plan_hint:
                    requirement = f"{plan_hint}\n\n---\n{requirement}"

        run_ctx = RunContext(
            run_id=run_id, team_id=run.team_id, project_id=run.project_id,
            session_id=run.session_id, triggered_by=run.triggered_by,
            run_type=run.run_type, worker_id=self.worker_id,
            repo=repo, bus=self.svcs.bus, locks=self.svcs.locks, store=self.svcs.store,
            user_request=latest,
            meta={"caps": caps_mod.normalize_caps(run_opts.get("caps")),
                  "model_profile_key": run_opts.get("model_profile_key"),
                  "reasoning_effort": run_opts.get("reasoning_effort"),
                  "permission_scope": perm_scope,
                  # 意图盘查结论（2026-11-14）：只用于提示措辞与刹车开关，不封锁任何动作。
                  # engage=False（盘查判纯勘察）时 runtime 不装「只勘察不动手」刹车——
                  # 用户只要一个结论时不该被催着改文件；engage=True 才催落地。
                  # 用户显式「仅分析」时 decided_by=explicit、intent=survey → engage 也是 False。
                  "intent": intent.intent, "intent_mode": intent.mode,
                  "engage": intent.intent == "modify",
                  "fix_task": intent.is_fix},
        )
        # 能力开关（测试/联网/媒体）写进本轮提示：关掉的明确「不做什么」
        cap_hint = caps_mod.caps_hint(caps_mod.normalize_caps(run_ctx.meta.get("caps")))
        if cap_hint:
            requirement = f"{cap_hint}\n\n---\n{requirement}"

        remembered = False
        blocked = False  # 助手提问等用户回答：任务没结束，暂存区要留给下一轮
        usage: dict = {}  # 本轮已消耗的 token/延迟（收尾写 runs.usage_json）
        try:
            if run.run_type == "analyze":
                _extra = {"run_type": "analyze"} if inspect_only else {}
                await repo.update_run(run_id, status="running", worker_id=self.worker_id, **_extra)
                await self._emit(run_ctx, "status", {"status": "running"})
                result = await self.run_agent("main", None, requirement, run_ctx, images=images)
                usage = _merge_usage(usage, getattr(result, "usage", None))
                # 同 modify 路径：模型异常退场时 run 落 failed，不把错误文本当交付
                if (result.text or "").lstrip().startswith("[main] 执行出错"):
                    raise RuntimeError((result.text or "").strip())
                await self._close_out_plan(run_ctx)
                final_text = self._prefer_summary(result, run_id)
                await repo.update_run(run_id, status="succeeded", finished_at=_now(),
                                     final_summary=final_text,
                                     usage_json=_run_usage_json(usage))
                self._summaries[run_id] = final_text
                # 先落回复消息（聊天流立即可见），紧接着发 run_done。
                # 为什么 run_done 必须紧贴回复落地：前端的完成提醒（提示音 + 桌面通知）
                # 就是靠这个事件触发的。以前它排在 AI 记忆合并之后，而记忆合并是一次
                # 模型调用（最长 90s）——用户看着回复已经出现在聊天里，提醒却要等一两分钟，
                # 表现就是「都结束了还没提醒」。
                await self._post_reply(run_ctx, run, final_text)
                await self._emit(run_ctx, "run_done", {"status": "succeeded"})
                remembered = True  # 记忆合并失败也要置位：否则 finally 里会再烧一次模型调用
                try:
                    await self._remember(run.project_id, run.team_id, run_id,
                                         await self._memory_input_text(run_id, final_text))
                except Exception:  # noqa: BLE001 - 收尾慢活失败不许改写已成功的 run
                    log.exception("收尾记忆合并失败", extra={"run_id": run_id})
                return

            # modify：平台不接管 git，改动直接落在工作区上（改动审阅/回滚走写前快照）。
            # 同项目要不要串行只剩 SAME_PROJECT_PARALLEL 这一条规则。
            token: Optional[int] = None
            if not SAME_PROJECT_PARALLEL:
                token = await self._acquire_with_wait(run, project.id)
                run_ctx.fencing = self.svcs.locks.make_fencing(project.id, token)
            await repo.update_run(run_id, status="running",
                                 worker_id=self.worker_id, lock_fencing=token)
            await self._emit(run_ctx, "status", {"status": "running"})

            hb = asyncio.create_task(self._heartbeat(project.id, run.id, token)) if token is not None else None

            main_res = await self.run_agent("main", None, requirement, run_ctx, images=images)
            usage = _merge_usage(usage, getattr(main_res, "usage", None))
            # 模型带着异常退场（LLM 5 连败、端点连不上等）：run 必须落 failed。
            # 错误文本当正常回复标 succeeded，界面就是「执行完毕」下面挂一条执行出错。
            # 抛出去走外层 except：failed 状态 + 系统错误消息 + run_done(failed) + 放锁，一条龙。
            # 放在纠正循环之前：端点都连不上了，补落地动作的纠正只会再烧 5 次重试。
            if (main_res.text or "").lstrip().startswith("[main] 执行出错"):
                raise RuntimeError((main_res.text or "").strip())
            # 纠正重跑前，先把第一轮「已交卷」的汇报落成一条聊天消息。
            # 为什么：纠正会把同一个 run 完整重跑一遍，第二轮的 report_to_user 会覆盖
            # _summaries、并且最终只落一条 _post_reply——于是第一轮那段正确的答复在聊天里
            # 彻底消失（实测 run 9a9656bc：5 分钟的准确诊断报告，被 23 分钟的强制重做顶掉，
            # 用户在聊天里根本看不到）。只在**确实要纠正重跑**时才先把这份答复发出去，
            # 正常一次收尾的路径完全不变；_posted_replies 去重保证它不会被收尾那条重复。
            # 收尾不再按「系统预判的意图」逼模型动手（2026-11-14 改）。
            # 旧逻辑：intent=modify 且零落地 → 强制重跑最多 2 次。问题是「要不要落地」本来就是
            # 模型该自己判断的（用户问「这个项目是干嘛的」也会被当成要动手，白重跑两轮）。
            # 现在只纠正一种**自相矛盾**的情况：模型自己说要改（立了清单/说改完），却一个产品
            # 改动都没落。纯问答/只给方案不在此列——那是模型对需求的正常判断，不该被推翻。
            plan_items_now = await self._plan_items(run_id)
            open_items_now = [i for i in plan_items_now if str(i.get("status")) != "completed"]
            claimed_action = self._claims_action(main_res, self._summaries.get(run_id) or "")
            landed = await self._landed_actions(run_id)
            will_correct = bool(open_items_now) or (claimed_action and landed == 0)
            if will_correct and self._summaries.get(run_id):
                await self._post_reply(run_ctx, run, self._summaries[run_id])
            # 「只勘察、不动手」守卫（用户最常抱怨的失职）：
            # 但触发条件改成模型自己的承诺：清单里还有未完成项，或汇报里明确说了「已改/已生成」。
            nudge = (
                "【系统纠正】你刚才只做了定位/勘察，没有任何落地动作"
                "（没写文件、没生成素材、没起服务），"
                "但你的待办清单/汇报里还留着要做的事。用户要的是「改完能用」，不是勘察报告。现在立刻执行：\n"
                "· 要改代码：自己用 write_file / edit_file 直接改（路径/行号你已经查到了）；\n"
                "· 要素材：generate_image / generate_audio 直接生成；\n"
                "· 改完自己验证：run_command 跑测试/编译就够（起服务 + 截图是重手段，"
                "用户明确要看效果或同一问题多轮没改好时才用），发现问题接着改；"
                "最后 report_to_user 汇报改了什么、产物路径、验证结果。\n"
                "「没拿到确切行号 / 读不到文件某段 / 不确定 class 名」都不是停工理由："
                "用 read_file(path, offset, limit) 按行翻页、grep(pattern, path, context) 带上下文定位。\n"
                "如果用户这一轮本来只要你给结论/方案（不要求动文件），把清单里那些项改成"
                "「不涉及改动」并说明即可，不要为了凑数去改代码。"
            )
            for attempt in range(1, 3):
                open_items_now = [i for i in await self._plan_items(run_id)
                                  if str(i.get("status")) != "completed"]
                if not (open_items_now or (
                        self._claims_action(main_res, self._summaries.get(run_id) or "")
                        and await self._landed_actions(run_id) == 0)):
                    break
                await self._emit(run_ctx, "status",
                                 {"status": "running", "note": f"补齐落地动作（第 {attempt} 次纠正）"})
                main_res = await self.run_agent(
                    "main", None, f"{requirement}\n\n---\n{nudge}", run_ctx, images=images)
                usage = _merge_usage(usage, getattr(main_res, "usage", None))
            # 纠正完仍然只有勘察、没有任何落地：**不要把 run 判死成 failed**。
            # 用户明确要求「不要限制他」——把一个还在读代码、正在定位的任务标成「执行失败」，
            # 再在输入框上方弹出「这次只做了定位/勘察…请重试」的浮层，既误导（让人以为这条
            # 任务废了、要重新描述），又违背「系统自己接着往下推」的收尾原则。
            # 改法：不落 failed、不弹失败浮层，照常收尾；由下面的 _auto_continue 自动续跑，
            # 把没做完的活接着做完（若它连清单都没立，就临时造一条待办驱动续跑）。
            landed_zero = await self._landed_actions(run_id) == 0
            if landed_zero:
                log.info("run finished without product change; auto-continue will keep pushing "
                         "only if the plan is still open",
                         extra={"run_id": run_id})
            final_summary = self._prefer_summary(main_res, run_id)
            await repo.update_run(run_id, final_summary=final_summary,
                                  usage_json=_run_usage_json(usage))

            # 收尾：变更报告（改动清单来自工具层写前快照，不看 git）
            run_after = await repo.get_run(run_id)
            artifacts = await repo.list_artifacts(run_id)
            cr = change_report_mod.build_change_report(
                workspace, run_id=run_id, run_type="modify",
                test_report=self._test_reports.get(run_id),
                has_placeholder=bool(run_after and run_after.has_placeholder), artifacts=artifacts,
                model_map={r: (ROLE_MODELS.get(r) or LLM_MODEL) for r in ROLE_TOOLS},
            )
            await repo.update_run(run_id, status="succeeded", finished_at=_now(),
                                 change_report_json=cr, final_summary=final_summary,
                                 usage_json=_run_usage_json(usage))
            plan_items, open_items = await self._close_out_plan(run_ctx)
            # 先落回复消息（聊天流立即可见），收尾的慢活全部挪到 run_done 之后再干。
            await self._post_reply(run_ctx, run, final_summary)
            if hb is not None:
                hb.cancel()
            if token is not None:
                try:
                    await self.svcs.locks.release(project.id, run.id, token)
                except Exception:  # noqa: BLE001
                    pass
            # 清单没做完 = 活儿没做完：直接排队续跑一轮，而不是等用户自己发现再点「继续」。
            # survey-only（只勘察没落地）也一样要续：哪怕它根本没立清单，也造一条「把活做完」
            # 的待办塞进续跑，确保这次「不判 failed」不会被理解成「这次就到此为止了」。
            # 续跑只是入队（毫秒级），留在 run_done 之前，保证「排队续跑」的提示先到、终态后到。
            ac_open = open_items
            if landed_zero and not ac_open:
                ac_open = [{"content": "把上一轮只做了勘察/定位、还没落地的活做完",
                            "status": "pending", "note": "上一轮未落地"}]
            await self._auto_continue(run_ctx, run, plan_items, ac_open,
                                      final_summary, workspace)
            # run_done 必须紧贴「回复已落地 + 续跑已入队」发出：前端的完成提醒
            # （提示音 + 桌面通知）就是靠它触发的。以前它排在 AI 记忆合并（最长 90s）
            # 和经验沉淀（最长 60s）之后，用户看着回复早出来了、提醒却要等一两分钟——
            # 表现就是「都结束了还没提醒」。
            await self._emit(run_ctx, "run_done", {"status": "succeeded",
                                                  "change_report": cr})
            log.info("run succeeded", extra={"run_id": run_id})
            # run_done 之后只剩慢活（各一次模型调用）和落库：全部兜底，任何一件失败都不许
            # 把已经成功的 run 再判成 failed —— 那会额外发一条 run_done(failed)，
            # 界面把刚做完的活标成失败，比不提醒更糟。
            remembered = True  # 记忆合并失败也要置位：否则 finally 里会再烧一次模型调用
            try:
                await self._remember(run.project_id, run.team_id, run_id,
                                     await self._memory_input_text(run_id, final_summary, cr))
                await self._harvest_notes(run_ctx, run.team_id, workspace, latest,
                                          final_summary, cr, run_id)
            except Exception:  # noqa: BLE001 - 收尾慢活失败不影响已完成状态
                log.exception("run 收尾慢活失败", extra={"run_id": run_id})
        except FencingLost:
            await repo.update_run(run_id, status="failed", error="锁丢失，已中止以防双写",
                                 finished_at=_now(), usage_json=_run_usage_json(usage))
            await self._post_system_error(run_ctx, run, "锁丢失，已中止以防双写")
            await self._emit(run_ctx, "run_done", {"status": "failed", "error": "fencing lost"})
        except AskUserStop as stop:
            # 助手提问：run 落「等待回答」（blocked），问题卡片已由 ask_user 落库并广播。
            # 用户作答后 → POST /questions/{id}/answer 把答案作为一条 user 消息写入同一会话
            # 并重新入队一个 run（会话历史会重新注入），等于「接着刚才那步继续」。
            await repo.update_run(run_id, status="blocked", finished_at=_now(),
                                 final_summary=stop.question.get("question", ""),
                                 usage_json=_run_usage_json(usage))
            blocked = True
            await self._emit(run_ctx, "run_done",
                             {"status": "blocked", "question": stop.question})
        except asyncio.CancelledError:
            # 区分「用户取消」和「服务关闭」：两者都表现为 CancelledError，但事实不同，
            # 措辞不能一概说成「已按你的要求取消」（那会让重启后看到一堆假取消记录）。
            # 整轮停止时，主agent 的取消会向下传播到各子agent；但传播有极小概率漏掉
            # 某个「刚好切换 task/正在收尾」的子任务，这里再兜底扫一遍：把仍 running 的子
            # task 落 cancelled 并补 task_done，保证前端子agent 分组不会永远停在「运行中」。
            await self._close_open_tasks(run_ctx, run_id)
            from game_agent.core.shutdown import is_shutting_down
            _shutdown = is_shutting_down()
            if _shutdown:
                await repo.update_run(run_id, status="failed",
                                     error="服务重启，运行中断，请重新发起", finished_at=_now(),
                                     usage_json=_run_usage_json(usage))
                await self._post_system_note(
                    run_ctx, run, "服务重启，本次执行被中断，请重新发起。")
                await self._emit(run_ctx, "run_done", {"status": "failed", "error": "服务重启中断"})
            else:
                await repo.update_run(run_id, status="cancelled", finished_at=_now(),
                                     usage_json=_run_usage_json(usage))
                await self._post_system_note(
                    run_ctx, run, "已按你的要求取消本次执行（已停止工具调用）。")
                await self._emit(run_ctx, "run_done", {"status": "cancelled"})
            # 区分两种取消：① 用户点「取消」→ 任务级 CancelledError（本 worker 未被 cancel），
            # 收尾后正常返回，绝不能把 CancelledError 抛进 worker_loop（那会永久打死一个 worker，
            # 表现为取消几次后整个应用不再执行任何任务）；② 服务关闭 → 本任务自身被 cancel，需继续抛。
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
        except Exception as e:  # noqa: BLE001
            log.exception("run failed")
            await repo.update_run(run_id, status="failed", error=str(e)[:500],
                                 finished_at=_now(), usage_json=_run_usage_json(usage))
            await self._post_system_error(run_ctx, run, str(e))
            await self._emit(run_ctx, "run_done", {"status": "failed", "error": str(e)[:300]})
        finally:
            # 确保锁释放
            try:
                if run_ctx.fencing is not None:
                    await self.svcs.locks.release(project.id, run.id, run_ctx.fencing.token)
            except Exception:  # noqa: BLE001
                pass
            # 收尾清理过程临时文件（放在放锁之后：锁已让出，此时若已有并行 run 抢到锁并
            # 在跑，下面的 siblings 查询会看见它 → 只删本 run 登记的，不整目录清）
            # blocked（等用户回答）不算会话结束——下一轮还要接着用，这里的标记留到那轮收尾再算
            if not blocked:
                try:
                    from game_agent.agents.tools.service import kill_run_services
                    # 先收后台服务：Windows 上日志句柄不关，文件就删不掉
                    kill_run_services(run_id)
                    async with self._sf() as s:
                        # run_id 是入参 str（32 位 hex），Uuid 列的绑定器只认 UUID 对象，
                        # 不转的话每次 run 收尾这里都炸一次、临时文件永远清不掉
                        siblings = (await s.execute(
                            select(Run.id).where(
                                Run.project_id == project.id,
                                Run.status.in_(("running", "claimed")),
                                Run.id != _u(run_id)))).all()
                        # 关服务看的是「整机还有没有别人在用显卡」，跨项目也算
                        others_active = (await s.execute(
                            select(Run.id).where(
                                Run.status.in_(("running", "claimed")),
                                Run.id != _u(run_id)))).all()
                    # 会话结束：只关这个会话自己起过的本地媒体服务
                    # （ComfyUI 生图 / IndexTTS2 配音），没起过的不碰——工作台、别的会话
                    # 起的服务不替它收摊（2026-10-30 用户要求）。
                    # 还有别的 run 在跑时不能关：GPU 服务是整机共用的，以前这里一关就把
                    # 别的会话正在出的图/正在配的音掐掉，下一张图还得再花 15s 重新拉服务
                    # （2026-10-30 凌晨：三个会话并行，收尾反复关 ComfyUI，配合 300s 超时
                    # 把一批图拖成「控制台一直刷进度条、素材条不涨」）。
                    from game_agent.assets import gpu_arbiter as _gpu
                    used_kinds = _gpu.take_used(run.session_id)
                    if not others_active:
                        used_kinds |= _gpu.take_pending()
                        await self._shutdown_media_services(run_ctx, run, used_kinds)
                    else:
                        _gpu.defer_stop(used_kinds)
                        log.info("其他 run 仍在运行（%d 条），跳过关闭本地媒体服务",
                                 len(others_active))
                    sweep_run(run_id, workspace, full=not siblings)
                except Exception:  # noqa: BLE001
                    log.warning("收尾清理过程临时文件失败", exc_info=True)
            # 兜底：本轮没走成功收尾，但确实往工作区落了改动（用户中途取消 / 失败发生在改完之后）
            # 也要补记项目记忆，否则「记忆永远停在上一轮」，用户会觉得记忆从不自动更新
            if not remembered and run.run_type == "modify":
                await self._remember_if_dirty(run, run_id)

    async def _shutdown_media_services(self, run_ctx: RunContext, run,
                                       kinds: set[str] | None = None) -> None:
        """run 收尾：关闭本地美术（ComfyUI）/配音（IndexTTS2）服务，释放显存。

        GPU 互斥只保证「同一时刻只有一个媒体服务占着 GPU」；本次会话结束后两边都该退场，
        不留后台进程吃显存。用户在配置里绑的是远程档位时，本地 stop 只是查不到端口、
        自然跳过，无害。只有真停掉了进程才发系统提示，避免每轮 run 都刷一句废话。
        kinds 为空集时说明这个 run 根本没起过生图/配音（纯文本任务），直接跳过，
        不要顺手把工作台或别的会话正在用的服务关掉。
        """
        if kinds is not None and not kinds:
            log.info("本次 run 未使用本地生图/配音服务，跳过收尾关闭")
            return
        try:
            img_base = aud_base = ""
            try:
                img_prof = await self.resolve_media_profile(str(run.team_id), "image")
                img_base = (img_prof or {}).get("base_url") or ""
                aud_prof = await self.resolve_media_profile(str(run.team_id), "audio")
                aud_base = (aud_prof or {}).get("base_url") or ""
            except Exception:  # noqa: BLE001 - 档位解析失败不影响关服务（走全局配置兜底）
                log.debug("收尾解析媒体档位失败，用全局配置关闭", exc_info=True)
            from game_agent.assets.gpu_arbiter import shutdown_media_services
            note = await shutdown_media_services(img_base, aud_base, kinds=kinds)
            if note:
                await self._post_system_note(run_ctx, run, note)
        except Exception:  # noqa: BLE001 - 收尾关服务失败不影响 run 状态
            log.warning("run 收尾关闭媒体服务失败", exc_info=True)

    async def _remember_if_dirty(self, run, run_id: str) -> None:
        """非成功终态但已落地改动时，补记一条项目记忆（带「未正常收尾」标记）。"""
        try:
            if await self._landed_actions(run_id) <= 0:
                return
            text = await self._memory_input_text(run_id, "", None)
            if not text.strip():
                return
            req = ""
            try:
                if run.trigger_msg_id:
                    async with self._sf() as s:
                        m = await s.get(Message, _u(run.trigger_msg_id))
                        # 续跑消息正文只有「继续完成任务目标」，要回溯到真正的原话
                        req = (await self._resolve_user_request(s, m)).strip()[:160] if m is not None else ""
            except Exception:  # noqa: BLE001
                req = ""
            if req:
                text = f"本次用户需求：{req}\n{text}"
            await self._remember(run.project_id, run.team_id, run_id,
                                 f"（这次没有正常收尾，但以下改动已经落在工作区，按当前事实记入）\n{text}")
            log.info("project memory updated from partial run",
                     extra={"run_id": str(run_id)})
        except Exception:  # noqa: BLE001 - 补记失败不影响 run 收尾
            log.exception("补记项目记忆失败", extra={"run_id": run_id})

    async def _acquire_with_wait(self, run: Run, project_id: str) -> int:
        try:
            return await self.svcs.locks.acquire(project_id, run.id, "exclusive", LOCK_TTL_S)
        except Exception:  # noqa: BLE001 - LockBusy
            await self.svcs.repo.update_run(run.id, status="blocked")
            await self._emit(RunContext(run_id=run.id, team_id=run.team_id,
                                        project_id=run.project_id, session_id=run.session_id,
                                        triggered_by=run.triggered_by, run_type=run.run_type,
                                        worker_id=self.worker_id, repo=self.svcs.repo,
                                        bus=self.svcs.bus, locks=self.svcs.locks,
                                        store=self.svcs.store),
                           "status", {"status": "blocked"})
            # 排队等锁：一次任务没有时长上限，同项目的另一条 run 可能跑很久。
            # 上限用 LOCK_WAIT_S（默认 30 分钟），到点如实报「等锁超时」而不是静默卡住。
            deadline = time.monotonic() + LOCK_WAIT_S
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                try:
                    return await self.svcs.locks.acquire(project_id, run.id, "exclusive", LOCK_TTL_S)
                except Exception:  # noqa: BLE001
                    continue
            raise RuntimeError(f"等待写锁超过 {LOCK_WAIT_S // 60} 分钟（同项目另一条任务仍在运行）")

    async def _heartbeat(self, project_id: str, run_id: str, token: int) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                await self.svcs.locks.heartbeat(project_id, run_id, token)
        except asyncio.CancelledError:
            return

    # ---------- 嵌套子任务（媒体/纠错等内部复用入口） ----------
    async def run_agent(self, role: str, parent_task_id: Optional[str], instruction: str,
                        run_ctx: RunContext, payload: dict | None = None,
                        images: list[str] | None = None,
                        task_title: str | None = None) -> AgentResult:
        repo = self.svcs.repo
        _init_result = None
        if payload and payload.get("question"):
            _init_result = {"question": payload.get("question")}
        task = await repo.create_task(
            run_id=run_ctx.run_id, parent_task_id=parent_task_id, kind=role,
            agent_key=role, title=task_title or f"{role} 执行", status="running",
            attempt=1, started_at=_now(), result_json=_init_result,
        )
        await self._emit(run_ctx, "task_start", {
            "task_id": str(task.id), "role": role, "kind": role,
            "parent_task_id": str(parent_task_id) if parent_task_id else None,
            # spawn_agents 派发时传进来的子任务名/问题：前端把它显示成组标题，
            # 用户才知道每个子agent 各自负责哪条线索（而不是一串相同的 sub 标签）
            "title": task_title or None,
            "question": (payload or {}).get("question") or None,
        })

        # 角色全量白名单（不做任何裁剪）：运行中改「仅分析 / 能力开关」时，runtime 要在这份
        # 全量基础上重算生效白名单，而不是在已裁剪的集合上再裁（否则关掉再打开也回不来）。
        role_whitelist = list(ROLE_TOOLS.get(role, []))
        # 本轮生效白名单：只按用户手动的能力开关裁剪，轮次类型不裁
        whitelist = caps_mod.effective_whitelist(
            role_whitelist, run_ctx.run_type, (run_ctx.meta or {}).get("caps"))

        async with self._sf() as s:
            cfg = await get_agent_config(s, run_ctx.team_id, role)
        # 本轮选项（输入框模型选择器）覆盖角色绑定档位：只在用户显式选择时生效
        opts = run_ctx.meta or {}
        profile_key = await self._resolve_profile_key(
            run_ctx.team_id, role, opts, own_key=cfg.model_profile_key)
        profile = await self._load_profile(run_ctx.team_id, profile_key)
        # 推理强度：本轮显式选择 > 本角色默认（agents.params_json，见 _agent_defaults）。
        # 按「本角色」而不是主agent 取：子agent 的默认仍是 factory 里的
        # GA_SUB_REASONING_EFFORT（默认 none，省 token），不会被主agent 的默认带跑。
        effort = (_norm_effort(opts.get("reasoning_effort"))
                  or _norm_effort((cfg.params_json or {}).get("reasoning_effort")))
        if effort:
            profile = {**profile,
                       "params": {**(profile.get("params") or {}),
                                  "reasoning_effort": effort}}

        project = await repo.get_project(run_ctx.project_id)
        workspace = PROJECTS_ROOT / project.repo_path
        python_bin = project.python_bin or default_python_bin() or None

        # MCP 动态工具：项目 MCP 配置（界面 CRUD / .trace/mcp.json 兜底）声明即注册
        extra_specs: dict[str, dict] = {}
        try:
            from game_agent.agents.tools.mcp_client import get_mcp_manager
            mgr = await get_mcp_manager(run_ctx.project_id, workspace)
            if mgr is not None:
                for mt in await mgr.list_tools():
                    extra_specs[mt["name"]] = {"description": mt["description"],
                                               "parameters": mt["schema"]}
                if extra_specs:
                    log.info("mcp tools attached: %s", ", ".join(extra_specs),
                             extra={"run_id": run_ctx.run_id})
        except Exception:  # noqa: BLE001 - MCP 不可用不影响主流程
            log.exception("load mcp tools failed")
        whitelist = whitelist + list(extra_specs)

        python_bin = python_bin  # noqa: F841 - 保持下方 manifest 引用顺序清晰
        audio_line = await self._audio_capability_line(str(run_ctx.team_id))
        manifest = self._build_manifest(project, run_ctx, python_bin, audio_line)
        # 每轮的硬约束块（指导项 + 权限作用域）必须放在系统提示**最前面**：
        # 实测（2026-11-02）挂在末尾时，前面 6k 字角色提示 + 运行环境把它的存在感压没了，
        # 模型仍然按旧习惯直接去读工作区外路径。位置在 prompt 头部才会被当成硬规则。
        lead_blocks: list[str] = []
        try:
            pol = await policy_mod.load_policy()
            guidance = policy_mod.guidance_block(pol.get("guidance") or [])
            if guidance:
                lead_blocks.append(guidance)
        except Exception:  # noqa: BLE001 - 指导项读取失败不影响 run
            log.exception("load guidance failed")
        lead_blocks.append(policy_mod.permission_hint(
            policy_mod.normalize_scope((run_ctx.meta or {}).get("permission_scope"))))
        system_prompt = ("\n\n".join(lead_blocks) + "\n\n"
                         + cfg.system_prompt + "\n\n" + manifest)

        from game_agent.agents.tools import ToolContext
        from game_agent.core.skills import get_extra_roots, roots_for
        try:
            extra_roots = await get_extra_roots()
        except Exception:  # noqa: BLE001 - 自定义技能目录读不到不阻塞 run
            extra_roots = []
        tctx = ToolContext(
            run_ctx=run_ctx, workspace=workspace, role=role, task_id=str(task.id),
            orchestrator=self, python_bin=python_bin,
            skill_roots=roots_for(workspace, extra_roots),
            permission_scope=policy_mod.normalize_scope(
                (run_ctx.meta or {}).get("permission_scope")),
        )
        ctx = Ctx(
            role=role, run_type=run_ctx.run_type, workspace=str(workspace),
            requirement=instruction, project_name=project.name,
            commands=project.commands_json or {}, payload=payload or {},
            model=(profile.get("model") or ROLE_MODELS.get(role) or LLM_MODEL),
            images=list(images or []),
            model_profile=profile,
        )
        from game_agent.agents.runtime import AgentRuntime
        runtime = AgentRuntime(role, whitelist, system_prompt, instruction, tctx, ctx,
                              extra_tool_specs=extra_specs,
                              # 角色全量白名单：运行中改运行选项时在它上面重算（MCP 工具由
                              # runtime 用 extra_specs 另加），而不是在已裁剪的 whitelist 上
                              # 二次裁剪（否则关掉再打开回不来）
                              base_whitelist=role_whitelist,
                              # 意图推理结论：决定是否启用「只勘察不动手」过程刹车
                              engage=bool((run_ctx.meta or {}).get("engage", True)),
                              fix_task=bool((run_ctx.meta or {}).get("fix_task", True)))

        # 取消来源：'run' = 用户点了整轮停止（主+子一起停）；'task' = 用户只点掉这个子agent。
        # 看门狗写入，CancelledError 分支据此决定「向上抛（连带整轮）」还是「就地止损只回
        # 一段说明（让主agent 继续）」。为什么不区分就必出错：子agent 被单独点掉时若直接
        # raise，CancelledError 会穿过 spawn_agents 的 asyncio.gather，把主agent 一起打断。
        cancel_scope: dict[str, str | None] = {"scope": None}

        async def _watch_cancel(rt_task: "asyncio.Task") -> None:
            """取消看门狗：用户点「取消」后立即把 agent 任务掐掉。

            为什么必须做：只在工具轮次之间检查 cancel_requested 是不够的——一次 LLM 调用
            （长思考/长上下文）或一次长工具调用就能挂几分钟，用户点了取消却「看着它继续跑」。
            这里 1s 轮询一次，置位即 rt_task.cancel()，中断正在进行的 HTTP 流。

            两种取消都要看：
              · run 级（runs.cancel_requested）—— 停止整轮，主agent 与所有子agent 一起停；
              · task 级（tasks.cancel_requested）—— 只停这个子agent，其余照跑。

            参数名不能叫 task：外层 DB 任务对象也叫 task，子agent 分支查库要用它的 id。
            以前参数就叫 task，`str(task.id)` 实际作用在 asyncio.Task 上 →
            AttributeError → 看门狗第一次轮询就永久退出 → 单个子agent 的「停止」永远不生效
            （2026-11-12 用户实测：5 个子agent 全部卡在「停止中…」）。
            单次轮询失败也不能退出：一次 DB 抖动就把看门狗打死，整轮停止会跟着失效。
            """
            while not rt_task.done():
                try:
                    await asyncio.sleep(1.0)
                    cur = await repo.get_run(run_ctx.run_id)
                    if cur is not None and getattr(cur, "cancel_requested", False):
                        cancel_scope["scope"] = "run"
                        log.info("run cancel requested, aborting agent task immediately",
                                 extra={"run_id": run_ctx.run_id, "role": role})
                        rt_task.cancel()
                        return
                    if role == "sub":
                        # 单个子agent 的独立停止位：只影响这一条子任务。
                        tcur = await repo.get_task(str(task.id))
                        if tcur is not None and getattr(tcur, "cancel_requested", False):
                            cancel_scope["scope"] = "task"
                            log.info("subagent task cancel requested, stopping it only",
                                     extra={"run_id": run_ctx.run_id,
                                            "role": role, "task_id": str(task.id)})
                            rt_task.cancel()
                            return
                except asyncio.CancelledError:
                    if rt_task.done():
                        return
                    raise
                except Exception:  # noqa: BLE001 - 单次轮询失败不影响下一轮
                    log.exception("cancel watcher poll failed, retrying")

        agent_task = asyncio.create_task(runtime.run())
        watcher = asyncio.create_task(_watch_cancel(agent_task))
        try:
            result = await agent_task
        except asyncio.CancelledError:
            await repo.update_task(str(task.id), status="cancelled", finished_at=_now())
            await self._emit(run_ctx, "task_done",
                             {"task_id": str(task.id), "role": role, "status": "cancelled"})
            # 单个子agent 被用户单独点掉：不向上抛（否则会连带打断主agent），
            # 回一段「已停止」的说明给主agent，让它知道这条线索没跑完、可以自己接手或再派。
            if role == "sub" and cancel_scope["scope"] == "task":
                return AgentResult(text="[sub] 已按用户要求停止这个子agent（本次核对未完成）。")
            raise
        except FencingLost:
            await repo.update_task(str(task.id), status="failed", finished_at=_now())
            await self._emit(run_ctx, "task_done",
                             {"task_id": str(task.id), "role": role, "status": "failed",
                              "error": "锁丢失，已中止"})
            raise
        except AskUserStop as stop:
            # 助手提问：任务按"完成"收口（不算失败），问题本身已由 ask_user 落库广播
            await repo.update_task(str(task.id), status="done", finished_at=_now(),
                                  result_json={"question": stop.question})
            await self._emit(run_ctx, "task_done",
                             {"task_id": str(task.id), "role": role, "status": "question"})
            raise
        except LlmAuthError as e:
            # 端点认证/额度错误：不能把错误文本当正常回复。
            await repo.update_task(str(task.id), status="failed", finished_at=_now(),
                                  result_json={"error": "LLM 端点认证/额度错误"})
            await self._emit(run_ctx, "task_done",
                             {"task_id": str(task.id), "role": role, "status": "failed",
                              "error": "LLM 端点认证/额度错误"})
            # 子agent 端点报错不该带崩整轮：回错误文本交给主agent 判断（它可能换个线索、
            # 或自己接手），而不是让 run 直接 failed。主agent 仍按原逻辑抛出让 run 落 failed。
            if role == "sub":
                return AgentResult(text=f"[sub] 执行出错：LLM 端点认证/额度错误（{e}）")
            raise
        except asyncio.TimeoutError:
            # 单次 LLM/工具调用的超时在各自适配层内部已吞掉重试，能冒到这里说明
            # 是真的挂住了：落成 failed，让看门狗之外还有一条明确的失败路径。
            await repo.update_task(str(task.id), status="failed", finished_at=_now())
            await self._emit(run_ctx, "task_done",
                             {"task_id": str(task.id), "role": role, "status": "failed",
                              "error": "调用挂住超时"})
            if role == "sub":
                return AgentResult(text="[sub] 执行出错：调用挂住超时")
            raise RuntimeError("run_agent 调用挂住超时")
        except Exception as e:  # noqa: BLE001
            await repo.update_task(str(task.id), status="failed", finished_at=_now(),
                                  result_json={"error": str(e)[:500]})
            # 关键：generic 失败也要广播 task_done(failed)——否则前端子agent 分组永远停在
            # 「运行中」（open=还没收到 task_done），用户看不到它其实已经报错了。
            await self._emit(run_ctx, "task_done",
                             {"task_id": str(task.id), "role": role, "status": "failed",
                              "error": f"{type(e).__name__}: {str(e)[:300]}"})
            # 子智能体失败：返回错误文本，由父级决定重试/上报
            return AgentResult(text=f"[{role}] 执行出错：{type(e).__name__}: {e}")
        else:
            # 子agent 的结论要带给前端：task_done 以前只带 status，用户在弹窗里只看到
            # 工具调用流水，看不到「它最后得出了什么」。summary 与交给主agent 的
            # spawn_agents 汇总同源（result.text），保证「界面看到的」=「主agent 收到的」。
            summary = (result.text or "").strip()
            if role == "sub":
                # 模型只吐思考/空收尾时，用它在 report_to_user 里写下的结论兜底
                saved = self._sub_summaries.get(str(task.id), "")
                if (not summary or getattr(result, "fallback", False)) and saved:
                    summary = saved
                    result.text = saved
                summary = summary[:4000]
            await repo.update_task(str(task.id), status="done", finished_at=_now(),
                                  result_json={"text": (summary or result.text)[:4000]})
            await self._emit(run_ctx, "task_done",
                             {"task_id": str(task.id), "role": role, "status": "done",
                              **({"summary": summary} if role == "sub" and summary else {})})
            return result
        finally:
            watcher.cancel()
            # 子agent 的临时结论已在上面的成功分支消费掉：清掉，避免长驻进程里越积越多
            if role == "sub":
                self._sub_summaries.pop(str(task.id), None)
            # 主agent 退出（正常/取消）时，后台 spawn 批次若还没回收：一并取消。
            # spawn_agents 现在是派发即返回，子任务独立于工具调用存活——不取消的话，
            # run 结束后它们会变孤儿继续烧 LLM/占连接（单任务停止位只对没退出的 run 有效）。
            if role == "main":
                for batch in list(getattr(tctx, "spawn_batches", [])):
                    for sub_task in list(getattr(batch, "tasks", [])):
                        if not sub_task.done():
                            sub_task.cancel()
            # 兜底：本协程被父级（整轮停止）取消时，`await agent_task` 处抛 CancelledError
            # 并不会连带取消 agent_task——它是独立 task，不主动 cancel 会让子agent 变孤儿
            # 继续在后台跑（用户点整轮停止，看到的却是子agent 还在动）。这里统一补一刀。
            if not agent_task.done():
                agent_task.cancel()

    # ---------- 记录 ----------
    async def update_plan(self, run_ctx: RunContext, items: list[dict], summary: str = "",
                          agent_key: str = "main") -> list[dict]:
        """计划机制：待办清单落库（runs.plan_json）并广播 plan_update，前端实时渲染勾选。

        单一写者（助手）整体覆盖，不带增量合并——避免多项并发改同一条时状态互相覆盖。
        """
        from game_agent.agents.tools.plan import plan_progress
        await self.svcs.repo.update_run(run_ctx.run_id, plan_json=items or [])
        await self._emit(run_ctx, "plan_update", {
            "agent_key": agent_key, "items": items or [],
            "summary": summary, "progress": plan_progress(items or []),
        })
        return items or []

    async def set_final_summary(self, run_id: str, text: str) -> None:
        self._summaries[run_id] = text

    async def set_sub_summary(self, task_id: str, text: str) -> None:
        """子agent 的收尾结论单独存：报告给主agent 的那段，不能覆盖用户的最终汇报。"""
        body = (text or "").strip()
        if body:
            self._sub_summaries[str(task_id)] = body

    def _prefer_summary(self, result: Any, run_id: str) -> str:
        """收尾用哪段文字：模型原话优先；模型只有运行时兜底时，优先用它 report_to_user 的汇报。

        为什么要这条：`report_to_user` 的汇报是模型亲口写给用户的总结，存在 _summaries；
        而 runtime 的空收尾兜底只是「按执行轨迹汇总」的次选。以前 execute_run 无条件用
        result.text，模型汇报过、最后又空收尾时，好端端的汇报被一句「端点可能吞了回复」顶掉。
        """
        text = str(getattr(result, "text", "") or "")
        saved = str(self._summaries.get(run_id) or "").strip()
        if getattr(result, "fallback", False) and saved:
            return saved
        return text or saved

    async def ask_user(self, run_ctx: RunContext, question: str, options: list[dict],
                       allow_text: bool = True, agent_key: str = "main",
                       progress=None) -> dict:
        """助手拿不准就问：落库一条 run_questions + 会话里发一条问题卡片 + 广播事件。

        **不再结束本轮**：问题落库、卡片广播后，这里挂起等用户回答（同一 run、同一轨迹、
        同一份待办清单），answering 端点 set_result 唤醒后返回 ``{"answer": ...}``，agent
        循环带着答案继续往下做。只有 future 拿不到（服务重启 / 多进程）时才回退到旧逻辑
        （返回的 record 没有 answer，调用方抛 AskUserStop 让 run 落 blocked、起新 run 续跑）。

        ``progress``：可选回调，等待期间周期调用以推进工具层「活进展」计数，避免被
        execute_tool_with_timeout 的 900s 无进展兜底当成卡死掐掉。
        """
        async with self._sf() as s:
            q = RunQuestion(
                team_id=_u(run_ctx.team_id), session_id=_u(run_ctx.session_id),
                run_id=_u(run_ctx.run_id), agent_key=agent_key,
                question=question, options_json=options or [], allow_text=allow_text,
                status="pending",
            )
            s.add(q)
            await s.flush()
            msg = Message(session_id=_u(run_ctx.session_id), run_id=_u(run_ctx.run_id),
                          author_type="agent", agent_key=agent_key,
                          content=question,
                          meta_json={"kind": "question", "question_id": str(q.id),
                                     "options": options or [], "allow_text": allow_text})
            s.add(msg)
            await s.commit()
            await s.refresh(q)
            await s.refresh(msg)
            record = {"id": str(q.id), "question": question, "options": options or [],
                      "allow_text": allow_text, "session_id": str(run_ctx.session_id),
                      "run_id": str(run_ctx.run_id), "message_id": str(msg.id)}
        # 同一 run 内的暂停：登记一个 future，让回答端点能唤醒本协程（而不是另起 run）。
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_answers[str(record["id"])] = fut
        await self._emit(run_ctx, "question_asked", record)
        await self._emit(run_ctx, "message_new", {
            "session_id": str(run_ctx.session_id), "message_id": str(msg.id),
            "author_type": "agent", "kind": "question",
        })
        # 挂起等待（同一 run 继续）。等待期间做的事：
        #  · 每 30s 唤一次 progress() → 工具层硬超时看门狗看到「有进展」，不会 900s 掐断；
        #  · 每 60s 落一条持久化 status 事件 → 30 分钟静默看门狗不会把暂停的 run 判死；
        #  · 用户点停止 → run_agent 的取消看门狗 cancel 本任务 → CancelledError 正常退场。
        waited = 0
        try:
            while True:
                try:
                    answer = await asyncio.wait_for(asyncio.shield(fut), timeout=30)
                    record["answer"] = answer
                    # 落一条已回答事件：前端据此把「等你回答」状态撤掉（同一 run 继续跑）
                    try:
                        await self._emit(run_ctx, "question_answered", {
                            "id": str(record["id"]), "answer": answer,
                            "session_id": str(run_ctx.session_id),
                            "run_id": str(run_ctx.run_id)})
                    except Exception:  # noqa: BLE001 - 事件失败不影响继续执行
                        pass
                    return record
                except asyncio.TimeoutError:
                    waited += 30
                    if callable(progress):
                        try:
                            progress()
                        except Exception:  # noqa: BLE001 - 推进计数失败不影响等待
                            pass
                    if waited % 60 == 0:
                        try:
                            await self._emit(run_ctx, "status", {
                                "status": "running",
                                "note": f"已暂停，等你回答（已等待 {waited // 60} 分钟）"})
                        except Exception:  # noqa: BLE001 - 心跳失败不影响等待
                            pass
        finally:
            # 被取消 / 已作答：把登记清掉，避免 future 泄漏
            self._pending_answers.pop(str(record["id"]), None)

    def resolve_question(self, question_id: str, answer_text: str) -> bool:
        """作答端点调用：唤醒正在 ask_user 上挂起的 run。返回是否命中了在途暂停。

        命中 = 同一进程里确有一条 run 正停在此问题等回答；没命中（服务重启过 / 多进程
        server 模式 / 问题已过期）由调用方回退到「起新 run 续跑」的老路径。
        """
        fut = self._pending_answers.pop(question_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(answer_text)
        return True

    def take_question(self, question_id: str) -> "asyncio.Future | None":
        """原子取走某个问题的在途等待 future（不设置结果）。命中返回 fut，否则 None。

        作答端点用它做「检查并消费」一步到位，避免 has_pending_question → 提交 → resolve
        之间被另一个请求抢走导致误判为回退路径、多起一条 run。
        """
        fut = self._pending_answers.pop(question_id, None)
        if fut is None or fut.done():
            return None
        return fut

    def has_pending_question(self, question_id: str) -> bool:
        """该问题当前是否有 run 在本进程内暂停等它（不消费 future）。"""
        fut = self._pending_answers.get(question_id)
        return fut is not None and not fut.done()

    async def _plan_items(self, run_id: str) -> list[dict]:
        """读本轮待办清单（模型自己用 update_plan 写的）。

        收尾纠正不再看「系统预判的意图」，改看**模型自己的承诺**：清单里还有未完成项，
        就说明它自己也认为活儿没干完。这比系统替它猜「该不该动手」可靠。
        """
        try:
            run = await self.svcs.repo.get_run(_u(run_id))
            return [dict(i or {}) for i in (getattr(run, "plan_json", None) or [])]
        except Exception:  # noqa: BLE001 - 读不到就当没清单，不纠错
            log.exception("read plan failed", extra={"run_id": run_id})
            return []

    # 汇报里「自己说要动手」的说法，分两类，都只认**第一人称的具体动作**：
    #   · 已完成类：「已修改/改好了/已生成」；
    #   · 将要做类：「我准备改/我来改/接下来我改」。
    # 命中却零落地 = 模型自己说要改却没改（自相矛盾），收尾纠正一次。
    # 刻意不收「建议怎么改 / 需要改这几处」这类**给出建议**的措辞——那是纯方案汇报的正常
    # 结尾，把它当成承诺就等于又在逼纯问答任务去改代码。
    _CLAIM_WORDS = ("已修改", "已改完", "改好了", "已更新文件", "已写入", "已生成",
                    "已创建", "已新增", "已删除", "已修复", "已重构", "已落盘",
                    "改动如下", "修改文件", "已实现",
                    # 第一人称「打算动手」：说了要改却没改，也算自相矛盾
                    "我准备改", "我准备动手", "我来改", "我现在改", "我现在就改",
                    "接下来我改", "接下来我来", "我将修改", "我将改")

    @classmethod
    def _claims_action(cls, res, summary: str) -> bool:
        """模型是否自称已经落地过改动（用于识别「说了改了、其实没改」）。"""
        text = f"{getattr(res, 'text', '') or ''}\n{summary or ''}"
        return any(w in text for w in cls._CLAIM_WORDS)

    async def _close_out_plan(self, run_ctx: RunContext) -> tuple[list[dict], list[dict]]:
        """run 成功收尾：把清单里没打勾的项标上真实收尾状态，返回 (整份清单, 未完成项)。

        以前这里会把未完成项一律勾成 completed（note=任务结束自动勾选）。实测后果是
        模型交回「128 条只写了 32 条、其余见下一步」的半成品汇报，界面却显示
        「待办清单 7/7 已完成 + 执行完毕」——用户看到的那句「做完了」是系统编的。
        现在只补一句诚实的 note，勾留给模型自己打（runtime 会打回要求它继续做）。
        """
        try:
            run = await self.svcs.repo.get_run(run_ctx.run_id)
            items = [dict(i or {}) for i in (getattr(run, "plan_json", None) or [])]
            open_items = [i for i in items if str(i.get("status")) != "completed"]
            if not open_items:
                return items, []
            for it in open_items:
                base = (it.get("note") or "").strip()
                tail = "这次执行结束时仍未完成"
                it["note"] = f"{base}；{tail}" if base and tail not in base else (base or tail)
            await self.update_plan(run_ctx, items,
                                   summary=f"这次执行结束：{len(open_items)} 项未完成")
            return items, open_items
        except Exception:  # noqa: BLE001 - 收尾失败不影响主流程
            log.exception("close out plan failed", extra={"run_id": run_ctx.run_id})
            return [], []

    # 自动续跑上限见 config.AUTO_CONTINUE_MAX（GA_AUTO_CONTINUE_MAX，0=关闭）：
    # 每轮 = 几十次 LLM 往返（实测单轮 50–90 次调用），
    # 真做不了的活儿不能靠无限重试烧额度。
    _AUTO_BRIEF_PREFIX = "【系统自动续跑：待办清单还没做完】"

    async def _auto_continue_depth(self, run) -> int:  # type: ignore[no-untyped-def]
        """这条需求已经自动续跑过几轮（沿 trigger 消息往回走链）。"""
        depth = 0
        try:
            async with self._sf() as s:
                cur_id = getattr(run, "trigger_msg_id", None)
                while cur_id and depth <= AUTO_CONTINUE_MAX + 2:
                    msg = await s.get(Message, _u(cur_id))
                    src = (msg.meta_json or {}).get("auto_continue_from") if msg else None
                    if not src:
                        break
                    depth += 1
                    nxt = await s.get(Run, _u(src))
                    cur_id = getattr(nxt, "trigger_msg_id", None) if nxt else None
        except Exception:  # noqa: BLE001 - 数不动就当作到底了
            log.exception("read auto-continue chain failed")
            return AUTO_CONTINUE_MAX
        return depth

    async def _auto_continue(self, run_ctx: RunContext, run, plan_items: list[dict],
                             open_items: list[dict], final_summary: str,
                             workspace: str) -> Optional[str]:
        """成功收尾但清单还有未完成项 → 自动排队一条「接着做」的新 run。

        为什么系统代劳：上一轮 128 条只写了 32 条，模型把剩下的写成「四、下一步
        （按这个顺序直接续）」交回来，run 却是 succeeded——用户不自己发现并点「继续」，
        活儿就永远停在那儿。这正是要消灭的「只做到一半」，所以由收尾流程直接续跑，
        而不是等人来催。未完成项、上次汇报的「下一步」原文都会塞进续跑说明。
        """
        if not open_items or getattr(run, "run_type", "") != "modify":
            return None
        depth = await self._auto_continue_depth(run)
        if depth >= AUTO_CONTINUE_MAX:
            log.warning("auto-continue budget used up", extra={"run_id": str(run.id)})
            return None
        try:
            from game_agent.orchestration import snapshots
            changed: list[str] = []
            try:
                changed = [f["path"] for f in snapshots.list_changes(
                    str(run.id), workspace, with_diff=False)["files"]]
            except Exception:  # noqa: BLE001 - 没快照照样续跑
                changed = []
            if not changed:
                cr = run.change_report_json or {}
                changed = [f.get("file") for f in (cr.get("files") or []) if f.get("file")]
            marks = {"in_progress": "进行中", "pending": "未开始"}
            # 整份清单都列出来（含已打勾的）：只给未完成项，模型看不到「哪些已经做了」，
            # 实测会回头把已写好的 32 条重新生成一遍。
            plan_lines = "\n".join(
                f"{'✓' if str(i.get('status')) == 'completed' else '•'} {i.get('content') or ''}"
                + (f"（{marks.get(str(i.get('status')), str(i.get('status')))}"
                   f"{('：' + str(i.get('note'))) if i.get('note') else ''}）"
                   if str(i.get("status")) != "completed" else "")
                for i in plan_items[:12])
            async with self._sf() as s:
                pending_q = (await s.execute(select(RunQuestion.id).where(
                    RunQuestion.run_id == _u(run.id),
                    RunQuestion.status == "pending").limit(1))).first()
                if pending_q:
                    return None  # 它在等用户拍板，不是等自己续跑
                parts = [
                    self._AUTO_BRIEF_PREFIX,
                    f"上一次执行结束时待办清单还有 {len(open_items)} 项没打勾，"
                    "系统已自动接着往下做。这次的目标就是把这些做完——全部 completed 才算交付。",
                    "上次计划进度：\n" + plan_lines,
                ]
                if (final_summary or "").strip():
                    parts.append("上一次执行的汇报（结尾若写「下一步」，那是派给你做的，"
                                 "不是交给用户的建议）：\n" + (final_summary or "").strip()[-1500:])
                if changed:
                    parts.append("上一次执行已改动的文件（先读一遍确认现状，不要重写）：\n"
                                 + "\n".join(f"- {p}" for p in changed[:20]))
                made: list[str] = []
                try:
                    for a in await self.svcs.repo.list_artifacts(str(run.id)):
                        made.append(f"- {a.asset_kind}/{a.slug}: {a.rel_path}"
                                    + ("（占位）" if a.is_placeholder else ""))
                except Exception:  # noqa: BLE001 - 查不到素材不影响续跑
                    made = []
                if made:
                    parts.append(f"上一次执行已产出的素材（共 {len(made)} 个，文件已在工作区，"
                                 "**不要重画**，缺哪张补哪张）：\n" + "\n".join(made[:40]))
                parts.append(
                    "要求：**从中断处接着做**，已完成的不重做、已产出的素材不重画；"
                    "量大的一批写成参数化、可断点续跑的脚本，用 start_service 后台跑 + "
                    "service_logs 轮询到结束，不要靠一条条工具调用数着做"
                    "（**配音/出图例外**：素材类批量一律 generate_audio / generate_image 的 items 一次生成，"
                    "不许写脚本或自己起语音/生图服务）；"
                    "每做完一项立刻 update_plan 把该项置 completed；"
                    "确实做不了的保持未完成并在 note 写清卡点。"
                    "不要重新提问、不要重新勘察全局、不要交「下一步怎么做」的报告。")
                brief = "\n\n".join(parts)
                msg = Message(session_id=_u(run.session_id), run_id=None, author_type="user",
                              author_id=_u(run.triggered_by), content="继续完成任务目标",
                              meta_json={"auto_continue_from": str(run.id),
                                         "auto_continue_depth": depth + 1,
                                         "open_count": len(open_items),
                                         "brief": brief})
                s.add(msg)
                await s.flush()
                await s.refresh(msg)
                # 继承上一轮的模型/推理等级/能力开关：否则用户执行中切了模型，
                # 这一轮又因清单没做完自动续跑，续跑的 run 会悄悄退回角色默认模型。
                # 必须重读数据库：运行中切模型是另一个会话写回 options_json 的，
                # 本 execute_run 手里的 run 快照不会自动刷新。
                fresh = await s.get(Run, _u(run.id))
                prev_opts = dict(getattr(fresh or run, "options_json", None) or {})
                carry = {k: prev_opts[k] for k in
                         ("model_profile_key", "reasoning_effort", "caps") if prev_opts.get(k)}
                new_run = Run(team_id=_u(run.team_id), project_id=_u(run.project_id),
                              session_id=_u(run.session_id), triggered_by=_u(run.triggered_by),
                              trigger_msg_id=msg.id, run_type=run.run_type,
                              options_json=carry, status="queued")
                s.add(new_run)
                await s.commit()
                await s.refresh(new_run)
                new_id = str(new_run.id)
            await self.svcs.queue.enqueue(new_id, priority=0)
            await self._emit(run_ctx, "message_new", {
                "session_id": str(run_ctx.session_id), "message_id": str(msg.id)})
            await self._emit(run_ctx, "status", {"status": "running",
                                                "note": (f"清单还有 {len(open_items)} 项未完成，"
                                                         f"已自动排队续跑（第 {depth + 1}/"
                                                         f"{AUTO_CONTINUE_MAX} 次）")})
            log.info("auto-continue enqueued", extra={"run_id": str(run.id),
                                                      "next_run": new_id, "depth": depth + 1})
            return new_id
        except Exception:  # noqa: BLE001 - 续跑失败不影响本次收尾
            log.exception("auto continue failed", extra={"run_id": str(run.id)})
            return None

    # ---------- 辅助 ----------
    _AUDIO_WORDS = ("配音", "声优", "语音", "台词", "念白", "念一遍", "音色", "音效",
                    "bgm", "背景音乐", "朗读", "tts")
    _IMAGE_WORDS = ("出图", "立绘", "头像", "图标", "卡面", "插画", "场景图", "画一张",
                    "生成一张", "生成图片", "抠图", "重绘", "海报", "美术资源")

    @classmethod
    def _inspect_hint(cls) -> str:
        """用户**亲手点「仅分析」**时的前置提示。

        只在 run_type=analyze（前端写入的显式选择）时使用——这不是系统猜测，而是用户按的按钮，
        所以可以明确「本轮只查不改」。系统自动盘查判出的 survey **绝不走这里**（见 intent.hint_for）：
        那种情况下只给软提示，模型想改就能改。
        """
        return (
            "## 本轮由用户指定为「仅分析」\n"
            "用户在前端明确选了「仅分析」，本轮请只给**结论**："
            "问题在哪、为什么、建议怎么改。\n"
            "本轮：读代码/日志/配置从容定位根因，需要验证（编译、跑命令、看运行日志）就照常执行；"
            "最后给出「结论 + 证据（文件:行号/日志原文）+ 建议改法」，"
            "本轮**不要**修改产品文件、不要起服务改配置。\n"
            "如果用户接着让你修，下一轮再动手改。"
        )

    @classmethod
    def _intent_hint(cls, text: str) -> str:
        """按用户这句话预判素材意图，生成前置硬提示（只放行"该生成就生成"）。

        为什么在代码里做而不是只靠提示词：模型对"重新生成 cv"这类短句会本能地去翻仓库找脚本，
        实测连翻 20+ 轮工具最后没产出任何音频；把意图和工具参数前置成结构化指令后能一轮出活。
        """
        import re as _re
        t = (text or "").strip()
        if not t:
            return ""
        low = t.lower()
        is_audio = any(w in low for w in cls._AUDIO_WORDS) or bool(
            _re.search(r"(?<![a-z])cv(?![a-z])", low))
        is_image = any(w in low for w in cls._IMAGE_WORDS)
        if not (is_audio or is_image):
            return ""
        if is_audio:
            # 参数口径（text/voice/design_desc/out/items…）由 generate_audio 的工具定义给，
            # 这里不再抄一遍：这段前置提示会拼进指令、随历史一直带着，抄一次就多背一次。
            # 留着的是实测会犯的那几件事：绕去起服务/跑脚本，和翻一堆角色档案才动手。
            return (
                "## 意图判定（系统预判，优先级最高）\n"
                "这是【音频/配音】需求（cv / 配音 / 语音都指配音）："
                "第一步就直接调用 **generate_audio**。\n"
                "严禁自己 start_service 起 IndexTTS2、curl 语音服务端口、"
                "跑 cv_gen.py / gen-cv-indextts.py / cv-presets.py 或写脚本调语音接口"
                "——语音服务由工具内部拉起，不用你管；用户要换声线/新音色也还是 "
                "generate_audio（design_desc + preset_name，见工具说明）。\n"
                "角色人设/台词最多读 1 个文件，拿到就生成；用户说「先看看再决定」"
                "就出到 assets/audio/voice/ 试听，不要覆盖线上音频。"
                "成功后汇报真实路径，失败如实说原因。"
            )
        return (
            "## 意图判定（系统预判，优先级最高）\n"
            "这是【图片】需求：第一步就直接调用 **generate_image** 生成真实图片文件"
            "（参数以工具定义为准，reference 传参考图相对路径）。"
            "禁止先翻仓库找脚本、禁止只回文字。\n"
            "成功后汇报真实路径，失败如实说原因。"
        )

    # 多步需求的动词特征：命中即认为"不是一句话能答完"，要求先列计划
    # 决策空缺信号：用户把决定权丢回来 / 明确说拿不准 → 必须先 ask_user
    _ASK_WORDS = (
        "你看着办", "你决定", "你来定", "随便你", "拿不准", "不确定", "你自己判断",
        "你判断", "看着来", "你选", "都行", "哪个都行", "帮我定", "你定吧",
        "我不知道", "不太确定", "你说呢", "要不要你定", "拍板",
    )

    _MULTI_WORDS = (
        "实现", "新增", "加一个", "加个", "加上", "改造", "重构", "优化", "修复",
        "接入", "迁移", "支持", "批量", "统计", "分几步", "梳理", "设计", "方案",
        "评估", "联调", "上线", "扩展", "拆分", "功能点", "机制", "流程", "逐一",
    )

    @classmethod
    def _plan_hint(cls, text: str) -> str:
        """多步需求：强制「第一步先列待办清单」，否则模型会一头扎进 grep 里翻代码。

        实测（2026-10-27 巡游）：用户说"梳理卡牌详情弹窗要做哪些功能点，分几步实现"，
        模型连做 12 轮 grep/list_dir（还全撞在 release/engine/*.dll 上），一次 update_plan 都没调。
        光在 system prompt 里写「复杂就列计划」不够，得在运行时把这条变成前置硬指令。
        """
        t = (text or "").strip()
        if len(t) < 8:
            return ""
        hits = [w for w in cls._MULTI_WORDS if w in t]
        if not hits:
            return ""
        return (
            "## 本次任务是【多步任务】（系统预判，优先级最高）\n"
            "第一步：调用 **update_plan** 列出 3–6 项待办（每项可独立验证，第一项 in_progress，"
            "其余 pending），例如「1 梳理现状 / 2 出方案 / 3 实现 / 4 验收 / 5 汇报」。\n"
            "第二步才允许开始定位与实现；每完成一项立刻 update_plan 把该项置 completed（整体覆盖整份清单）。\n"
            "禁止：不列计划就直接 grep/list_dir 翻仓库；翻代码最多 2–3 次轻量定位，"
            "定位是为了动手改，不是自己考古。"
        )

    @classmethod
    def _ask_hint(cls, requirement_text: str = "", full: str = "") -> str:
        """决策空缺：用户把决定权丢回来时，强制先 ask_user 让用户拍板，而不是自己替他定。

        实测（2026-10-27 巡游）：用户说「rarity 有"--"有留空，这类卡算不算匹配我拿不准，你看着办吧」，
        模型直接自己选了个口径就往下写方案——正是用户明确要求"不要自己做决定"的场景。
        光写 system prompt 不够（工具存在≠会用），所以在运行时把它变成前置硬指令。
        """
        t = (requirement_text or "").strip()
        if len(t) < 8:
            return ""
        hits = [w for w in cls._ASK_WORDS if w in t]
        if not hits:
            return ""
        return (
            "## 本次任务存在【决策空缺】（系统预判，优先级最高）\n"
            f"用户原话里出现了：{'、'.join(hits)} —— 说明有事情需要**用户拍板**，你不能自己定。\n"
            "做法：先用 **ask_user** 把待定事项问清楚（一次 1–3 个关键问题；每题 2–5 个互斥选项，"
            "每个选项写清后果，allow_text=true），然后停下来等用户回答；"
            "用户作答后你会带着答案被重新唤起，再继续分析/动手。\n"
            "禁止：自己选一个口径就当已确认、在正文里写「我按 X 处理（如不同意请告知）」蒙混过去。"
        )

    async def _audio_capability_line(self, team_id: str) -> str:
        """实时探测音频档位，生成能力清单里的那行。

        为什么必须探测而不是写死：以前这里硬编码「占位模式 ⚠️」，即使团队配好了
        IndexTTS2，模型看到的仍是"只有占位"，于是反复翻仓库找脚本、
        最后把静音 wav 当成品上报。能力清单必须反映真实档位。
        """
        try:
            prof = await self.resolve_media_profile(team_id, "audio")
        except Exception:  # noqa: BLE001 - 探测失败按未配置处理
            prof = {}
        prov = (prof.get("provider") or "").lower()
        params = prof.get("params") or {}
        preset = params.get("preset") or params.get("voice") or ""
        if prov in ("indextts2", "indextts", "local_tts"):
            tail = f"，音色预设「{preset}」" if preset else ""
            return ("- 已配置本地 IndexTTS2 零样本克隆{tail} → **可出真实人声**；"
                    "配音一律直接调 generate_audio，产物落 assets/audio/voice/。".format(tail=tail))
        if prov in ("openai_tts",):
            return "- 已配置在线 TTS → **可出真实人声**；配音一律直接调 generate_audio。"
        return ("- 未配置音频档位（设置 → 模型配置，kind=audio，再绑到助手的 "
                "audio_profile）→ **占位模式 ⚠️：现在调 generate_audio 只会得到静音 wav。**"
                "正确做法：第一步先告知用户「未配置真实音频供应商，需要先在设置里配置配音模型」，"
                "并停在这一步等用户决定——不要为了交差生成一堆静音占位文件再上报；"
                "用户明确说「先出占位也行」才继续生成，且产物必须如实标注占位。")

    def _build_manifest(self, project, run_ctx: RunContext, python_bin: str,
                        audio_line: str = "") -> str:
        if COMFYUI_BASE_URL:
            img_line = "- 已配置 ComfyUI 本地生图 → 可生成真实图片（端点不可用时自动回退占位并标注）"
        else:
            img_line = ("- 未配置图像供应商 → 占位模式 ⚠️：生成的是带标记的占位图，不是成品。"
                        "先告知用户并等确认（「先出占位也行」才继续），不要闷头出占位图充数")
        audio_line = audio_line or "- 占位模式 ⚠️ 只会生成静音占位 wav，不是成品"
        return (
            "## 你当前可用的能力（系统实时探测，以此为准）\n"
            "### 代码执行\n- 原生模式（fs + shell）: ✅ 当前可用（主线）\n"
            "### 联网\n- web_search(关键词) 搜索网页 / web_fetch(url) 抓取网页正文："
            "需要外部资料（文档/报错/方案对比）时用，不要凭记忆编造网上事实\n"
            "（操作系统/shell/解释器/命令写法只在此处一份：见系统提示最前面的「运行环境」段，"
            "同一条命令失败两次就别再重试，换思路或如实上报）\n"
            f"### 图片生成\n{img_line}\n"
            f"### 音频\n{audio_line}\n"
            "### 项目环境\n"
            f"- 项目: {project.name}\n"
            f"- 构建: {project.commands_json.get('build', 'python -m compileall -q .')} · "
            f"测试: {project.commands_json.get('test', 'python -m pytest -q')}\n"
            "（run_command 里直接写 python 即可，平台会自动换成项目解释器，不要自己拼解释器绝对路径）\n"
            "### 自测与验收\n"
            "- **默认不起服务**：改完跑 run_command 的编译/构建/单测，过了就汇报；起服务验收是重手段，"
            "只在「用户明确要求看效果」或「同一问题多轮修改仍没解决」时才用。\n"
            "- 真要起服务: start_service（后台起、等端口就绪）→ service_logs 看日志 → "
            "screenshot_page 截图（截图会直接进入你的视野，你必须真的看图核对效果）→ stop_service 收掉\n"
            "- **绝对不要 `taskkill /IM chrome.exe` / `killall chrome` / `pkill chrome` 清场**："
            "那是按进程名全杀，会把用户自己正开着的浏览器（含本界面）一起关掉。要截图就用 "
            "screenshot_page（它自己起独立 profile 的无头实例，跑完自动清理）；确实要手动起 CDP 时，"
            "用独立 `--user-data-dir` + `--remote-debugging-port`，收尾按端口/PID 精确杀。\n"
            "- 看已有图片（自己/脚本截的图、生成的图、用户给的参考图）: view_image(path)，"
            "图片直接进视野；不要读 base64、不要用 screenshot_page 开 file://（本地文件不走浏览器）\n"
            "- 快速校验: run_command 跑 build/test 命令（可用命令头见上方白名单；"
            "缺的命令让用户加进 .trace/allow_commands.txt）\n"
            "判断规则：你要图/音频就 generate_image / generate_audio 直接生成；"
            "改完代码默认只做静态验证（run_command 跑编译/构建/单测），别自己拉服务截图；"
            "用户明确要看效果、或同一问题多轮修改仍没解决时才起服务+截图核对；起服务换了几种方式仍起不来就停，"
            "把原因、试过的方法、风险点写进汇报；标 ❌/⚠️ 的能力要如实上报。\n"
            "### 大批量任务的正确做法（决定你会不会「只做到一半」）\n"
            "没有「这次先做多少条」的说法：清单还有没做完的项时，交卷 = 半成品。"
            "唯一的硬约束是 run_command 单条命令约 300 秒超时，所以：\n"
            "- 十几条以内：直接一条条做完。\n"
            "- 几十上百条（全量配音/出图/批量改文件）：**写成脚本** —— 参数化、"
            "跳过已完成项（断点续跑）、进度写进文件，然后 start_service 后台跑，"
            "用 service_logs 轮询到全部结束再收尾。脚本在后台自己循环，"
            "几百条也在同一次执行里跑完。\n"
            "- 禁止的收尾方式：把没做完的写成「四、下一步（按这个顺序直接续）」交回来，"
            "或者为了让清单好看把没做的勾成已完成。没做完就如实留着，系统会接着往下推。"
        )

    async def _emit(self, run_ctx: RunContext, kind: str, payload: dict) -> None:  # type: ignore[no-untyped-def]
        # run_done 必须自带 session_id：事件通道是按「项目」扇出的，前端收到 run_done 后
        # 只有知道是哪条会话，才能让用户点提醒直接跳过去（任务完成提示音/桌面通知都靠这个）。
        # question_asked 的载荷本来就带 session_id，这里只补 run_done，
        # 不给每条轨迹事件都塞一份重复字段（事件是要落库的，能省则省）。
        if kind == "run_done" and "session_id" not in payload:
            payload = {**payload,
            "session_id": str(run_ctx.session_id) if run_ctx.session_id else None}
        # 外部 IM 通道提醒（微信/钉钉）：用户不在浏览器前时靠它知道任务跑完／被提问了。
        # 用 create_task 甩出去：推送要走公网 HTTP（最长 8s 超时），绝不能让某个
        # 通道慢/挂掉把 run 收尾拖住或改写状态；失败只落 channel_messages。
        if kind in ("run_done", "question_asked"):
            self._notify_channels(run_ctx, kind, payload)
        # 注意缩进：这一步必须在 if 外面。曾经它被多缩一级套进 run_done 分支里，
        # 结果除 run_done 外的所有事件（task_start / task_done / plan_update /
        # status / question_asked / notes_saved）都被静默丢掉——子agent 分组拿不到
        # task_start/task_done，界面就退化成一串「子Agent · 读网页」流水。
        try:
            await self.svcs.bus.emit(EventEnvelope(
                team_id=run_ctx.team_id, project_id=run_ctx.project_id,
                run_id=run_ctx.run_id, type=kind, payload=payload,
            ))
        except Exception:  # noqa: BLE001
            log.exception("emit event failed: %s", kind)

    def _notify_channels(self, run_ctx: RunContext, kind: str, payload: dict) -> None:
        """把 run 收尾/提问推送到已绑定的 IM 通道（后台 task，失败不影响 run）。

        为什么要单独兜一层 try：调用点在 `_emit` 里，而 `_emit` 被收尾路径的每一步使用，
        这里抛任何异常都会污染 run 状态判定。
        """
        try:
            import asyncio as _aio
            from game_agent.integrations import channels as _ch
            status = str(payload.get("status") or "")
            if kind == "question_asked":
                status = "blocked"
            elif status == "succeeded":
                status = "succeeded"
            else:
                status = status or "succeeded"
            _aio.get_running_loop().create_task(_ch.notify_run_finished(
                self._sf, run_id=str(run_ctx.run_id), team_id=run_ctx.team_id,
                status=status, session_id=str(run_ctx.session_id) if run_ctx.session_id else None))
        except Exception:  # noqa: BLE001 - 通道通知绝不影响 run
            log.exception("schedule channel notify failed")

    async def _close_open_tasks(self, run_ctx: RunContext, run_id: str) -> None:
        """整轮结束/取消时的兜底收口：把仍 running 的子 task 落 cancelled 并补 task_done。

        为什么需要：整轮停止靠 CancelledError 向下传播，但取消是并发竞争——某个子agent 可能
        刚被 task.cancel() 还没有走到 except 分支，主agent 的 run 收尾就已把 run_done 发出去了；
        该子 task 之后虽会被抹平，但前端没收到 task_done 就会把它一直画成「运行中」。这里统一
        再扫一遍并补事件（幂等：已收口的 task 状态不是 running，不会被重复处理）。
        """
        try:
            tasks = await self.svcs.repo.list_tasks(run_id)
        except Exception:  # noqa: BLE001
            return
        for t in tasks:
            if t.status == "running":
                try:
                    await self.svcs.repo.update_task(str(t.id), status="cancelled",
                                                     finished_at=_now())
                    await self._emit(run_ctx, "task_done",
                                     {"task_id": str(t.id),
                                      "role": getattr(t, "agent_key", "sub") or "sub",
                                      "status": "cancelled"})
                except Exception:  # noqa: BLE001
                    log.exception("close open task failed", extra={"task_id": str(t.id)})

    async def _agent_profile_key(self, team_id: str, role: str) -> str | None:
        return (await self._agent_defaults(team_id, role))[0]

    async def _agent_defaults(self, team_id: str, role: str) -> tuple[str | None, str]:
        """角色默认：绑定档位 key + 默认推理强度（agents.params_json.reasoning_effort）。

        为什么推理强度要跟着角色默认走（2026-11-14）：输入框右下角切换模型时会顺手把
        「模型 + 推理等级」写回角色默认，新会话/没单独选过的会话才和刚才那次选择一致；
        不认这个默认的话，设置页改了等于没改，用户看到的仍是端点默认强度。
        """
        async with self._sf() as s:
            cfg = await get_agent_config(s, team_id, role)
        params = cfg.params_json or {}
        return cfg.model_profile_key, _norm_effort(params.get("reasoning_effort"))

    async def _resolve_profile_key(self, team_id: str, role: str,
                                   opts: dict, own_key: str | None = None) -> str | None:
        """决定这次 run_agent 用哪个模型档位（None = 用 .env 全局兜底）。

        优先级：输入框显式选择 > 本角色绑定 > （子agent）主agent 绑定 > 全局兜底。

        为什么子agent 必须继承主agent 的档位：sub 角色通常没有自己的绑定（老安装里甚至
        没有 sub 的 agents 行）。不继承就会 `_load_profile(None)` → {} → build_llm 掉回
        .env 全局端点；而全局订阅常常已失效（403 SUBSCRIPTION_NOT_FOUND）。实测 2026-11-11：
        一轮派出 6 个子agent 全部 403、主agent 却正常——根因正是主agent 走团队绑定档位
        （211.101.244.244），子agent 掉到了 .env 里过期的 gonode.si 老订阅。
        """
        key = opts.get("model_profile_key") or own_key
        if not key and role == "sub":
            try:
                key = await self._agent_profile_key(team_id, "main")
            except Exception:  # noqa: BLE001 - 取主档位失败只影响子agent 的档位选择
                key = None
        return key

    async def _load_profile(self, team_id: str, profile_key: str | None) -> dict:
        """解析角色绑定的 ModelProfile → Ctx.model_profile；未绑定返回 {}（用全局默认）。

        取值优先级：档位 inline > 档位所绑供应商 > 环境变量（环境变量在 factory 兜底）。
        供应商模式把 base_url/key 集中在 llm_providers，档位只选模型名；inline 仍可用，
        便于临时试新端点而不改动供应商。
        """
        if not profile_key:
            return {}
        async with self._sf() as s:
            p = await s.scalar(select(ModelProfile).where(
                ModelProfile.team_id == _u(team_id), ModelProfile.key == profile_key))
            if p is None:
                # 绑定的档位已被删除却仍指向它 → 会静默回退到环境变量而踩 403；
                # 显式告警，便于第一时间发现"删了档位没解绑角色"的问题。
                log.warning("角色绑定的模型档位 key=%r 不存在（可能已被删除且未解绑），"
                            "将回退到全局默认配置（环境变量），若环境变量也已过期会报 403",
                            profile_key)
                return {}
            params = p.params_json or {}
            base_url, api_key, user_agent, prov_label = p.base_url, None, None, None
            protocol = p.provider or "openai"
            if p.provider_id:
                pv = await s.get(LlmProvider, p.provider_id)
                if pv is None:
                    log.warning("档位 %r 绑定的供应商已被删除，改用档位自身的 base_url/key",
                                profile_key)
                elif not pv.enabled:
                    log.warning("档位 %r 绑定的供应商 %r 已停用，运行时回退到环境变量默认",
                                profile_key, pv.label)
                else:
                    base_url = base_url or pv.base_url
                    api_key = pv.api_key
                    user_agent, prov_label = pv.user_agent, pv.label
                    protocol = pv.protocol or protocol
                    # 供应商模型目录里的窗口/视觉按模型名校正 params：目录是「按模型」的
                    # 权威配置，优先于档位 params 里可能残留的旧值（用户改了目录就该生效）。
                    from game_agent.llm.context_window import merge_model_catalog_params
                    params = merge_model_catalog_params(params, pv.models_json, p.model)
            return {"model": p.model, "base_url": base_url,
                    # 配置页把 API Key 存在 api_key_ref 字段（lite 模式简化存储），params_json.api_key 优先
                    "api_key": params.get("api_key") or p.api_key_ref or api_key,
                    "provider": prov_label, "protocol": protocol,
                    "user_agent": user_agent, "params": params}

    async def resolve_media_profile(self, team_id: str, kind: str) -> dict:
        """解析媒体生成档位（kind: image|audio）：
        main 角色 params_json.image_profile / audio_profile → 对应 ModelProfile。
        返回 {provider, model, base_url, api_key, params}；未绑定返回 {}（走环境变量/占位）。"""
        role = "main"
        field = f"{kind}_profile"
        prof: Optional[dict] = None
        try:
            async with self._sf() as s:
                cfg = await get_agent_config(s, team_id, role)
                pkey = (cfg.params_json or {}).get(field)
                if pkey:
                    p = await s.scalar(select(ModelProfile).where(
                        ModelProfile.team_id == _u(team_id), ModelProfile.key == pkey,
                        ModelProfile.kind == kind))
                    if p is not None:
                        params = p.params_json or {}
                        prof = {"provider": (params.get("provider") or p.provider or "").lower(),
                                "model": p.model, "base_url": p.base_url,
                                "api_key": params.get("api_key") or p.api_key_ref,
                                "params": params}
        except Exception as e:  # noqa: BLE001 - 档位解析失败不影响生成主流程
            # 以前这里静默返回 {}，导致"音色明明配好了却一直出静音占位"根本查不出来
            # （真凶是 get_agent_config 的 str/UUID 不匹配，异常被吞）。至少留痕。
            log.warning("resolve_media_profile(%s/%s) 失败：%s: %s",
                        team_id, kind, type(e).__name__, e)
        if prof is not None:
            return prof
        # 未绑定/解析失败 → 音频回退环境默认档位（v2 语义：档位是「显式覆盖」，
        # 不绑也应能用本地默认语音服务）。2026-10-28 实测：角色合并后 main 行没绑
        # audio_profile，这里返回 {} 被当成"未配置"，整批配音 0 失败却全是静音占位。
        # 图像路径在 specialized 里本来就有 COMFYUI_BASE_URL 环境回退，维持不动。
        if kind == "audio":
            from game_agent.config import TTS_BASE_URL
            return {"provider": "indextts2", "model": "", "base_url": TTS_BASE_URL,
                    "api_key": "", "params": {}}
        return {}

    @staticmethod
    def _attach_note(images: list[str], img_paths: list[str], files: list[dict],
                     workspace, inherited: bool, team_id: str | None = None) -> str:
        """拼「随本条需求附带的图片/附件」提示块（图片像素 + 原图路径 + 附件清单）。"""
        if images:
            src = "沿用上一条需求附带的" if inherited else "用户随本条需求附上的"
            # 图片像素不保证每轮都在（超出请求体字节预算时最老的会被省略），
            # 所以必须把原图路径一并给出：模型想看细节有确定的路可走，不用猜。
            # 附图原图现在存在 DATA_DIR/chat_uploads（不在工作区），resolve_ref 换成绝对路径，
            # view_image 直接能开；老数据仍是 .trace/uploads 相对路径，同样能解析。
            from game_agent.core import chat_images
            resolved: list[str] = []
            for p in img_paths[:len(images)]:
                real = chat_images.resolve_ref(p, workspace, team_id)
                resolved.append(str(real) if real else str(p))
            where = ("；原图在这几个路径：" + "、".join(resolved)
                     + "（若本轮没带上像素，用 view_image 单看一张、view_images 多张合并成一张）"
                     ) if resolved else ""
            note = f"（{src} {len(images)} 张图片，已作为视觉输入提供{where}）"
        else:
            note = ""
        if files:
            # 附件清单注入：模型必须知道文件在工作区的哪个位置才能 parse_document
            lead = "沿用上一条需求附带的" if inherited else "用户随本条需求附上的"
            flines = []
            for f in files:
                missing = ""
                if workspace is not None:
                    try:
                        if not (workspace / str(f.get("path", ""))).exists():
                            missing = "，该文件已不存在"
                    except OSError:
                        pass
                flines.append(f"- {f.get('path')}（原文件名 {f.get('original_name') or f.get('name')}，{f.get('size', 0)} 字节{missing}）")
            file_note = (f"（{lead} {len(files)} 个文件，已存入工作区，"
                         "xlsx/docx/pdf 用 parse_document 解析，文本类直接 read_file：\n"
                         + "\n".join(flines) + "）")
            note = f"{note}\n{file_note}" if note else file_note
        return note

    async def _load_context(self, team_id: str, msg_id: Optional[str],
                            session_profile: dict,
                            project_id: Optional[str] = None,
                            workspace=None,
                            session_skills: list[str] | None = None,
                            override_req: Optional[str] = None,
                            override_images: Optional[list[str]] = None,
                            override_image_paths: Optional[list[str]] = None,
                            override_files: Optional[list[dict]] = None) -> tuple[str, list[str]]:
        """加载触发消息 + 会话历史（按会话模型窗口压缩），返回 (requirement, images)。

        会话历史包含此前用户/智能体的消息，压缩策略：从最新往前保留，
        超出预算的最早若干条交给 AI 总结成摘要注入（摘要失败退化为截断标注）。
        若提供 project_id，则把项目记忆（历史要点）一并注入；
        若提供 workspace，则前置注入实时扫描的项目代码结构，帮助模型延续既有结构与风格。
        session_skills 为会话显式挂载的技能名单（渐进式披露，None=跟随项目默认）。

        续跑（点「继续」）没有触发消息：``override_req`` 直接给出续跑说明，配合
        ``override_images/image_paths/files`` 当成本轮需求与附件。此时本函数只负责
        环境/结构/记忆前置块 + 需求 + 附件说明；对话历史由调用方用 ``_history_block``
        另行注入（避免与续跑说明里的计划/事实清单重复）。
        """
        memory_block = await self._memory_block(project_id) if project_id else ""
        structure_block = (await self._structure_block(workspace, session_skills)
                           if workspace is not None else "")
        # 环境事实（.trace/env.json）：上次用命令撞出来的机器现实，直接给它，别再试探
        env_block = envfacts.prompt_block(workspace) if workspace is not None else ""
        # 历史改动经验（.trace/notes/*.md）：注入标题索引，正文由 agent 按需 read_file
        notes_block = project_notes.index_block(workspace) if workspace is not None else ""
        if override_req is not None:
            imgs = [str(u) for u in (override_images or [])]
            note = self._attach_note(imgs, [str(p) for p in (override_image_paths or [])],
                                     [dict(f) for f in (override_files or [])],
                                     workspace, inherited=False, team_id=team_id)
            return (structure_block + env_block + notes_block + memory_block
                    + override_req + note), imgs
        if not msg_id:
            return "", []
        inherited = False
        async with self._sf() as s:
            m = await s.get(Message, _u(msg_id))
            if m is None:
                return "", []
            images = [str(u) for u in ((m.meta_json or {}).get("images") or [])]
            img_paths = [str(p) for p in ((m.meta_json or {}).get("image_paths") or [])]
            files = [dict(f) for f in ((m.meta_json or {}).get("files") or [])]
            # 续跑消息的正文只是一句「继续完成任务目标」，真正要交给模型的完整续跑说明
            # （原需求、图/附件、已改文件、计划进度、工具事实…）存在 meta["brief"] 里。
            # 这里在同一个会话里取出，供下面拼「本条用户需求」用——既让模型拿到全部上下文，
            # 又不把这一大坨贴进聊天界面。
            brief = str((m.meta_json or {}).get("brief") or "").strip()
            req = brief or m.content
            if not images and not files:
                # 「继续 / 修一下」这类消息本身没附图/附件，但上一条需求带了。
                # 不继承的话模型就"看不见"那张图/那个文件——实测用户发「继续」，
                # 模型思考里直接说「服务重启了两次，我丢失了图片内容」。
                # 只回溯最近几条用户消息，图片最多继承 2 张（base64 很占上下文）。
                tail_ids = (await s.execute(
                    select(Message.id).where(
                        Message.session_id == m.session_id,
                        Message.author_type == "user",
                        Message.created_at <= m.created_at,
                        Message.id != m.id,
                    ).order_by(Message.created_at.desc()).limit(8))).scalars().all()
                for rid in tail_ids:
                    meta = (await s.execute(
                        select(Message.meta_json).where(Message.id == rid))).scalar_one_or_none()
                    cand = [str(u) for u in ((meta or {}).get("images") or [])]
                    cand_files = [dict(f) for f in ((meta or {}).get("files") or [])]
                    if cand or cand_files:
                        images = cand[:2]
                        img_paths = [str(p) for p in ((meta or {}).get("image_paths") or [])][:2]
                        files = cand_files[:6]
                        inherited = True
                        break
            # 只按「id + 时间」排序，再按 id 取整行。
            # 原因（实测踩坑）：messages 行里带 base64 附图（meta_json 可达数 MB），
            # 直接 select(Message).order_by(created_at) 会让 SQLite 对巨型行做 filesort，
            # 内存和耗时一起爆 → 整个 run 在加载上下文时卡死。
            order_rows = (await s.execute(
                select(Message.id, Message.created_at).where(
                    Message.session_id == m.session_id,
                    Message.created_at <= m.created_at,
                    Message.id != m.id,
                ).order_by(Message.created_at))).all()
            ids = [r[0] for r in order_rows]
            rows: list[Message] = []
            if ids:
                got = (await s.execute(
                    select(Message).where(Message.id.in_(ids)))).scalars().all()
                by_id = {r.id: r for r in got}
                rows = [by_id[i] for i in ids if i in by_id]

        note = self._attach_note(images, img_paths, files, workspace, inherited, team_id)
        if not rows:
            return (structure_block + env_block + notes_block + memory_block
                    + req + note), images

        window = int(((session_profile.get("params") or {}).get("context_window"))
                     or DEFAULT_CONTEXT_WINDOW)
        # 历史只占预算的一小部分，其余留给本条需求、工具往返与回复
        budget = min(int(window * 0.25), SESSION_HISTORY_MAX_TOKENS)
        lines: list[str] = []
        for h in rows:
            who = "用户" if h.author_type == "user" else ROLE_LABELS.get(h.agent_key or "", h.agent_key or "系统")
            body = h.content
            h_imgs = (h.meta_json or {}).get("images") or []
            if h_imgs:
                body = f"{body}\n[附图×{len(h_imgs)}]"
            h_files = (h.meta_json or {}).get("files") or []
            if h_files:
                paths = ", ".join(str(f.get("path", "")) for f in h_files)
                body = f"{body}\n[附件×{len(h_files)}：{paths}]"
            lines.append(f"{who}: {body}")
        picked: list[str] = []
        used = 0
        cap_per_line = min(4000, budget)  # 单条历史截断上限，避免垃圾长文挤占全部预算
        for line in reversed(lines):
            t = estimate_tokens(line) + 4
            if used + t > budget:
                if not picked:
                    remain = min(cap_per_line, max(0, budget - used - 8))
                    line = line[:remain] + "…[单条超长已截断]"
                    picked.append(line)
                    used += remain + 8
                    continue
                break
            picked.append(line)
            used += t
        picked.reverse()
        dropped = len(lines) - len(picked)
        header = ""
        if dropped:
            # 摘要式压缩：装不下的最早对话交给 AI 总结成摘要再注入，而非直接丢弃
            header = await self._summarize_dropped(team_id, lines[:dropped],
                                                   dropped, len(lines))
        history = "\n".join(picked)
        body = (f"## 会话上下文（旧→新）\n{history}\n\n## 本条用户需求{note}\n{req}"
                if history else f"{req}{note}")
        return structure_block + env_block + notes_block + memory_block + body, images

    async def _summarize_dropped(self, team_id: str, dropped_lines: list[str],
                                 dropped: int, total: int) -> str:
        """摘要式上下文压缩：把因窗口预算装不下的最早若干条对话交给 AI 总结成摘要，
        替代原来「已省略 N 条」的截断标注，让早期需求/结论不丢失。
        LLM 失败 / mock 模式 / 条数过少时退化为截断标注。"""
        fallback = (f"[会话历史共 {total} 条，因模型窗口限制已省略最早 {dropped} 条]\n")
        if dropped < 4:
            return fallback
        try:
            transcript = "\n".join(dropped_lines)[:8000]
            prompt = (
                "以下是智能体开发会话中较早的一段对话，因模型上下文窗口装不下，"
                "需要压缩为摘要供后续对话参考。\n"
                "要求：\n"
                "1. 提炼其中仍然重要的信息：用户提出的需求与偏好、已达成的结论、"
                "重要决定、遗留问题；\n"
                "2. 忽略寒暄与过程性内容，用「- 」列表输出，总长不超过 300 字；\n"
                "3. 只输出摘要本身，不要任何解释和开场白。\n\n"
                f"【早期对话（旧→新）】\n{transcript}"
            )
            profile = await self._load_profile(
                team_id, await self._agent_profile_key(team_id, "main"))
            ctx = Ctx(
                role="main", run_type="analyze", workspace="",
                requirement=prompt, project_name="", commands={}, payload={},
                model=(profile.get("model") or ROLE_MODELS.get("main") or LLM_MODEL),
                images=[], model_profile=profile,
            )
            llm = build_llm("main", ctx)
            # 90s 而不是 20s：适配层自身带退避重试（1.5s/3s/…），外层掐太紧会在
            # 它 sleep 等下一次次尝试时把任务取消，日志里留下一串 CancelledError 堆栈
            resp = await asyncio.wait_for(
                llm.chat([{"role": "user", "content": prompt}], [], ctx), timeout=90)
            text = (resp.text or "").strip()
            if text:
                return (f"[会话历史共 {total} 条，最早 {dropped} 条因窗口限制已压缩为以下摘要]\n"
                        f"{text[:600]}\n")
        except asyncio.TimeoutError:
            log.warning("早期对话摘要压缩超过 90s 未完成，退化为截断标注")
        except Exception as e:  # noqa: BLE001 - 摘要失败退化为截断标注，不阻塞 run
            # 一行带原因即可：尝试次数与每失败原因在 llm.openai 的 WARNING 里已逐条落过
            log.warning("早期对话摘要压缩失败（%s），退化为截断标注", str(e)[:200])
        return fallback

    @staticmethod
    async def _skill_list(workspace) -> list[str]:
        from game_agent.core.skills import list_skills

        pool = (await list_skills(workspace))["skills"]
        defaults = [s for s in pool if s["scope"] == "project" or s["name"] in ("excel", "word", "pdf")]
        return [f"- {s['name']}: {s['description'] or s['title'] or '（无说明）'}" for s in defaults]

    async def _attached_skills_block(self, workspace, names: list[str]) -> str:
        """会话显式挂载技能的清单块（渐进式披露：名称+描述进上下文，正文由 load_skill 拉取）。"""
        from game_agent.core.skills import SKILL_SCOPE_LABELS, resolve_attached
        try:
            metas = await resolve_attached(names, workspace)
        except Exception:  # noqa: BLE001
            log.exception("resolve attached skills failed")
            return ""
        if not metas:
            return ""
        lines = ["## 可用技能（本会话已指定；用 load_skill 工具按需加载正文）"]
        lines += [f"- {m['name']}: {m['description'] or m['title'] or '（无说明）'}"
                  f"（来源：{SKILL_SCOPE_LABELS.get(m['scope'], m['scope'])}）"
                  for m in metas]
        return "\n".join(lines)

    async def _structure_block(self, workspace, session_skills: list[str] | None = None) -> str:
        """实时扫描工作区生成项目代码结构摘要（不落库，永远新鲜），
        注入编排上下文，让模型延续既有目录组织、技术栈与代码风格。

        缓存按「目录指纹」失效：顶层条目名 + 各子目录首层条目名 + 关键清单文件
        的 (mtime,size)。子目录内部变化时顶层 mtime 不一定变，所以不能只看根 mtime；
        同时扫描只列两级、代价很小，指纹比内容便宜一个数量级。
        """
        import json as _json
        from pathlib import Path

        ws = Path(workspace)

        def _fingerprint() -> str:
            """顶层 + 各子目录首层条目名 + 关键文件 (mtime,size)。"""
            try:
                top = [(p.name, p.is_dir()) for p in ws.iterdir()]
            except OSError:
                return ""
            parts = sorted(f"{n}:{1 if d else 0}" for n, d in top)
            try:
                for p in sorted(ws.iterdir(), key=lambda x: x.name.lower()):
                    if not p.is_dir() or p.name.startswith(".") or p.name in _NOISE_DIRS:
                        continue
                    subs = [c.name for c in sorted(p.iterdir(), key=lambda c: c.name.lower())
                            if not c.name.startswith(".") and c.name not in _NOISE_DIRS][:8]
                    parts.append(f"{p.name}/[{','.join(subs)}]")
            except OSError:
                pass
            for key in ("package.json", "requirements.txt", "pyproject.toml"):
                f = ws / key
                try:
                    st = f.stat()
                    parts.append(f"{key}:{st.st_mtime_ns}:{st.st_size}")
                except OSError:
                    parts.append(f"{key}:-")
            return "|".join(parts)

        def _scan() -> list[str]:
            lines: list[str] = []
            try:
                entries = sorted(ws.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                return lines
            shown = 0
            for p in entries:
                if p.name.startswith(".") or p.name in _NOISE_DIRS:
                    continue
                if shown >= 24:
                    lines.append("- …（更多顶层条目省略）")
                    break
                shown += 1
                if not p.is_dir():
                    lines.append(f"- {p.name}")
                    continue
                try:
                    subs = [c.name + ("/" if c.is_dir() else "")
                            for c in sorted(p.iterdir(), key=lambda c: c.name.lower())
                            if c.name not in _NOISE_DIRS and not c.name.startswith(".")][:8]
                    lines.append(f"- {p.name}/（如 {'、'.join(subs)}）" if subs else f"- {p.name}/")
                except OSError:
                    lines.append(f"- {p.name}/")
            stack: list[str] = []
            pkg = ws / "package.json"
            if pkg.is_file():
                try:
                    data = _json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
                    deps = sorted(set(list((data.get("dependencies") or {}).keys())
                                      + list((data.get("devDependencies") or {}).keys())))
                    stack.append(f"Web 前端（{data.get('name') or 'package.json'}；依赖 "
                                 f"{', '.join(deps[:8])}{'…' if len(deps) > 8 else ''}）")
                except Exception:  # noqa: BLE001
                    stack.append("Web 前端（package.json）")
            req = ws / "requirements.txt"
            if req.is_file():
                try:
                    deps = [ln.strip().split("==")[0].split(">=")[0]
                            for ln in req.read_text(encoding="utf-8", errors="replace").splitlines()
                            if ln.strip() and not ln.startswith("#")][:8]
                    stack.append(f"Python（requirements: {', '.join(deps)}）")
                except Exception:  # noqa: BLE001
                    stack.append("Python（requirements.txt）")
            if (ws / "pyproject.toml").is_file():
                stack.append("Python（pyproject.toml）")
            if not lines and not stack:
                return []
            out = ["## 项目代码结构（实时扫描，新代码请延续既有目录组织与风格）"]
            if stack:
                out.append(f"技术栈：{'；'.join(stack)}")
            out.extend(lines)
            return out

        # 纯结构块按指纹缓存；技能块（下方）可能因 session_skills 或磁盘技能变化而不同，
        # 每次都即时追加，不混进缓存值，避免「技能清空后仍返回旧技能」这类污染。
        structure = ""
        if ws.is_dir():
            try:
                fp = await asyncio.to_thread(_fingerprint)
            except Exception:  # noqa: BLE001
                fp = ""
            cached = self._structure_cache.get(fp) if fp else None
            if cached is not None:
                structure = cached
            else:
                try:
                    lines = await asyncio.to_thread(_scan)
                except Exception:  # noqa: BLE001 - 结构注入失败不阻塞 run
                    log.exception("扫描项目结构失败")
                    lines = []
                if lines:
                    structure = "\n".join(lines) + "\n\n"
                if fp:
                    self._structure_cache[fp] = structure

        # 显式名单（含空名单）优先；默认提供项目技能和内置文档解析技能。
        sk = ""
        if session_skills is not None:
            sk = await self._attached_skills_block(workspace, session_skills)
        else:
            try:
                proj_skills = await self._skill_list(ws)
                sk = ("\n## 可用技能（用 load_skill 工具按需加载正文）\n" + "\n".join(proj_skills)
                      if proj_skills else "")
            except Exception:  # noqa: BLE001
                sk = ""
        return (structure or "") + (("\n" + sk + "\n") if sk else "")

    async def _memory_block(self, project_id: str, limit: int = 20) -> str:
        """项目记忆注入块（渐进式披露）：只给标题索引，正文让 agent 用 memory_read 取。

        v1 把最多 20 条正文整段塞进上下文，长会话里记忆块本身就成了负担；
        v2 的结构化条目在 project_memory.index_block 里只输出「标题 + 一句概括 + id」。
        """
        try:
            entries = await project_memory_mod.load_entries(project_id, limit=max(limit, 40))
            return project_memory_mod.index_block(entries)
        except Exception:  # noqa: BLE001 - 记忆注入失败不阻塞 run
            log.exception("加载项目记忆失败")
            return ""

    @staticmethod
    def _brief_original_request(brief: str) -> str:
        """从续跑说明里取回「上次请求原文」，供素材意图/计划提示判定。

        续跑说明由 ``_BRIEF_PREFIX`` 开头，第二段是「上次请求原文：\\n<正文>」。
        取不到就返回空串（意图提示只是加分项，缺失不影响续跑本身）。
        """
        marker = "上次请求原文："
        i = (brief or "").find(marker)
        if i < 0:
            return ""
        rest = brief[i + len(marker):].lstrip("\n")
        # 截到下一段（空行 + 已知段首）为止
        for stop in ("\n\n上次请求附带", "\n\n上次中断原因", "\n\n上次计划进度",
                     "\n\n上次已改动的文件", "\n\n上次已产出的素材", "\n\n上次组长的结论",
                     "\n\n上次最后的动作", "\n\n要求："):
            j = rest.find(stop)
            if j >= 0:
                rest = rest[:j]
        return rest.strip()

    async def _resolve_user_request(self, s, m) -> str:  # type: ignore[no-untyped-def]
        """找出这条唤起消息背后「用户真正的原话」。

        续跑消息（手动「继续」/ 系统自动续跑）的正文只是一句「继续完成任务目标」，
        真正需求在更早一条用户消息里。顺着续跑链（continue_from / auto_continue_from）
        往前回溯，拿到最初那条真实需求的正文；链断了就用当前这条的正文兜底。
        """
        text = (m.content or "") if m is not None else ""
        cur = m
        seen: set[str] = set()
        for _ in range(5):
            meta = (cur.meta_json or {}) if cur is not None else {}
            prev = str(meta.get("auto_continue_from") or meta.get("continue_from") or "")
            if not prev or prev in seen:
                break
            seen.add(prev)
            prev_run = await s.get(Run, _u(prev))
            if prev_run is None or not prev_run.trigger_msg_id:
                break
            cur = await s.get(Message, prev_run.trigger_msg_id)
            if cur is None:
                break
            text = cur.content or text
        return text

    async def _history_block(self, session_id: Optional[Any], exclude_msg_id: Optional[Any]) -> str:
        """同会话最近消息（旧→新），供模型理解连续对话上下文；失败返回空。

        截断策略：用户/助手的结论性文字最容易在截断处丢掉关键信息（「已经生成了
        哪 12 张图」「别改哪个文件」），所以用户消息留 600 字、助手回复留 800 字；
        更早的消息只留一行摘要感（300 字），靠条数控制总量，而不是靠砍每条。
        """
        if not session_id:
            return ""
        try:
            async with self._sf() as s:
                rows = (await s.execute(
                    select(Message).where(
                        Message.session_id == _u(str(session_id)),
                        Message.id != _u(str(exclude_msg_id)) if exclude_msg_id else True,
                    ).order_by(Message.created_at.desc()).limit(24))).scalars().all()
            rows = list(rows)
            rows.reverse()
            if not rows:
                return ""
            labels = {"user": "用户", "system": "系统"}
            total = len(rows)
            lines = []
            for idx, m in enumerate(rows):
                who = labels.get(m.author_type) or (m.agent_key or "助手")
                # 越靠后（越新）留得越多：最近 6 条是「当前正在做的事」的完整上下文
                recent = idx >= total - 6
                if recent:
                    cap = 800 if m.author_type != "user" else 600
                else:
                    cap = 300
                text = (m.content or "").strip()
                if len(text) > cap:
                    text = _clip_keep_tail(text, cap)
                if text:
                    lines.append(f"{who}：{text}")
            if not lines:
                return ""
            return "## 对话历史（当前会话，旧→新）\n" + "\n".join(lines)
        except Exception:  # noqa: BLE001
            log.exception("读取会话历史失败", extra={"session_id": str(session_id)})
            return ""

    async def _resume_block(self, run) -> str:
        """同会话上一轮的技术前沿：结局 / 未完成清单 / 已产出文件。

        只带「机器可核对的事实」（plan_json 与 artifacts），不重复聊天正文——正文已经由
        _history_block 带进来了。实测（kpgame 2026-10-29）：「继续完成任务目标」之后，
        新 run 用 9.7 分钟重新查了一遍上一轮已经查清的 36 张资产矩阵，一张图没出就被取消；
        下一次「继续」又重来。这些信息本来就在库里，只是从来没交给下一轮。
        「继续」续跑（trigger_msg_id 为空、brief 在 options 里）已经把这些前沿事实写进
        续跑说明，再拼一遍只会重复，故直接跳过。
        """
        if not run.session_id:
            return ""
        # 「继续」续跑没有触发消息，前沿事实已在 options 的续跑说明里，跳过避免重复
        if not run.trigger_msg_id and str((getattr(run, "options_json", None) or {}).get("brief") or "").strip():
            return ""
        try:
            async with self._sf() as s:
                trig = (await s.get(Message, _u(str(run.trigger_msg_id)))
                        if run.trigger_msg_id else None)
                # 点「继续」生成的续跑说明已经带齐了计划进度/量到的事实/最后的动作
                #（routers/runs.py 的 brief），再拼一遍只会让同一份清单出现两次。
                if trig is not None:
                    meta = trig.meta_json or {}
                    if meta.get("continue_from") or meta.get("auto_continue_from") \
                            or str(trig.content or "").lstrip().startswith("【继续上次被中断的任务】"):
                        return ""
                prev = await s.scalar(
                    select(Run).where(
                        Run.session_id == _u(str(run.session_id)),
                        Run.id != _u(str(run.id)),
                        Run.created_at <= run.created_at,
                        Run.status.in_(("succeeded", "failed", "cancelled", "blocked")),
                    ).order_by(Run.created_at.desc()).limit(1))
            if prev is None:
                return ""
            items = [dict(i or {}) for i in (prev.plan_json or [])]
            arts = await self.svcs.repo.list_artifacts(str(prev.id))
        except Exception:  # noqa: BLE001 - 前沿拿不到就当没有，不影响本轮
            log.exception("读取上一轮前沿失败", extra={"run_id": str(run.id)})
            return ""
        if not items and not arts:
            return ""
        lines = ["## 上一轮停在哪（系统自动带过来；以工作区现状为准，别照抄重做）",
                 f"- 上一轮结局：{prev.status}"
                 + (f"（{str(prev.error)[:120]}）" if prev.error else "")]
        if items:
            done = sum(1 for i in items if str(i.get("status")) == "completed")
            marks = {"completed": "✓", "in_progress": "◐", "pending": "•"}
            lines.append(f"- 上一轮清单（{done}/{len(items)} 完成；note 里是那一项查到的结论）：")
            for it in items[:8]:
                note = str(it.get("note") or "").strip()
                lines.append(
                    f"  {marks.get(str(it.get('status')), '•')} "
                    f"{str(it.get('content') or '')[:60]}"
                    + (f"——{note[:80]}" if note else ""))
            if len(items) > 8:
                lines.append(f"  …还有 {len(items) - 8} 项")
        if arts:
            paths = [str(a.rel_path) for a in arts][:12]
            lines.append(f"- 上一轮已产出 {len(arts)} 个文件（已落盘，不要重出）：")
            lines.append("  " + "、".join(paths) + ("…" if len(arts) > 12 else ""))
        lines.append("- 先按上面接着做；需要确认现状就看具体文件，不要从头再勘察一遍。")
        return "\n".join(lines)

    async def _post_system_error(self, run_ctx: RunContext, run, error: str) -> None:
        """run 失败时在会话里落一条系统消息，聊天流能直接看到失败原因。"""
        if not run.session_id:
            return
        try:
            msg = await self.svcs.repo.create_message(
                session_id=run.session_id, run_id=run.id,
                author_type="system", author_id=None, agent_key=None,
                content=f"执行失败：{(error or '未知错误')[:500]}", meta_json={})
            await self._emit(run_ctx, "message_new", {
                "session_id": str(run.session_id), "message_id": str(msg.id)})
        except Exception:  # noqa: BLE001
            log.exception("写入失败提示消息失败", extra={"run_id": run.id})

    async def _post_system_note(self, run_ctx: RunContext, run, note: str) -> None:
        """在会话里落一条系统提示（取消/中断等），让聊天流能直接看到发生了什么。"""
        if not run.session_id:
            return
        try:
            msg = await self.svcs.repo.create_message(
                session_id=run.session_id, run_id=run.id,
                author_type="system", author_id=None, agent_key=None,
                content=note[:500], meta_json={})
            await self._emit(run_ctx, "message_new", {
                "session_id": str(run.session_id), "message_id": str(msg.id)})
        except Exception:  # noqa: BLE001
            log.exception("写入系统提示消息失败", extra={"run_id": run.id})

    async def _post_reply(self, run_ctx: RunContext, run, text: Optional[str]) -> None:
        """把 run 的最终回复作为 agent 消息写入会话消息流，
        聊天页像普通消息一样展示（而非只藏在运行面板里），失败不影响 run。

        · 幂等：同一 (run_id, 正文) 只落一条。纠正重跑前会先把第一轮汇报发出去，
          收尾时若最终正文与它相同，不再重复落（否则同一段答复出现两次）。
        · 不硬截断到 4000：实测一份 4338 字的完整报告在 `⑥ 后续建议` 处被拦腰截断，
          末尾整段建议+结论在聊天里看不见（run 详情里却是全的）。这里放宽到
          20000 字只作病态保护，正常报告（几 K 字）一律完整。
        """
        if not (text or "").strip() or not run.session_id:
            return
        body = (text or "").strip()
        key = (str(run.id), body)
        if key in self._posted_replies:
            return
        self._posted_replies.add(key)
        try:
            msg = await self.svcs.repo.create_message(
                session_id=run.session_id, run_id=run.id,
                author_type="agent", author_id=None, agent_key="main",
                content=body[:_REPLY_MAX_CHARS], meta_json={})
            await self._emit(run_ctx, "message_new", {
                "session_id": str(run.session_id), "message_id": str(msg.id)})
        except Exception:  # noqa: BLE001
            self._posted_replies.discard(key)
            log.exception("写入 agent 回复消息失败", extra={"run_id": run.id})

    async def _harvest_notes(self, run_ctx: RunContext, team_id: str, workspace,
                             requirement: str, summary: str, change_report: Optional[dict],
                             run_id: str) -> None:
        """本轮收尾判断「值不值得沉淀成项目改动经验」，值得就分类追加到工作区 md。

        不写库、不流水账：判得起 JSON 就落条目，判不了/不值得就一行都不写，
        免得像以前那样每轮把记忆重写一遍、把真正有价值的做法挤掉。
        """
        if workspace is None:
            return
        try:
            files = [str(f.get("file") or "") for f in
                     ((change_report or {}).get("files") or []) if f.get("file")]
            text = (summary or "").strip()
            if not text and not files:
                return
            prompt = project_notes.judge_prompt(requirement or "", text, files,
                                                project_notes.index_titles(workspace))
            profile = await self._load_profile(team_id, await self._agent_profile_key(team_id, "main"))
            ctx = Ctx(
                role="main", run_type="memory", workspace="",
                requirement=prompt, project_name="", commands={}, payload={},
                model=(profile.get("model") or ROLE_MODELS.get("main") or LLM_MODEL),
                images=[], model_profile=profile,
            )
            llm = build_llm("main", ctx)
            resp = await asyncio.wait_for(llm.chat([{"role": "user", "content": prompt}],
                                                   [], ctx), timeout=60)
            verdict = project_notes.parse_judge(resp.text or "")
            if not verdict["worth"]:
                log.info("本轮不值得沉淀改动经验：%s", verdict.get("reason") or "（未说明）",
                         extra={"run_id": run_id})
                return
            touched = project_notes.append(workspace, verdict["entries"], run_id=run_id)
            if not touched:
                return
            await self._emit(run_ctx, "notes_saved", {
                "files": touched, "entries": len(verdict["entries"]),
                "reason": verdict.get("reason") or "",
                "titles": [e["title"] for e in verdict["entries"]][:6]})
            log.info("项目改动经验沉淀 %d 条 → %s", len(verdict["entries"]),
                     ", ".join(touched), extra={"run_id": run_id})
        except Exception as e:  # noqa: BLE001 - 沉淀失败不影响 run 收尾
            log.warning("沉淀项目改动经验失败（%s）", str(e)[:200], extra={"run_id": run_id})

    async def _remember(self, project_id: str, team_id: str, run_id: str,
                        text: Optional[str]) -> None:
        """run 成功收尾：交给 memory 模块做「多对话融合」。

        模型把本次结果与已有记忆、其他对话最近的结论合并成结构化条目：
        重复的合并、冲突的以新为准、上限 12 条；失败时退化为追加一条「项目现状」。
        手动补充的约定（manual）不参与改写。
        """
        new_text = (text or "").strip()
        if not new_text:
            return
        try:
            res = await project_memory_mod.consolidate(
                project_id, run_id=str(run_id), new_text=new_text)
            log.info("project memory updated",
                     extra={"run_id": str(run_id), "project_id": str(project_id),
                            "before": res.get("before"), "after": res.get("after"),
                            "used_ai": res.get("used_ai")})
        except Exception:  # noqa: BLE001 - 记忆更新失败不阻塞 run
            log.exception("更新项目记忆失败", extra={"run_id": run_id})

    async def _memory_input_text(self, run_id: str, final_summary: Optional[str],
                                 change_report: Optional[dict] = None) -> str:
        """记忆合并的输入文本：优先用本次汇报；汇报为空时用「落地动作 + 改动文件」兜底。

        为什么要兜底：调整类指令（改 UI、换配色、修布局）经常出现模型只勘察、
        最后没调 report_to_user 的情况 → final_summary 为空 → 记忆更新被直接跳过，
        项目记忆就永远停在上一轮，后续对话全按旧状态理解项目。
        """
        text = (final_summary or "").strip()
        if text:
            return text
        acts: list[str] = []
        try:
            events = await self.svcs.repo.get_events_since(run_id, 0)
        except Exception:  # noqa: BLE001
            events = []
        for e in events:
            if e.type != "tool_call":
                continue
            payload = e.payload_json or {}
            tool = str(payload.get("tool") or "")
            args = payload.get("args") or {}
            if tool == "run_command":
                continue
            if tool in LANDING_TOOLS and tool not in acts:
                key = str(args.get("path") or args.get("slug") or args.get("role") or "")
                acts.append(f"{tool}{('(' + key + ')') if key else ''}")
        parts: list[str] = []
        if acts:
            parts.append("本次落地的动作：" + "、".join(acts[:10]))
        files = []
        if change_report:
            files = [str(f.get("file") or "") for f in (change_report.get("files") or [])
                     if f.get("file")]
        if files:
            parts.append("改动文件：" + "、".join(files[:12]))
        return "\n".join(parts)


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


def _now():  # type: ignore[no-untyped-def]
    return datetime.now(timezone.utc)
