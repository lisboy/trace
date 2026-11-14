"""意图盘查（**只作为提示，不作为闸门**）。

## 这一层现在还做什么

判断用户这句话「更像是让助手动手落地」还是「更像是只要一个结论」。结论只有两个用途：
1. 给模型**一句软提示**（`hint_for()`）：「这条听起来像只要结论，但你自己判断，要改就改」，
   或者「这条听起来像要改，别只勘察」；
2. 决定 runtime 的「只勘察不动手」过程刹车开不开（`engage`）——纯问答时不该被催着改文件，
   但那是**提醒**，不是禁止。

## 这一层不再做什么（2026-11-14 用户拍板）

- **不再改 run_type**。以前判成 survey 就把整轮降级成 analyze，等于系统替模型决定「本轮不许改」；
  用户说「检查一下然后改掉」会被直接禁手。现在只有用户在前端亲手点「仅分析」才走 analyze。
- **不再往需求前面塞「本轮不要修改产品文件」这类硬提示**。那段的优先级压过模型自己的理解。
- **不裁任何工具**。工具权限本来就全开（见 orchestration/caps.py：effective_whitelist 不按
  run_type 裁剪），所以「要不要动手」完全是模型读完原话和现场之后的自由决定。

## 判别方式

- 先用关键词做**无歧义的快速预判**（命中就短路，省一次模型往返，用户反馈过判定太慢）；
- 关键词拿不准（含否定、说法含糊）再调一次极短的大模型判别（超时 20s）；
- 模型不可用一律按 `modify` 兜底——宁可多给一句「该动手就动手」的提醒，也不要让模型以为
  自己被禁止动手。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from game_agent.llm.base import Ctx
from game_agent.llm.factory import build_llm
from game_agent.logging_setup import get_logger

log = get_logger("orchestration.intent")

# 单次意图判别的超时上限：极短的辅助调用，不能按正文的 5×360s 拖死首轮。
INTENT_TIMEOUT_S = 20

_INTENT_PROMPT = """你是一个「意图盘查器」，只做一件事：判断用户这条消息更像是**让助手动手改东西**，还是**只要一个结论/解释/勘察**。

两类：
- modify（动手）：用户希望最终把东西**落到文件/代码/素材**上——改代码、修 bug、重构、
  加功能、删代码/删表、改配置、改数据库/给 SQL、生成图片/音频/视频、写文档/脚本等。
  哪怕措辞是「帮我看看」「怎么弄」，只要目的是把它改好/做出来，就是 modify。
- survey（只要结论）：用户**明确只要**一个结论、解释、原因、评估、方案对比。

判别要点：
- 有具体要落地的产物（改哪里、删什么、加什么、生成什么）→ modify。
- 「检查一下 / 排查一下 / 诊断一下 / 看看问题在哪」如果后面**没有**要求顺手改好 → survey；
  但只要带「然后修掉 / 改好 / 顺便处理」等要求落地的意思 → modify。
- 纯知识性提问、问项目用了什么技术、问某段代码为什么这样写 → survey。
- 拿不准时 → 一律 modify。

注意：你的结论**不会限制助手做什么**，只用来决定提示措辞，所以不必顾虑「判错了会不会禁手」。

用户消息：
<<<
{req}
>>>

只输出一个 JSON 对象，不要任何多余文字：
{{"intent":"modify" 或 "survey","mode":"fix" 或 "build","reason":"一句话理由"}}
（mode 仅在 intent=modify 时有意义：fix=修 bug/小改动，build=新建/生成/批量/重构。）"""

_VALID_INTENT = {"modify", "survey"}
_VALID_MODE = {"fix", "build"}

# ---------- 关键词预判（只为省一次模型往返，不参与任何封锁） ----------
# 刻意不用单字「做/写」当动手词——「怎么做/怎么写」是纯疑问，会把「只答疑」误判成「要动手」。

# 明确「动手落地」的动词。含单字「改/修」：能覆盖「有问题就修/有就改」这类省略宾语的短句；
# 其带来的「先别改」误判由 _NEG_WORDS 兜住。
_MODIFY_WORDS = (
    "改", "修", "修改", "改成", "改掉", "改好", "改动", "修复", "修掉", "修好", "修一下",
    "删除", "删掉", "移除", "去掉", "清除", "新增", "增加", "添加", "加上", "加个", "加一个",
    "创建", "新建", "实现", "重构", "优化", "迁移", "接入", "替换", "重命名",
    "安装", "部署", "导入", "导出", "生成", "批量", "落地", "搞定", "编译",
)

# 修 bug / 小改动（只影响提示语气：是「直接改掉」还是「按计划建起来」）
_FIX_WORDS = ("改", "修", "bug", "报错", "错误", "崩", "异常", "失败", "闪退", "卡死", "错")

# 纯勘察词：命中这些且**没有**任何动手动词时，才判 survey。
_SURVEY_WORDS = (
    "检查", "排查", "诊断", "看看", "看一下", "查一下", "原因", "为什么", "分析",
    "评估", "对比", "讲解", "解释", "梳理", "什么问题", "有什么问题", "怎么回事",
)

# 否定标记：出现即「用户先不让动」，交给模型拿捏（别硬判 modify）。
_NEG_WORDS = ("先别", "先不", "不要", "不用", "别改", "别动", "别修", "暂不", "先不要", "先不用")

# 「要的是一份东西，不是一次改动」：出现这些交付物词、且没有落地连词时，即便句子里带了
# 「优化/重构」这类动手动词也按 survey 判——用户要的是方案/建议本身。
# 实测误判：2026-11-14「分析下性能瓶颈并给出优化方案」→ 命中「优化」判成 modify。
_DELIVERABLE_WORDS = ("方案", "建议", "评估", "计划", "清单", "报告", "对比", "思路",
                      "怎么改", "怎么优化", "值不值得", "要不要")
# 落地连词：出现即说明「说完就要顺手做」，此时上面那条让位（仍判 modify）。
_ACT_THEN_WORDS = ("然后", "接着", "顺便", "顺手", "直接", "落地", "实现", "执行",
                   "改好", "动手", "按这个改", "就改", "修掉")

# 「改善类」动词：既可以是要动手，也可能只是用户在要一份「怎么优化」的方案，
# 所以带交付物词、又没有具体落地动词时，不把它们当成动手信号。
_IMPROVE_WORDS = ("优化", "重构", "迁移", "替换", "重命名", "接入", "升级", "改造")


@dataclass
class IntentResult:
    intent: str = "modify"          # modify | survey（**仅作提示依据，不封锁任何工具**）
    mode: str = "build"             # fix | build（只影响提示语气）
    reason: str = ""
    decided_by: str = "model"       # model | keyword | fallback | explicit

    @property
    def is_survey(self) -> bool:
        return self.intent == "survey"

    @property
    def is_fix(self) -> bool:
        return self.intent == "modify" and self.mode == "fix"

    def as_answer(self) -> str:
        """测试用：模拟一次直接给出该结论。"""
        return json.dumps({"intent": self.intent, "mode": self.mode,
                           "reason": self.reason}, ensure_ascii=False)


def guess_mode(user_request: str) -> str:
    """按措辞粗分 fix/build，只用于提示语气，不影响权限与守卫开关。"""
    t = (user_request or "").lower()
    if not t:
        return "build"
    return "fix" if any(w in t for w in _FIX_WORDS) else "build"


def _parse(text: str) -> tuple[str, str, str] | None:
    """从模型回复里抠出 JSON（容忍 ```json 包裹 / 前后废话）。"""
    t = (text or "").strip()
    if not t:
        return None
    if "```" in t:
        for part in t.split("```"):
            p = part.strip()
            if p.startswith("{") or p.lower().startswith("json"):
                t = p
                break
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(t[i:j + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    intent = str(obj.get("intent") or "").strip().lower()
    mode = str(obj.get("mode") or "").strip().lower()
    reason = str(obj.get("reason") or "").strip()
    if intent not in _VALID_INTENT:
        return None
    if mode not in _VALID_MODE:
        mode = "fix" if intent == "modify" and any(
            w in reason for w in ("bug", "修", "改", "错", "崩")) else "build"
    return intent, mode, reason


def _classify_by_keyword(req: str) -> IntentResult | None:
    """关键词预判：命中无歧义情况就短路返回，拿不准返回 None（交模型）。"""
    t = (req or "").strip()
    if not t:
        return None
    # 否定 → 拿不准，交模型（「先别改，先看看」不能被「改」误判成动手）
    if any(w in t for w in _NEG_WORDS):
        return None
    hit_mod = [w for w in _MODIFY_WORDS if w in t]
    hit_sur = [w for w in _SURVEY_WORDS if w in t]
    hit_deliver = [w for w in _DELIVERABLE_WORDS if w in t]
    # 「要一份方案/建议」+ 没有落地连词 + 也没有具体落地动词 → 判 survey
    strong_act = [w for w in hit_mod if w not in _IMPROVE_WORDS]
    if hit_deliver and not strong_act and not any(w in t for w in _ACT_THEN_WORDS):
        why = '、'.join(hit_deliver[:2])
        if hit_mod:
            why += f"（含「{'、'.join(hit_mod[:2])}」但无落地动词）"
        return IntentResult(intent="survey", mode="build",
                            reason=f"要的是交付物：{why}",
                            decided_by="keyword")
    if hit_mod:
        mode = "fix" if any(w in t for w in _FIX_WORDS) else "build"
        return IntentResult(intent="modify", mode=mode,
                            reason=f"关键词命中动手：{'、'.join(hit_mod[:4])}",
                            decided_by="keyword")
    if hit_sur:
        return IntentResult(intent="survey", mode="build",
                            reason=f"关键词命中勘察：{'、'.join(hit_sur[:4])}",
                            decided_by="keyword")
    return None


def _fallback(reason: str) -> IntentResult:
    """模型不可用时的兜底：按 modify（动手）——绝不让模型以为「本轮被禁止动手」。"""
    return IntentResult(intent="modify", mode="build",
                        reason=f"意图盘查不可用，按动手放行（{reason}）", decided_by="fallback")


async def classify_intent(user_request: str, *, profile: dict | None = None,
                          timeout_s: int = INTENT_TIMEOUT_S) -> IntentResult:
    """判断「更像要动手」还是「更像只要结论」。先关键词（快），未命中再调模型，异常兜底 modify。

    ⚠️ 结论**只用于提示措辞与刹车开关**，调用方不得据此降级 run_type 或禁改文件。
    """
    req = (user_request or "").strip()
    if not req:
        return _fallback("空请求")
    kw = _classify_by_keyword(req)
    if kw is not None:
        log.info("意图盘查（关键词预判）：%s/%s（%s）", kw.intent, kw.mode, kw.reason or "-")
        return kw
    ctx = Ctx(role="main", run_type="intent", requirement=req,
              model_profile=profile or {})
    prompt = _INTENT_PROMPT.replace("{req}", req)
    try:
        llm = build_llm("main", ctx)
        resp = await asyncio.wait_for(
            llm.chat([{"role": "user", "content": prompt}], [], ctx),
            timeout=timeout_s)
        parsed = _parse(getattr(resp, "text", "") or "")
        if parsed is None:
            log.warning("意图盘查返回无法解析，按动手放行：%r",
                        (getattr(resp, "text", "") or "")[:120])
            return _fallback("返回无法解析")
        intent, mode, reason = parsed
        log.info("意图盘查：%s/%s（%s）", intent, mode, reason or "-")
        return IntentResult(intent=intent, mode=mode, reason=reason, decided_by="model")
    except Exception as e:  # noqa: BLE001 - 盘查失败绝不能影响主流程
        log.warning("意图盘查失败，按动手放行：%s: %s", type(e).__name__, str(e)[:160])
        return _fallback(f"{type(e).__name__}: {str(e)[:120]}")


def hint_for(result: IntentResult) -> str:
    """把盘查结论翻成**一句软提示**（可以放行、也可以什么都不说）。

    设计要点（2026-11-14 用户拍板「保留判别但不作为闸门」）：
    - 只在「有信息量」时说话：判 survey 才提醒「可能只要结论」，判 modify 且是 fix 语气才提醒
      「别光勘察，定位到就改」；
    - 措辞一律带「你自己判断」「要改就改」，绝不出现「不许修改」「本轮禁止」这类禁手词；
    - 判 modify + build（新建/生成类）不发提示——那种需求模型本来就清楚要产出东西，
      多发一句只会占上下文。
    """
    if result.decided_by == "explicit":
        return ""
    if result.is_survey:
        return (
            "## 参考（不是命令）：这条听着像只要结论\n"
            "系统盘查觉得用户要的可能是**一个结论**（问题在哪、为什么、建议怎么改），"
            "而不是马上动手。这只是一句提醒，**你自己判断**：\n"
            "- 如果用户确实只要结论 → 从容定位根因，给出「结论 + 证据（文件:行号/日志原文）+ 建议」；\n"
            "- 如果读下来发现用户其实是想改好、或问到一半顺手就要落地 → **直接动手改**，"
            "不必等他说第二遍（需要用户拍板的业务口径仍然先 ask_user）。"
        )
    if result.is_fix:
        return (
            "## 参考（不是命令）：这条像是在报问题等修复\n"
            "系统盘查觉得用户是要把问题**修好**。仍然**你自己判断**："
            "先定位到根因，定位准了就在原地改掉并跑验证，不要只交一份分析就收尾。"
        )
    return ""


__all__ = ["IntentResult", "classify_intent", "guess_mode", "hint_for"]
