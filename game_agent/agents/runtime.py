"""AgentRuntime：统一的智能体执行引擎（设计 §7.1）。

- 每次工具调用推事件（多人可见）。
- 工具结果截断（默认 8k）。
- 每次 LLM 往返前校验 fencing token：锁丢了立刻停，杜绝双写。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import dataclass
from typing import Any

from game_agent.agents.tools import ToolContext, ToolResult, is_scratch_path
from game_agent.agents.tools.registry import (
    build_tool_specs, execute_tool_with_timeout,
)
from game_agent.config import TOOL_HARD_TIMEOUT_S, MAX_TOOL_CALLS_PER_ROUND, env_brief
from game_agent.llm.base import Ctx, LLMResponse
from game_agent.llm.context_window import (
    DEFAULT_CONTEXT_WINDOW, DIGEST_SECTIONS, VIEWBACK_MARK, compress_messages,
)
from game_agent.llm.factory import build_llm
from game_agent.llm.text_tool_calls import contains_unparsed_calls
from game_agent.logging_setup import get_logger
from game_agent.orchestration import steering

log = get_logger("agents.runtime")

# 同一轮工具调用的并行上限（信号量）。真并行只对互不依赖的只读查询有明显收益；
# 写操作本来就靠写锁串行，这里限并发主要是保护 asyncio 线程池和外部连接数不被一轮抽干。
_TOOL_PARALLELISM = 6

# tool_result 事件正文上限（前端轨迹整段展示这段文字，超出截断）。
_TOOL_RESULT_EVENT_CAP = 2000
# 联网来源块（[Sources] …）额外保留的字节预算。来源清单在正文最末尾，若沿用「从头截
# 2000 字」会把来源块截没，前端 parseSources 拿不到来源 → 「来源」芯片整块不渲染
# （长搜索结果尤其常见：中文 10 条结果轻松破 1200 字，用户实测有的搜索有来源、有的没有）。
_SOURCES_EVENT_CAP = 4000
_SOURCES_MARK = "[Sources]"


def _sig_arg(arg: Any) -> Any:
    """死循环签名用的参数归一化：数字/时间戳/随机段不参与签名，路径取 basename。

    同一实质操作换了个绝对路径或时间戳，签名应保持一致；不同文件/不同命令
    仍能区分（basename 保留）。
    """
    import re as _re
    if isinstance(arg, dict):
        return {k: _sig_arg(v) for k, v in arg.items()}
    if isinstance(arg, list):
        return [_sig_arg(v) for v in arg]
    if isinstance(arg, bool):
        return arg
    if isinstance(arg, (int, float)):
        return "#num"
    s = str(arg)
    # 长随机串要先于数字归一：像 token-0123456789abcdef… 这种「数字+字母」混合串，
    # 若先替换数字会被 #num 切断，[\w-]{20,} 就再也匹配不上，随机段参与签名。
    s = _re.sub(r"[A-Za-z0-9_]{20,}", "#rand", s)   # 纯字母/数字/下划线长串（hex token 等）
    if "/" not in s and "\\" not in s:
        # 带连字符的长串（UUID 等，≥20）。只在非路径字符串上做：相对路径里的
        # 长横杠目录名要原样保留，不能被归一成 #rand。
        s = _re.sub(
            r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+){2,}",
            lambda m: "#rand" if len(m.group()) >= 20 else m.group(),
            s,
        )
    s = _re.sub(r"\d{4,}", "#num", s)          # 年份/时间戳/长数字
    s = _re.sub(r"\d+", "#num", s)             # 短数字
    s = s.replace("\\", "/")
    # 绝对路径只保留最后一段；相对路径保留原样（不同文件名仍可区分）
    if s.startswith("/") or (len(s) > 2 and s[1] == ":"):
        s = s.rsplit("/", 1)[-1]
    return s


def _call_signature(name: str, args: Any) -> str:
    try:
        return f"{name}:{json.dumps(_sig_arg(args), sort_keys=True, ensure_ascii=False)}"
    except Exception:  # noqa: BLE001 - 参数不可序列化时退回工具名
        return name


def _tail_keep_head(text: str, limit: int, head: int = 80) -> str:
    """超长文本尾部优先截断：保留开头一小段作锚点 + 末尾主体。

    模型/工具的关键结论（根因、行号、下一步）多在中后部；旧逻辑 [:limit] 取头
    把最值钱的部分丢了。这里统一为「前 head 字 + …[中略]… + 尾部主体」，
    供思考落库、工具结果展示、续跑摘要复用。
    """
    if len(text) <= limit:
        return text
    keep = max(limit - head, 0)
    tail = text[-keep:]
    return (text[:head] + "\n…[中略]…\n" + tail).strip()


def _tail_with_sources(text: str) -> str:
    """截断工具结果用于事件展示，但保证末尾的 [Sources] 来源清单完整保留。

    正文照旧截到 _TOOL_RESULT_EVENT_CAP；若结果含来源块，则把来源块（含标记）原样
    补回末尾，整体上限 _SOURCES_EVENT_CAP。无来源块的普通结果行为不变。
    """
    cap = _TOOL_RESULT_EVENT_CAP
    if len(text) <= cap:
        return text
    idx = text.rfind(_SOURCES_MARK)
    if idx < 0:
        return text[:cap]
    body = text[:idx].rstrip()
    sources = text[idx:]
    if len(body) > cap:
        body = _tail_keep_head(body, cap)
    out = body + "\n\n" + sources
    if len(out) > _SOURCES_EVENT_CAP:
        # 来源块本身超长（极少见）：只保留来源块，避免被正文挤掉
        return sources[:_SOURCES_EVENT_CAP]
    return out


@dataclass
class AgentResult:
    text: str
    usage: dict = None  # type: ignore[assignment]
    # text 是不是运行时生成的兜底文案（不是模型写的话）。编排器据此优先用
    # report_to_user 已经存下的汇报，避免「模型明明汇报过，收尾却被兜底文案顶掉」。
    fallback: bool = False


# 「活没做完就交卷」最多打回几次。以前只提醒一次、且提醒内容是「把做完的打上勾」，
# 等于教模型：中途停下是合法的，交份「下一步」报告再补个勾就行（实测 128 条只写 32 条）。
MAX_PLAN_NUDGES = 2

# 模型「只输出思考、不输出正文」时最多重问几次。
# 以前是 bool 只给一次额度：实测 run 7c35a9af 第 50 轮空回复把额度用掉，第 54 轮
# 真正收尾时又空（reasoning 有内容、text/completion_tokens 都是 0），直接落成
# 「端点可能吞掉了回复」的兜底文案——而那轮其实改了 29 次文件、跑了 92 次工具调用。
# 现在按「每段有进展的执行」重新计数，最多重问 2 次；仍为空则按真实轨迹自动汇总。
MAX_EMPTY_NUDGES = 2

# 工具参数 JSON 没解析出来（多为一篇长文档/大补丁被输出长度上限截断）时，最多要求
# 换写法重做几次。以前这种调用会被静默当成「空参数」重发 3 次、然后被死循环检测掐停，
# 文档只能口述、没落盘。给几次带明确纠正（改成分块 append）的重试机会，仍不行才收尾。
MAX_ARG_RETRIES = 2

# 「只排查不动手」中途刹车。实测 run 27a8a2bf（2026-10-28）：一个「出战提示操作失败」
# 的小 bug，模型在 49 分钟里做了 348 次工具调用（78 次检索历史、90 条命令、23 个探针
# 脚本、翻了数据库和全库文件时间），产品代码 0 改动，最后被用户手动取消。
# 光靠收尾时的事后守卫没用——必须在过程里就把它拽回「改代码」。
EXPLORE_BRAKE_AT = 25          # 连续多少次非产品改动调用后第一次警告
EXPLORE_BRAKE_STEP = 30        # 之后每再这么多调用警告一次
EXPLORE_BRAKE_MAX = 2          # 警告次数上限，超过直接中止本轮
EXPLORE_HARD_STOP = 90         # 累计只读调用达到这个数还没改过产品代码 → 强制收尾

# D 收敛守卫（创建/生成类任务，走 _is_fix_task 之外的分支）：按「连续几次往返只在读」计。
# 只提醒、不掐停——任务没有次数上限，掐停只有用户点停止/写锁失效/死循环三种。
CONVERGE_NUDGE_AT = 7          # 连着这么多次往返零落地，先要一句结论 + 一张清单
CONVERGE_NUDGE_STEP = 7        # 之后再每这么多往返催一次（第二次起要它直接落笔）
CONVERGE_NUDGE_MAX = 3         # 同一段「没落地」的期间最多催几次

_BRAKE_MSG = (
    "（系统刹车）你已经连续调用了 {n} 次工具，一次都没落地交付物"
    "（write_file/edit_file 写的都是 target/_probe 之类的探针脚本不算）。\n"
    "用户要的是那份东西本身——修 bug 就改代码，写文档/方案就动笔成文件，不是让你做侦探。\n"
    "现在立刻停止勘察，按下面顺序做：\n"
    "1. 用已有证据下判断（修 bug：选**最可能的一个原因**，报错文案对应的调用点 → 它上面的失败分支；"
    "写文档：看过的东西够起草就先起草，别等「把整个项目读完」）；\n"
    "2. 直接 write_file / edit_file 把第一版落到该落的路径上，不要等「完全确定」；\n"
    "3. 落完跑最小验证（起服务/跑一条命令/截图/回读一遍），不对就基于新证据改第二版。\n"
    "禁止：再写探针脚本、再查数据库/存档/文件修改时间、再用 search_history 找用户说过什么"
    "（需求就在最早那条用户消息里）。若确实缺只有用户能给的信息（账号/复现路径），"
    "现在就用 ask_user 问一句，不要继续猜。"
)

_HARD_STOP_MSG = (
    "（系统刹车）本次任务已累计 {n} 次只读调用，交付物仍是 0 落地，判定为无效勘察，"
    "强制收尾。请如实说明：查到了什么、为什么没落地、卡在哪。"
)

# D 收敛守卫的两段式提醒：第一次只要「一句结论 + 一张清单」（把已经在脑子里的东西落到
# 纸上，下一步才知道该动哪里），第二次起要它直接产出第一版。
_CONVERGE_PLAN_MSG = (
    "（系统提示）你连着 {n} 次调用工具都只是在读（看文件 / grep / 列目录 / 查存档），"
    "没有落下任何东西：没写文件、没出图、没出音频、也没建待办清单。\n"
    "先把已经查到的东西交代清楚，再接着做：\n"
    "1. 正文里写一行结论：入口在哪个文件哪一行、这次要改/要产的到底是哪一块；\n"
    "2. 调用 update_plan 列 3–6 项可验证待办（第一项 in_progress，其余 pending）。\n"
    "列完就照清单动手，不要再去读下一批文件——需求写在最早那条用户消息里，"
    "不用靠通读仓库来确认它。")

_CONVERGE_WRITE_MSG = (
    "（系统提示）清单立了，但仍是 0 落地：没有写进产品文件的改动，"
    "也没有 generate_image / generate_audio 的真实产物。\n"
    "现在就把第一版做出来：素材类直接调 generate_image / generate_audio 把第一批派出去"
    "（多张/多条用 items，一次最多 24 项）；代码或文档类直接 write_file 把第一版落到"
    "目标路径，写完再跑最小验证（编译 / 起服务 / 截图 / 回读），不对就照报错改第二版。\n"
    "禁止：继续列目录、通读文件、翻存档找线索；禁止用一份「我打算这么做」的报告代替执行。")

# 工具参数 JSON 没解析出来时给模型的纠正（不是逻辑失败，别让它原样重发同样的巨型调用）。
# 实测 2026-11-06 山西可研 run：模型想把一份长评估文档一次 write_file 落盘，参数在传输/
# 生成时被截断，适配层静默回退成 {}，write_file 报「工具错误：'path'」，模型原样重试
# 三次 → 死循环检测掐停 → 文档根本没落盘，最后只能口述「文件尚未落盘」。
_ARG_RETRY_HINT = (
    "（系统提示）你上一条工具调用的**参数 JSON 没解析成功**（{err}），"
    "所以它没有真正执行——不是文件/命令本身的问题。"
    "常见原因：一次要塞的内容太长（尤其 write_file 的整篇文档/大补丁）被输出长度上限截断，"
    "或正文里的引号/换行没转义。\n"
    "请换一种更稳的写法再试：\n"
    "· 写长文档/大文件：拆成多块——第一块用 write_file（默认 mode=write）写开头，"
    "后面每块用 write_file(mode=\"append\", path=同一个文件) 追加，每块控制在两三千字以内；\n"
    "· 改已有文件：用 edit_file 只替换要变的那几行，别整篇重写；\n"
    "· 无论如何不要原样重发上一条参数——它已经被证明会被截断。")

# 看图不落字：像素只在「看图那一轮」的请求里有效，模型对图的判断又只写在 reasoning 里
#（不进历史），所以结论不写下来，下一轮必然重新打开、重新判。实测 kpgame 立绘任务
#（2026-10-29）：51 次看图 / 178 次文件级查看，6 张图每张 8–9 遍，40 个看图轮次吃掉
# 11.5 分钟 + 95.5k completion tokens，最后仍只能写「无法目视确认」。
VIEW_NUDGE_AT = 3              # 连着这么多轮只在看图，要求把判断写成文件
VIEW_NUDGE_MAX = 2             # 同一段看图期间最多催几次

_VIEW_VERDICT_MSG = (
    "（系统提示）你已经连着 {n} 轮只在看图，没有留下任何文字判断。"
    "像素不会留到下一轮，你的判断也只存在于上一轮的思考里——不写下来，下一轮就只能"
    "重新打开、重新猜，这就是「看了十几遍还说不清」的原因。现在按顺序做：\n"
    "1. write_file 写 `.trace/art-review.md`：一行一张图，写清 `文件路径 → 动作/构图/风格/合格与否`"
    "（已有内容先 read_file 再追加，不要覆盖）；\n"
    "2. 之后要复核先读这份文件；确实需要看细节时才重新 view_image 单张。\n"
    "验收结论写成文件才算数，写在思考里等于没写。")

# 哪些需求算「改代码/修 bug」——只有这类才启用上面的刹车。
# 出图/配音/批量数据处理/纯分析都不该被催着改代码，否则会误伤正常的长任务。
# 只看需求正文的**结尾一段**：instruction 前面还拼着项目结构/记忆/历史，不是需求本身。
_FIX_WORDS = (
    "修", "改", "bug", "报错", "错误", "异常", "失败", "崩溃", "闪退",
    "卡住", "卡死", "卡顿", "白屏", "没反应", "不工作", "坏了", "无效",
)
# 批量/生成类的大活：天然要查很多文件，不能按「小 bug」催它直接改。
# 注意别用裸「全部/所有」：「点全部翻开卡死了」按钮名会被误判成批量任务。
_BATCH_WORDS = ("所有角色", "全部角色", "每个角色", "批量", "全量", "重构",
                "生成", "导出", "迁移", "所有文件", "全部文件")
# 用户明说「只看不许动」——这种任务绝不该被刹车催着改代码。
# 收尾守卫（orchestrator._needs_action）用同一份词表，别再抄一份。
ANALYZE_ONLY_WORDS = ("只分析", "不要改", "别改", "先别动", "不用改", "只看看", "别动代码")

# 「只检查/只排查/先诊断」这类意图：用户要的是**一个结论（哪里有问题）**，不是立刻改。
# 它们常带「检查/排查/诊断」等动词，会被误判成「动手改代码」——于是过程刹车刚开跑就催改、
# 收尾时没改动就判 failed / 强制重做。这里单独识别：这一类走「先出结论、要动手等你点头」。
# ⚠️ 只认明确的「只/先/别动」话术；裸「检查一下 X」仍按可能动手处理，避免把「检查完顺手修好」
# 的需求误判成纯勘察（那会变得一样啰嗦）。
INSPECT_ONLY_WORDS = (
    "只检查", "仅检查", "只排查", "仅排查", "只诊断", "仅诊断",
    "检查一下", "检查下", "排查一下", "排查下", "诊断一下", "诊断下",
    "先检查", "先排查", "先诊断", "先定位", "找一下原因", "找出原因", "排查一下原因",
    "检查一下原因", "定位一下", "帮我定位", "帮我找出", "先看看问题", "看看问题出在哪",
    "检查问题", "排查问题", "看看什么问题", "什么问题", "是什么原因", "什么原因",
    # 「帮我查一下/确认」这类口语化诊断：要的是原因，不是马上动手。
    # 注意不收「帮我看看/看看为什么」——太宽泛，且与既有语义（「帮我看看为什么白屏」按动手指令）冲突。
    "帮我查一下", "帮我查查", "帮我确认", "帮我核实", "帮我查下", "查一下为什么",
    "在哪里", "在哪", "哪一块", "哪里出",
)
# 同一句里出现这些「明确让我改/修」的话术时，即便有检查/排查字样也按动手指令处理。
_IMPERATIVE_FIX_WORDS = ("修复", "修好", "修一下", "帮我修", "给我修", "改好", "改一下",
                         "帮我改", "给我改", "解决", "搞定", "弄好", "直接改", "直接修",
                         "改成", "改为",
                         "优化", "重构", "实现", "新增", "接入", "接上", "补上", "调整",
                         "删掉", "加上", "换成", "换掉",
                         "加个", "加一个", "加一下", "搞个", "做个", "做个", "写个", "建个",
                         "去掉", "移除", "改成", "怎么改", "怎么加", "怎么弄")


def wants_inspect_only(text: str) -> bool:
    """用户只要「先检查/排查/诊断出问题」，而不是马上动手改。"""
    t = (text or "").strip()
    if not t:
        return False
    if any(w in t for w in ANALYZE_ONLY_WORDS):
        return True
    if any(w in t for w in _IMPERATIVE_FIX_WORDS):
        return False
    return any(w in t for w in INSPECT_ONLY_WORDS)

# 2026-10-28 会话「首页为啥还有滚动条」：疑问句里一个 _FIX_WORDS 都没有，刹车整段没
# 启用，模型连着查了 84 轮、产品代码一行没改。用户问「为啥还有 X」不是在做学术提问，
# 是在说 X 不该存在、把它弄掉。
# 判定要「疑问」+「问题还在」两截同时命中，只看「为什么」会把
# 「这个函数为什么这样写」这种纯提问也拽进刹车。
_ASK_WORDS = ("为啥", "为什么", "为何", "怎么", "咋")
_STILL_BROKEN_WORDS = ("还有", "仍有", "还是", "依旧", "仍然", "去不掉", "消不掉",
                       "没去掉", "出不来", "没出来", "不显示", "显示不出", "打不开",
                       "点不动", "加载不出", "没响应", "又出现")


def is_symptom_complaint(text: str) -> bool:
    """「首页为啥还有滚动条」这类疑问式故障描述：要的是动手，不是解答。"""
    t = (text or "").strip()
    return (any(a in t for a in _ASK_WORDS)
            and any(s in t for s in _STILL_BROKEN_WORDS))


_VIEW_TOOL_NAMES = ("view_image", "view_images")

# 插话消息的开头：明确「接着当前进度」，不要理解成新任务
_STEER_HEAD = ("（用户在你执行过程中插话。请接着**当前进度**处理这条补充要求："
               "已做完的不要重做，已完成的部分直接沿用；只调整需要调整的地方。）\n")


def _steer_message(note: dict) -> dict:
    """一条插话 → 一条 user 消息。带图时拼成 content parts。

    图片走 user 消息而不是视觉回灌（VIEWBACK_MARK），所以不会在下一轮被过期——
    用户主动附的截图就是要一直看得见。
    """
    text = _STEER_HEAD + str(note.get("text") or "")
    imgs = [str(u).strip() for u in (note.get("images") or []) if str(u or "").strip()]
    if not imgs:
        return {"role": "user", "content": text}
    return {"role": "user", "content": [
        {"type": "text", "text": text},
        *[{"type": "image_url", "image_url": {"url": u}} for u in imgs],
    ]}


#: orchestrator 拼在 system_prompt 最前面的授权块（指导项 + 本轮权限）的起始标记。
#: 这里只认「第一块」：指导项在最前，权限提示紧随其后，直到第一个非授权小节为止。
_AUTH_HEAD_MARKS = ("## 经验指导", "【本轮权限：", "## 引导", "## 指导项")


def _split_authority_head(prompt: str) -> tuple[str, str]:
    """把 system_prompt 开头的授权块切出来（环境块要排在它后面）。

    只切「开头连续的一串」：从第一个非空行开始，按空行分段，只要段落起始命中
    `_AUTH_HEAD_MARKS` 就继续吃；遇到别的段落（角色提示 / 清单）立即停手。
    没命中就原样返回 ("", prompt)，行为与以前完全一致。
    """
    text = prompt or ""
    if not text.strip().startswith(_AUTH_HEAD_MARKS):
        return "", text
    paras = text.split("\n\n")
    n = 0
    while n < len(paras) and paras[n].lstrip().startswith(_AUTH_HEAD_MARKS):
        n += 1
    head = "\n\n".join(paras[:n]).strip()
    body = "\n\n".join(paras[n:]).lstrip()
    return head, body


def _explore_weight(tool_names: list[str]) -> int:
    """这一轮里真正算「只读勘察」的调用条数——看图不算。

    美术/页面验收本来就要一轮轮对照图。把 view_image 也算进只读调用里，
    一个「6 角色 × 6 资产」的验收任务必然撞上 EXPLORE_HARD_STOP=90（实测 kpgame
    2026-10-29：12:33 被「已强制收尾」时人还在验图，最终报告只能写「未能目视确认」）。
    """
    return sum(1 for n in tool_names if n not in _VIEW_TOOL_NAMES)


def _landed_in_round(calls) -> bool:  # type: ignore[no-untyped-def]
    """这一轮是否真有「产品改动/素材产出」的调用。

    参数没解析出来（arguments_error）或 path 为空的写调用不算——否则一次失败的
    write_file（其实是空参数）会让「只勘察不动手」守卫误以为已经落过地。
    """
    from game_agent.agents.tools import is_scratch_path
    for tc in calls:
        if getattr(tc, "arguments_error", ""):
            continue
        if tc.name in ("write_file", "edit_file", "delete_file"):
            rel = str((tc.arguments or {}).get("path") or "").strip()
            if rel and not is_scratch_path(rel):
                return True
        elif tc.name in ("generate_image", "generate_audio",
                         "dispatch_art", "dispatch_audio"):
            return True
    return False


def _is_fix_task(instruction: str) -> bool:
    tail = (instruction or "").strip()[-400:]
    if any(w in tail for w in _BATCH_WORDS):
        return False
    # 只分析 / 只检查（要一个结论，不是马上改）：过程刹车不该催它动手。
    if wants_inspect_only(tail):
        return False
    return any(w in tail for w in _FIX_WORDS) or is_symptom_complaint(tail)

# 一次任务没有次数上限：以前按角色定几十次轮数（组长 60），等于亲口告诉模型
# 「你的次数是有限的」，它就真的把 128 条语音拆成好几批、每批只做一个阶段，最后交回
# 一份「本轮汇报（截至信息收集与执行阶段结束）」。现在做完为止，掐停只有三种：
# 用户点停止、写锁 fencing 失效、同「工具+参数」连续重复 3 次（死循环，不是次数多）。

_NUDGE_OPEN_PLAN = (
    "（系统提示）待办清单还有 {n} 项没完成：{items}。"
    "现在交卷就是「只做到一半」——这活是你的，做完为止，没有「留给下一轮」这回事。\n"
    "1. 能做的现在就用工具做完（写文件 / 跑脚本 / 派发素材），"
    "每做完一项就 update_plan 把该项置 completed（整体覆盖整份清单）。\n"
    "2. 量大的一批（几十上百条）不要靠一条条工具调用数着做："
    "写成一个参数化、能断点续跑、进度落到文件的脚本，用 start_service 后台跑起来，"
    "再用 service_logs 轮询到全部完成——脚本在后台跑，一次就能跑到干净。\n"
    "3. 确实被卡住做不了的：保持原状态，并在该项 note 里写清卡点（缺什么、被什么挡住）。\n"
    "禁止：为了清单好看把没做的勾成已完成；禁止用「下一步怎么做」的交接报告代替执行。")

_NUDGE_HANDOFF = (
    "（系统提示）你的清单已全部打了勾，但答复里写的还是「下一步 / 还差 / 待补」——"
    "说明有活儿没落地。请二选一：现在就把提到的剩余工作做完；"
    "或者把这些项用 update_plan 加回清单（置 pending 并在 note 里写清卡在哪），"
    "然后再给最终答复。不要交一份「下一步怎么做」的报告就收工。")

# 清单还没做完就调 report_to_user：这是阶段汇报，不是交付。汇报已经给用户看了，
# 但不代表可以收工——继续把剩下几项做完，最后再汇报一次。
_NUDGE_STAGE_REPORT = (
    "（系统提示）你刚才调用了 report_to_user，但待办清单还有 {n} 项没完成：{items}。"
    "这段汇报会被当作**阶段汇报**，不是最终交付——现在还没到收尾的时候。\n"
    "请继续把剩下的项做完：每做完一项用 update_plan 把该项置 completed（整体覆盖清单），"
    "全部完成后，再用 report_to_user 写一份完整的最终汇报。"
    "如果确实卡住了，就在对应项的 note 里写清卡点，并在最终汇报里如实说明哪些没做完。")



class AgentRuntime:
    def __init__(self, role: str, whitelist: list[str], system_prompt: str,
                 instruction: str, tctx: ToolContext, ctx: Ctx,
                 extra_tool_specs: dict | None = None,
                 base_whitelist: list[str] | None = None) -> None:
        self.role = role
        self.whitelist = whitelist
        # 角色全量白名单（未裁剪 analyze / caps）：运行中改「仅分析 / 能力开关」时在它上面重算。
        # 不传时退回本次生效白名单（老调用方行为不变，只是运行中开关重算会受限）。
        self.base_whitelist = list(base_whitelist if base_whitelist is not None else whitelist)
        self.extra_specs = dict(extra_tool_specs or {})
        self.system_prompt = system_prompt
        # 把编排器拼好的提示拆成「头部约束块 / 正文」两份底稿：运行中改运行选项时只重建头部，
        # 正文（角色提示 + manifest）与指导项保持不变。只拆一次，之后每次重建都基于底稿，
        # 保证反复切换不会把旧约束段一层层叠加。
        head0, body0 = _split_authority_head(system_prompt)
        self._prompt_body = body0
        self._prompt_guidance: list[str] = []
        if head0:
            for para in head0.split("\n\n"):
                p = para.strip()
                if p and not p.startswith(("【本轮权限：", "【自动化测试已开启】", "【本轮关闭了")):
                    self._prompt_guidance.append(p)
        self.instruction = instruction
        self.tctx = tctx
        self.ctx = ctx
        # 端点 503/限流退避重试时，往运行轨迹发一条内联提醒（系统日志只留 WARNING）
        self.ctx.on_llm_retry = self._emit_llm_retry
        self.llm = build_llm(role, ctx)
        self.tools = build_tool_specs(whitelist, extra_tool_specs)
        self._summary_cache: dict[str, str] = {}  # 压缩摘要缓存（按内容 hash），同 run 不重复调 LLM
        self._compact_noted = False  # 本次压缩周期是否已发过「正在压缩」进度提示
        self._compact_deadline = 0.0  # 本次压缩周期的墙钟截止（monotonic），超出改用要点兜底
        # 本轮真实执行轨迹的轻量账本：模型只吐 reasoning、不吐文字时，用它生成
        # 「做了什么」的真实汇总，而不是拿一句「端点可能吞了回复」糊弄用户。
        self._trace_files: dict[str, str] = {}   # 产品文件 -> 动作（写入/修改/删除）
        self._trace_assets: list[str] = []       # 生成的素材（slug / 工具名）
        self._trace_commands = 0                 # 执行命令条数（验证证据）
        self._trace_screens = 0                  # 页面截图次数
        self._trace_tool_counts: dict[str, int] = {}

    def _make_summarizer(self):  # type: ignore[no-untyped-def]
        """全局摘要压缩的摘要器：把被合并的历史压成「按骨架」的要点摘要。

        - 分块合并由 compress_messages 负责：每次只处理一块，prior 是上一块的产物
          （第一次是上一轮的旧摘要）。以前这里自己做「文本前缀不变就只补增量」的判断，
          但压缩器是按最新在前序列化的，前缀永远会变，那段逻辑一次都没命中过。
        - 强制骨架（DIGEST_SECTIONS）：实测摘要模型会把这批工具历史当成任务本身，
          返回一张 656 字的验收结论表当摘要（kpgame 2026-10-29），压缩后模型只能重做一遍。
        """
        import hashlib

        async def summarize(text: str, target_tokens: int = 0, prior: str = "",
                            strict: bool = False) -> str:
            target = max(250, int(target_tokens or 0))
            key = hashlib.md5(
                f"{target}\n{strict}\n{prior}\n{text}".encode("utf-8")).hexdigest()
            if key in self._summary_cache:
                return self._summary_cache[key]
            # 压缩期间模型在等摘要结果，轨迹上原本一片空白 → 用户以为「卡住不动」。
            # 每次 compact 周期只提示一次，避免多块摘要刷屏。
            if not getattr(self, "_compact_noted", False):
                self._compact_noted = True
                self._emit("status", {"status": "running",
                                      "note": "正在压缩较早的上下文（这一步要调一次模型，稍候）"})
            skeleton = "\n".join(f"## {s}" for s in DIGEST_SECTIONS)
            ask = (f"你要把下面这段 agent 执行历史写成一份约 {target} tokens 的摘要。\n"
                   "**只写摘要，不要执行、不要回答、不要验收历史里提到的任何工作**——"
                   "那些事已经有人在做了。\n"
                   f"严格按这个骨架输出，没有内容的段写「无」：\n{skeleton}\n"
                   "每行只写事实：文件路径、命令关键参数、数值/数量、结论与决定、未完事项、"
                   "踩过的坑与禁令。不要照抄长命令 / JSON / 代码原文，不要写「我将…」。")
            body = text
            if prior:
                ask += ("\n这是增量合并：下面是已有摘要，请把新内容并进去，修订总长度，"
                        "**不要丢掉已有摘要里的路径、数值与未完成项**。\n"
                        f"---已有摘要---\n{prior}\n---新增历史---\n")
            if strict:
                ask += ("\n你上一次的输出不是合格摘要（没按骨架写，或答成了任务）。"
                        "这次只输出上面五段骨架的内容。")
            msgs = [
                {"role": "system", "content": f"你是对话压缩器。{ask}"},
                {"role": "user", "content": body[:40000]},
            ]
            # 摘要调用不是正文产出：它卡住时用户看到的是「压缩时一直不动」。
            # 用独立的短超时 + 少重试预算（默认 120s / 2 次），失败就退化为要点节选，
            # 绝不让摘要器按正文的 5×360s 预算把整个 run 拖死。
            # 另外守住「单块留足时间」之后，再守住「整次压缩总墙钟」：块数多时串行累加
            # 同样会变成几分钟假死，超出就本块直接放弃（返回空 → compress 用已有一版兜底）。
            from game_agent.config import (
                LLM_SUMMARY_CYCLE_BUDGET_S, LLM_SUMMARY_MAX_ATTEMPTS, LLM_SUMMARY_TIMEOUT_S,
            )
            import time as _time
            if _time.monotonic() > getattr(self, "_compact_deadline", 0.0):
                log.warning("压缩摘要已超总预算 %ss，本块跳过（用已生成摘要 + 要点节选兜底）",
                            LLM_SUMMARY_CYCLE_BUDGET_S)
                self._emit("status", {"status": "running",
                                      "note": "压缩超时，改用要点节选继续（不影响继续执行）"})
                return ""
            resp = await self._chat_with_heartbeat(
                msgs, [], label="上下文压缩",
                timeout_s=LLM_SUMMARY_TIMEOUT_S,
                max_attempts=LLM_SUMMARY_MAX_ATTEMPTS)
            try:
                # 压缩也是一次真实调用：进用量与会话统计，别让压缩成本凭空消失。
                # purpose='summarize' 落库：它的 prompt 只是「被压的那段历史」（几 k），
                # 不是模型真正看到的上下文——用量/上下文占用要能把它和正文往返区分开，
                # 否则顶栏「上下文」会取到这条摘要的 prompt，凭空显示成 3%。
                await self._record_llm_call(resp, purpose="summarize")
            except Exception:  # noqa: BLE001 - 统计失败不影响压缩
                pass
            s = (resp.text or "").strip()
            if s:
                self._summary_cache[key] = s
            return s

        return summarize

    async def _compact(self, messages: list[dict]) -> list[dict]:
        """把整条历史压进模型窗口，并把压缩结果**写回** messages。

        以前压缩只发生在适配层、压完就丢：下一轮又从全量历史重算「丢最旧的 N 条」，
        于是丢的数量逐轮上涨（实测同一 run 里 4→7→13→29），界面上却完全看不出
        压缩发生过。写回之后历史以「摘要 + 最近原文」的形态继续增长，
        并给轨迹发一条 context_compacted 事件（前端「已压缩上下文」分隔行）。
        """
        window = int(getattr(self.llm, "context_window", 0) or 0) or DEFAULT_CONTEXT_WINDOW
        rep: dict = {}
        self._compact_noted = False  # 新一次压缩周期：允许再发一条「正在压缩」进度提示
        # 整次压缩（含所有分块摘要调用）的墙钟截止：超出就停止再调模型，用已生成的
        # 摘要 + 要点节选兜底——压缩这一步本身不能变成几分钟的假死。
        import time as _time
        from game_agent.config import LLM_SUMMARY_CYCLE_BUDGET_S
        self._compact_deadline = _time.monotonic() + LLM_SUMMARY_CYCLE_BUDGET_S
        try:
            out = await compress_messages(messages, window, self._make_summarizer(),
                                          rep, tools=self.tools,
                                          facts_provider=self._digest_facts)
        except Exception:  # noqa: BLE001 - 压缩失败不能让 run 直接崩，交给端点报错兜底
            log.exception("上下文压缩失败，按原样发送")
            return messages
        if rep.get("media_expired") or rep.get("media_dropped"):
            log.info("图片字节预算：过期回灌 %s 张、超出预算省略 %s 张，本轮实发 %s 张 / %sKB",
                     rep.get("media_expired"), rep.get("media_dropped"),
                     rep.get("images"), int(rep.get("bytes") or 0) // 1024)
        if rep.get("compressed"):
            self._emit("context_compacted", {
                "agent_key": self.role, "round": int(self.ctx.llm_round or 0),
                "merged_msgs": int(rep.get("dropped") or 0),
                "merged_tokens": int(rep.get("dropped_tokens") or 0),
                "digest_tokens": int(rep.get("digest_tokens") or 0),
                "target_tokens": int(rep.get("target_tokens") or 0),
                "before": int(rep.get("est_before") or rep.get("est_tokens") or 0),
                "after": int(rep.get("est_tokens") or 0),
                "window": int(rep.get("window") or window),
                "budget": int(rep.get("budget") or 0),
                # 摘要质量与覆盖面：merged_tokens 大而 summarized_chars 小 = 又有历史没进摘要器
                "digest_ok": bool(rep.get("digest_ok")),
                "chunks": int(rep.get("chunks") or 0),
                "chunks_dropped": int(rep.get("chunks_dropped") or 0),
                "summarized_chars": int(rep.get("summarized_chars") or 0),
                "pinned": int(rep.get("pinned") or 0),
            })
            log.info("上下文已压缩：%s → %s tokens（合并 %s 条，摘要 %s tokens，"
                     "%s 块 / %s 字符%s）",
                     rep.get("est_before"), rep.get("est_tokens"),
                     rep.get("dropped"), rep.get("digest_tokens"),
                     rep.get("chunks"), rep.get("summarized_chars"),
                     "" if rep.get("digest_ok") else "，摘要不合格已退化")
        return out

    async def _digest_facts(self) -> str:
        """压缩时附在摘要前面的硬事实：当前未完成的待办（含 note 里的结论）。

        摘要模型被要求「保留未完成事项」，但那是模型自觉；清单在 DB 里是权威且体积很小，
        直接原样带上，避免压缩后模型忘了还剩哪几项。
        """
        try:
            items = await self._open_plan_items()
        except Exception:  # noqa: BLE001 - 清单读不到就只带摘要
            return ""
        if not items:
            return ""
        marks = {"in_progress": "进行中", "pending": "未开始"}
        lines = []
        for i, it in enumerate(items[:8], 1):
            note = str(it.get("note") or "").strip()
            lines.append(f"{i}. [{marks.get(str(it.get('status')), str(it.get('status')))}] "
                         f"{str(it.get('content') or '')[:60]}"
                         + (f"——{note[:80]}" if note else ""))
        if len(items) > 8:
            lines.append(f"…还有 {len(items) - 8} 项")
        return "待办清单（未完成）：\n" + "\n".join(lines)

    def _emit_llm_retry(self, attempt: int, max_attempts: int, delay_s: float,
                        error: str) -> None:
        self._emit("llm_retry", {
            "round": self.ctx.llm_round,
            "attempt": attempt, "max_attempts": max_attempts,
            "delay_ms": int(delay_s * 1000), "error": str(error)[:300],
        })

    def _emit(self, kind: str, payload: dict, ephemeral: bool = False) -> None:  # type: ignore[no-untyped-def]
        from game_agent.core.abstractions import EventEnvelope
        rc = self.tctx.run_ctx
        try:
            asyncio.get_running_loop().create_task(self.tctx.run_ctx.bus.emit(EventEnvelope(
                team_id=rc.team_id, project_id=rc.project_id, run_id=rc.run_id,
                task_id=self.tctx.task_id, type=kind, payload=payload, ephemeral=ephemeral,
            )))
        except Exception:  # noqa: BLE001 - 事件失败不影响主流程
            log.exception("emit event failed: %s", kind)

    async def _post_stage_report(self, text: str, *, round_no: int, open_count: int) -> None:
        """把一段「阶段汇报」落成会话里的一条 agent 消息（聊天流按完整 Markdown 渲染）。

        与最终汇报（orchestrator._post_reply）的区别：这里带 `meta.kind=stage_report`，
        前端据此把它调成与最终汇报不同的外观、并标明「还有 N 项没做完，继续执行」，
        同时**不接管该 run 的执行轨迹宿主**（轨迹仍挂在最后那条收尾消息上）。
        失败只记日志、绝不打断本 run 的继续执行。
        """
        rc = self.tctx.run_ctx
        if not rc.session_id:
            return
        try:
            msg = await rc.repo.create_message(
                session_id=rc.session_id, run_id=rc.run_id,
                author_type="agent", author_id=None, agent_key=self.role,
                content=text[:8000],
                meta_json={"kind": "stage_report", "round": round_no,
                           "open_count": open_count})
            from game_agent.core.abstractions import EventEnvelope
            await rc.bus.emit(EventEnvelope(
                team_id=rc.team_id, project_id=rc.project_id, run_id=rc.run_id,
                task_id=self.tctx.task_id, type="message_new",
                payload={"session_id": str(rc.session_id), "message_id": str(msg.id),
                         "author_type": "agent", "kind": "stage_report"},
            ))
        except Exception:  # noqa: BLE001 - 落消息失败不影响主流程
            log.exception("写入阶段汇报消息失败", extra={"run_id": rc.run_id})

    async def _assert_fencing(self) -> None:
        f = self.tctx.run_ctx.fencing
        if f is not None:
            await f.assert_valid()

    async def _check_cancel(self) -> None:
        """用户点「取消」后 runs.cancel_requested 置位——每轮工具循环前检查，
        命中即抛 CancelledError（orchestrator 已有分支落 cancelled 状态）。"""
        try:
            run = await self.tctx.run_ctx.repo.get_run(self.tctx.run_ctx.run_id)
            if run is not None and getattr(run, "cancel_requested", False):
                log.info("cancel requested, aborting agent loop", extra={"run_id": self.tctx.run_ctx.run_id})
                raise asyncio.CancelledError()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 取消检查失败不影响主流程
            log.exception("check cancel_requested failed")

    async def _apply_model_switch(self) -> None:
        """运行中切换模型：取走待应用的切换 → 重新解析档位 → 重建 LLM。

        为什么不能「改 ctx 就完事」：模型/bast_url/key/context_window/reasoning_effort
        在 build_llm 那一刻就被固化进 LLM 适配器实例了（self.llm.model 等），运行期改
        ctx.model_profile 不会自动传导。所以这里显式重建；只换大脑，不动历史与工具。

        档位解析只走 orchestrator._load_profile（服务端权威）：前端只能给 profile_key，
        不能给 base_url/key——否则等于把「用哪个端点、什么密钥」交给浏览器。
        """
        note = steering.take_model_switch(str(self.tctx.run_ctx.run_id))
        if note is None:
            return
        orch = self.tctx.orchestrator
        if orch is None:
            return
        pkey = note.get("profile_key") or None
        effort = (note.get("effort") or "").strip()
        rc = self.tctx.run_ctx
        try:
            if pkey:
                profile = await orch._load_profile(rc.team_id, pkey)
            else:
                # 空串=回到「跟随角色绑定默认」
                profile = await orch._load_profile(
                    rc.team_id, await orch._agent_profile_key(rc.team_id, self.role))
            if effort:
                profile = {**profile,
                           "params": {**(profile.get("params") or {}),
                                      "reasoning_effort": effort}}
            self.ctx.model_profile = profile
            self.ctx.model = profile.get("model") or self.ctx.model
            self.llm = build_llm(self.role, self.ctx)
            label = profile.get("model") or "角色默认模型"
            self._emit("status", {"status": "running",
                                  "note": f"已切换模型 → {label}（本轮后续对话生效）"})
            log.info("runtime model switched", extra={"run_id": rc.run_id, "role": self.role,
                                                      "model": self.ctx.model,
                                                      "effort": effort or None})
        except Exception:  # noqa: BLE001 - 切模型失败不能带崩正在跑的任务
            log.exception("apply model switch failed", extra={"run_id": rc.run_id})
            self._emit("status", {"status": "running", "note": "切换模型失败，继续用原模型"})

    async def _apply_settings(self) -> None:
        """运行中改运行选项：能力开关 / 会话权限 / 仅分析 → 立刻作用到这一轮。

        背景（用户实测）：「我中途调整的时候，没有生效，都是以最开始的会话来的」。原因是
        caps / analyze / permission_scope 只在 run 开跑时读进 run_ctx.meta 与 ToolContext，
        运行期再改前端只管 localStorage，跑着的 run 完全看不到。这里每轮开头把邮箱里最新
        的目标值落到「这一轮真正生效的三处」：
        1. run_ctx.meta —— 写回目标值，后续续跑/日志一致；
        2. run_ctx.run_type —— 仅分析开关（runtime 的只读刹车判断、以及后续子智能体入口都读它）；
        3. tctx.permission_scope —— 工具层路径守卫实际用的就是它；
        4. 工具白名单 + tools schema —— 在角色**全量**白名单上重算（不是二次裁剪，关掉再打开能回来）。
        系统提示头部的约束块（能力提示/权限提示）也一起换新，否则模型仍按旧的「不做什么」行事。
        """
        note = steering.take_settings_switch(str(self.tctx.run_ctx.run_id))
        if note is None:
            return
        from game_agent.orchestration import caps as caps_mod
        from game_agent.orchestration import policy as policy_mod
        rc = self.tctx.run_ctx
        meta = rc.meta if isinstance(rc.meta, dict) else {}
        changed: list[str] = []
        try:
            # ---- 能力开关 ----
            if "caps" in note:
                new_caps = caps_mod.normalize_caps(note.get("caps"))
                if new_caps != caps_mod.normalize_caps(meta.get("caps")):
                    changed.append("能力")
                meta["caps"] = new_caps
            # ---- 仅分析 ----
            if "analyze" in note and note.get("analyze") is not None:
                want = "analyze" if note.get("analyze") else "modify"
                if want != rc.run_type:
                    changed.append("仅分析开启" if want == "analyze" else "仅分析关闭")
                rc.run_type = want
                self.ctx.run_type = want  # 上下文视图与适配层也读它
            # ---- 会话权限 ----
            if "permission_scope" in note and note.get("permission_scope") is not None:
                scope = policy_mod.normalize_scope(note.get("permission_scope"))
                if scope != self.tctx.permission_scope:
                    changed.append("全部权限" if scope == "full" else "仅工作区")
                meta["permission_scope"] = scope
                self.tctx.permission_scope = scope
            # ---- 重算白名单 + 工具 schema（在角色全量白名单上） ----
            effective = caps_mod.effective_whitelist(
                self.base_whitelist, rc.run_type, meta.get("caps"))
            effective = effective + list(self.extra_specs)  # MCP 动态工具仍挂上
            self.whitelist = effective
            self.tools = build_tool_specs(effective, self.extra_specs)
            # ---- 重建 system_prompt 头部约束块（顺序：能力提示 / 权限提示） ----
            head_bits: list[str] = []
            cap_hint = caps_mod.caps_hint(caps_mod.normalize_caps(meta.get("caps")))
            if cap_hint:
                head_bits.append(cap_hint)
            head_bits.append(policy_mod.permission_hint(self.tctx.permission_scope))
            self.system_prompt = self._rebuild_system_prompt(head_bits)
            label = "、".join(changed) if changed else "运行选项已更新"
            self._emit("status", {"status": "running",
                                  "note": f"已应用运行选项（{label}），下一轮起生效"})
            log.info("runtime settings switched", extra={"run_id": rc.run_id, "role": self.role,
                                                        "changed": changed, "run_type": rc.run_type,
                                                        "scope": self.tctx.permission_scope})
        except Exception:  # noqa: BLE001 - 改运行选项失败不能带崩正在跑的任务
            log.exception("apply settings switch failed", extra={"run_id": rc.run_id})
            self._emit("status", {"status": "running", "note": "应用运行选项失败，继续用原设置"})

    def _rebuild_system_prompt(self, head_bits: list[str]) -> str:
        """用「新约束块」重建 system_prompt：指导项（底稿）+ 新约束块 + 正文（底稿）。

        底稿在 __init__ 一次性拆好（_prompt_guidance / _prompt_body），所以反复切换只会
        替换头部约束，不会把旧段层层叠加。
        """
        merged = "\n\n".join(self._prompt_guidance + [b for b in head_bits if b])
        return f"{merged}\n\n{self._prompt_body}" if self._prompt_body else merged

    async def _open_plan_items(self) -> list[dict]:
        """待办清单里没打勾的项（用于"活没做完就交卷"的打回判定）。"""
        try:
            run = await self.tctx.run_ctx.repo.get_run(self.tctx.run_ctx.run_id)
            items = list(getattr(run, "plan_json", None) or [])
            return [dict(i or {}) for i in items
                    if str((i or {}).get("status")) != "completed"]
        except Exception:  # noqa: BLE001 - 读计划失败不影响主流程
            log.exception("read plan_json failed")
            return []

    # 交卷正文里这些说法 = 它把没做完的活儿写成「交接报告」了（实测：32/128 后写「四、下一步」）
    _HANDOFF_WORDS = ("下一步", "接下来需要", "剩余工作", "还差", "还没写", "还没做",
                      "尚未完成", "未完成", "待补", "待写", "只完成了", "没有做完")

    def _quit_early_reason(self, open_items: list[dict], text: str,
                           nudges: int) -> str:
        """模型想直接交卷时，返回打回理由；空串表示可以结束本轮。

        第二档（handoff）只在**已经打回过一次**之后才生效：正常收尾的报告结尾写
        「下一步建议…」很常见，为此 nag 模型只会惹恼用户；但如果我们已经提醒过
        「活没做完」，它却只把清单全勾了就交回一份「下一步」报告，那是用打勾蒙事。
        """
        if open_items:
            return "open_plan"
        t = (text or "").strip()
        if nudges >= 1 and any(w in t for w in self._HANDOFF_WORDS):
            return "handoff"
        return ""

    def _instruction_msg(self) -> dict:
        """首条 user 消息；有附图时转 OpenAI vision content parts（非视觉模型由适配层降级）。"""
        if not self.ctx.images:
            return {"role": "user", "content": self.instruction}
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": self.instruction},
                *[{"type": "image_url", "image_url": {"url": u}} for u in self.ctx.images],
            ],
        }

    def _note_trace(self, tool: str, args: dict, result_text: str) -> None:
        """记录本轮真实落地动作（只记产品改动，过程文件/探针不算）。

        用途只有一个：模型收尾时只吐 reasoning、不吐正文（实测 run 7c35a9af 第 54 轮
        completion_tokens=0）时，用这份账本生成真实的「做了什么」，而不是笼统地说
        「端点可能吞掉了回复」——用户看到上方 92 次工具调用、29 次改文件，却被一句
        甩锅文案打发，完全不知道到底改了什么。
        """
        try:
            if result_text.startswith(("工具错误", "未执行", "工具不存在")):
                return
            self._trace_tool_counts[tool] = self._trace_tool_counts.get(tool, 0) + 1
            if tool in ("write_file", "edit_file", "delete_file"):
                path = str((args or {}).get("path") or "").strip()
                if path and not is_scratch_path(path):
                    self._trace_files[path] = {
                        "write_file": "新增/覆盖", "edit_file": "修改", "delete_file": "删除",
                    }.get(tool, "修改")
            elif tool in ("generate_image", "generate_audio", "edit_image",
                          "generate_sfx", "generate_music", "save_character_sheet"):
                key = str((args or {}).get("slug") or (args or {}).get("preset_name")
                          or (args or {}).get("name") or "").strip()
                item = f"{tool}{'(' + key + ')' if key else ''}"
                if item not in self._trace_assets:
                    self._trace_assets.append(item)
            elif tool == "run_command":
                self._trace_commands += 1
            elif tool == "screenshot_page":
                self._trace_screens += 1
            elif tool == "report_to_user":
                # 汇报正文由 orchestrator.set_final_summary 存，runtime 不用重复记
                pass
        except Exception:  # noqa: BLE001 - 记账失败不能影响主流程
            log.exception("note trace failed: %s", tool)

    def _trace_summary(self) -> str:
        """把本轮真实执行轨迹写成一段可读的落地清单；没有落地动作返回空串。"""
        lines: list[str] = []
        if self._trace_files:
            files = list(self._trace_files.items())
            shown = "、".join(f"{p}（{act}）" for p, act in files[:12])
            more = f" 等 {len(files)} 个" if len(files) > 12 else ""
            lines.append(f"- 改动文件 {len(files)} 个：{shown}{more}")
        if self._trace_assets:
            shown = "、".join(self._trace_assets[:12])
            more = f" 等 {len(self._trace_assets)} 个" if len(self._trace_assets) > 12 else ""
            lines.append(f"- 生成素材 {len(self._trace_assets)} 个：{shown}{more}")
        if self._trace_commands or self._trace_screens:
            bits = []
            if self._trace_commands:
                bits.append(f"运行命令 {self._trace_commands} 次")
            if self._trace_screens:
                bits.append(f"页面截图 {self._trace_screens} 次")
            lines.append("- 验证：" + "、".join(bits))
        return "\n".join(lines)

    def _final_text(self, text: str) -> tuple[str, bool]:
        """收尾文本兜底，返回 (给用户看的文本, 是否运行时兜底)。

        分两种情况，不能混为一谈：
        1. 模型正常给了正文 → 原样用；
        2. 模型一个字都没吐：
           a. 本轮有真实落地动作 → 按执行轨迹自动生成「做了什么」的事实清单
              （明确标注「系统按执行轨迹汇总，不是模型原话」），用户至少知道改了什么；
           b. 本轮什么也没落地 → 才提示「模型没有输出文字结果」，并建议重发/换档位。
        旧版对两种情况都甩「端点可能吞掉了回复或调用协议」，把干了活的 run 说成
        像是失败，用户完全无法判断结果（截图反馈的就是这一条）。
        """
        body = (text or "").strip()
        if body:
            return text, False
        trace = self._trace_summary()
        if trace:
            return ("（系统按本轮执行轨迹汇总，不是模型原话：模型这次只输出了思考过程，"
                    "没有生成文字总结。）\n本轮实际已完成：\n" + trace +
                    "\n\n**【原因 / 说明 / 风险点】本轮模型没有写出收尾总结，这三项无法自动生成**："
                    "上面只是按工具调用记录的事实清单（改了哪些文件、跑了什么验证），"
                    "不代表改动原因、方案取舍或潜在风险。要拿到完整总结，请让助手再汇报一次，"
                    "或直接追问「为什么这么改、有什么风险」。"
                    "\n\n详细过程见上方执行轨迹；如需调整，直接继续发消息即可。"), True
        return ("（这次执行没有返回任何内容 —— 模型没有输出文字结果，"
                "本轮也没有检测到实际改动或产物。上方执行过程是真实的；"
                "**因此没有可用的「原因 / 说明 / 风险点」总结**。"
                "可重新发起这条需求，或换一个模型档位。）"), True

    async def _chat_with_heartbeat(self, messages: list[dict], tools: list,
                                   *, on_delta=None, summarizer=None, label: str = "模型",
                                   timeout_s: int | None = None,
                                   max_attempts: int | None = None):  # type: ignore[no-untyped-def]
        """包一层 LLM 调用：等模型期间每 ~10s 发一条 ephemeral 状态心跳。

        为什么需要：网关挂住不吐增量、或非流式调用（汇报/摘要）没有 delta 时，
        界面上轨迹一动不动，用户就以为「卡死了」。心跳会在「模型处理中，已等待 N 秒…」
        里报出已等待时长，让人知道它还在跑。心跳只走 WS（ephemeral），不落库、不刷屏；
        只要有真实的思考增量在流，就不额外发（避免和思考流重复）。调用一结束立即停。
        """
        start = time.monotonic()
        state = {"last": start}
        wrapped = None
        if on_delta is not None:
            def wrapped(rd: str, td: str) -> None:  # noqa: F811 - 有增量就刷新活跃时间
                state["last"] = time.monotonic()
                try:
                    on_delta(rd, td)
                except Exception:  # noqa: BLE001 - 增量回调失败不影响收集
                    pass
        done = asyncio.Event()

        async def beat() -> None:
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=3)
                    return
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() - state["last"] < 8:
                    continue
                waited = int(time.monotonic() - start)
                self._emit("status", {"status": "running",
                                      "note": f"{label}处理中，已等待 {waited} 秒…",
                                      "waiting": True}, ephemeral=True)

        # 只在调用方明确收紧预算时才传这两个参数：老实现/测试替身没有它们时也能照常跑，
        # 不会因为多了两个 None 关键字参数抛 TypeError。
        budget_kw: dict = {}
        if timeout_s is not None:
            budget_kw["timeout_s"] = timeout_s
        if max_attempts is not None:
            budget_kw["max_attempts"] = max_attempts

        task = asyncio.create_task(beat())
        try:
            return await self.llm.chat(
                messages, tools or [], self.ctx,
                on_delta=wrapped, summarizer=summarizer, **budget_kw)
        finally:
            done.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _make_thinking_streamer(self, round_no: int = 0):  # type: ignore[no-untyped-def]
        """流式思考节流器：LLM 增量攒批（0.4s / 160 字符）后以 ephemeral 事件实时推送。
        delta 不落库（整轮完整版 agent_thinking 随后落库兜底），刷新页面后以完整版为准。

        带上 `round`：一个 agent 循环里每轮 LLM 往返算一轮。前端据此判断
        「这是新一轮的思考」→ 清掉上一轮残留文本，避免新思考行显示上一轮内容。
        """
        import time as _time
        buf: list[str] = []
        state = {"last": _time.monotonic()}

        def on_delta(rd: str, _td: str) -> None:
            if not rd:
                return
            buf.append(rd)
            now = _time.monotonic()
            if now - state["last"] >= 0.4 or sum(len(x) for x in buf) >= 160:
                state["last"] = now
                self._emit("agent_thinking_delta",
                           {"agent_key": self.role, "round": round_no,
                            "text": "".join(buf)}, ephemeral=True)
                buf.clear()

        return on_delta

    async def _record_llm_call(self, resp: LLMResponse, purpose: str = "agent") -> None:
        """单次 LLM 往返即时落库（llm_calls + usage_ledger）。

        必须按次记录而非 agent 循环结束后汇总：否则 run 不结束统计永远是 0，
        运行中的会话看不到实时 token 消耗（前端徽标依赖 calls > 0）。

        purpose='summarize' 标记「压缩历史的摘要调用」：它也是真实花费，但 prompt 只是
        被压的那一段（几 k），不是模型看到的完整上下文。落库时显式带上，用量统计才能
        把它从「最近一次正文往返」里排除，而不是靠 llm_transcripts 关联（那张表 14 天就清）。
        """
        rc = self.tctx.run_ctx
        try:
            repo = rc.repo
            await repo.record_usage(
                team_id=rc.team_id, run_id=rc.run_id, agent_key=self.role,
                kind="tokens", amount=int(resp.usage.get("prompt_tokens", 0)
                                               + resp.usage.get("completion_tokens", 0)),
            )
            if hasattr(repo, "record_llm_call"):
                await repo.record_llm_call(
                    team_id=rc.team_id, run_id=rc.run_id, agent_key=self.role,
                    model=self.ctx.model,
                    prompt_tokens=int(resp.usage.get("prompt_tokens", 0)),
                    completion_tokens=int(resp.usage.get("completion_tokens", 0)),
                    cached_tokens=int(resp.usage.get("cached_tokens", 0)),
                    cache_reported=1 if resp.usage.get("cache_reported") else 0,
                    latency_ms=int(resp.latency_ms),
                    purpose=purpose,
                    # 与 llm_transcripts.call_id 对齐：计数行和大字段行说的是同一次往返
                    **({"call_id": resp.trace_id} if resp.trace_id else {}),
                )
        except Exception:  # noqa: BLE001 - 统计失败不影响主流程
            log.exception("record_llm_call failed for %s", self.role)

    async def _record_transcript(self, rec: dict) -> None:
        """适配层回调：把「发给模型什么 / 模型返回什么」整轮落库（llm_transcripts）。

        回调在 LLM 往返之后、下一轮之前被 await，所以必须自己吞掉异常：
        观测数据写不进去可以忍，把用户的 run 带崩不能忍。
        """
        rc = self.tctx.run_ctx
        try:
            repo = rc.repo
            if hasattr(repo, "record_llm_transcript"):
                await repo.record_llm_transcript(team_id=rc.team_id, run_id=rc.run_id, **rec)
        except Exception:  # noqa: BLE001
            log.exception("record_llm_transcript failed for %s", self.role)

    def _assemble_sys_prompt(self) -> str:
        """把授权头切到运行环境之前（授权块必须绝对排第一），与 run() 起始逻辑一致。"""
        head, body = _split_authority_head(self.system_prompt)
        env = env_brief(str(self.tctx.workspace))
        return f"{head}\n\n{env}\n\n{body}" if head else f"{env}\n\n{body}"

    async def run(self) -> AgentResult:
        # 授权类硬约束（指导项 / 本轮权限作用域，由 orchestrator 拼在 system_prompt 最前面）
        # 必须**绝对**排第一：这里是实际发给模型的那一份，它会在最前面再拼一段运行环境——
        # 实测（2026-11-02，查 llm_transcripts）这样做会把指导项顶到 1670 字之后，
        # 模型照样按旧习惯去读工作区外路径。所以先按标记切出授权块，放到环境块之前。
        sys_prompt = self._assemble_sys_prompt()
        messages: list[dict] = [
            {"role": "system", "content": sys_prompt},
            self._instruction_msg(),
        ]
        total_usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0,
                             "cached_tokens": 0, "latency_ms": 0}
        recent_sigs: list[str] = []  # 工具调用签名（工具+参数），用于死循环检测
        plan_nudges = 0  # 已就「活没做完就交卷」打回几次（上限 MAX_PLAN_NUDGES）
        stage_reports = 0  # 清单未完成时连续遇到的「阶段汇报」次数（超过 MAX_PLAN_NUDGES 就当最终收尾）
        empty_nudges = 0  # 本段执行里「模型只吐思考、不吐正文」已重问几次（上限 MAX_EMPTY_NUDGES）
        explore_calls = 0  # 自上次改产品代码以来，累计的只读/探针调用次数
        explore_brakes = 0  # 已发过几次「只排查不动手」刹车
        probe_rounds = 0   # 连续几次往返只读不落地（D 收敛守卫，创建/生成类任务）
        probe_nudges = 0   # 这段「没落地」期间已催过几次
        view_rounds = 0    # 连续几轮只在看图（看图不落字守卫）
        view_nudges = 0    # 这段看图期间已催过几次写结论
        arg_retries = 0    # 工具参数 JSON 没解析出来、已要求换写法重做几次
        # 每轮「发给模型什么 / 模型返回什么」落库的钩子（只有适配层知道压缩后的真实请求）
        self.ctx.on_llm_request = self._record_transcript
        for rnd in itertools.count():
            self.ctx.llm_round = rnd
            await self._assert_fencing()
            await self._check_cancel()
            # 运行中切换模型：用户在执行中改了模型选择 → 下一轮 LLM 调用换新档位。
            # 只有 main 消费这条通道（子智能体各用自己角色绑定的档位，不该抢这条切换）；
            # 放在「取插话」之前：同一次请求既插话又切模型时，先换大脑再把它带进来。
            if self.role == "main":
                # 运行中改运行选项（能力开关/权限/仅分析）：先落约束，再换模型，最后取插话——
                # 顺序保证同一次请求里「关掉出图 + 切模型 + 插话」都按新约束生效。
                await self._apply_settings()
                await self._apply_model_switch()
                # 用户插话：不取消当前执行，只是把这句话并入这一轮的上下文（模型接着做，不重来）
                for note in steering.drain(str(self.tctx.run_ctx.run_id)):
                    imgs = list(note.get("images") or [])
                    self._emit("status", {"status": "running", "note":
                                          (f"收到你的插话（含图 {len(imgs)} 张），已并入当前执行"
                                           if imgs else "收到你的插话，已并入当前执行")})
                    messages.append(_steer_message(note))
            # system 消息按当前（可能刚被运行选项改过的）约束重建：能力提示/权限提示换了新块，
            # 这一轮就要让模型看到，否则它仍按旧的「不做什么/只能碰哪」行事。
            messages[0] = {"role": "system", "content": self._assemble_sys_prompt()}
            # 全局压缩：把较早的历史合并成摘要、结果写回历史，下一轮从「摘要 + 最近原文」继续
            messages = await self._compact(messages)
            resp: LLMResponse = await self._chat_with_heartbeat(
                messages, self.tools, label="模型",
                on_delta=self._make_thinking_streamer(rnd),
                summarizer=self._make_summarizer())
            await self._record_llm_call(resp)
            total_usage["prompt_tokens"] += resp.usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += resp.usage.get("completion_tokens", 0)
            total_usage["cached_tokens"] += int(resp.usage.get("cached_tokens") or 0)
            total_usage["latency_ms"] += resp.latency_ms
            total_usage["calls"] += 1
            if resp.reasoning:
                self._emit("agent_thinking", {"agent_key": self.role, "round": rnd,
                                              "reasoning": _tail_keep_head(resp.reasoning, 4000)})
            if resp.text:
                self._emit("agent_message", {"agent_key": self.role, "text": resp.text})
            if not resp.tool_calls:
                # 空回复、或正文里仍残留没解析成功的调用协议（工具名不在白名单/参数畸形）：
                # 端点把调用吞了。以前直接 return → run 静默「成功」，界面上就是模型把
                # <｜｜DSML｜｜invoke ...> 当回复吐出来然后停住、用户拿不到任何结果。重问一次。
                stalled = not (resp.text or "").strip() or contains_unparsed_calls(resp.text)
                if stalled and empty_nudges < MAX_EMPTY_NUDGES:
                    empty_nudges += 1
                    log.warning("模型未产出有效回复（空内容或残留调用协议），要求重新作答（第 %s 次）",
                                empty_nudges, extra={"run_id": str(self.tctx.run_ctx.run_id)})
                    self._emit("status", {"status": "running",
                                          "note": "这轮没拿到有效结果，已要求模型重新作答"})
                    messages.append({"role": "assistant", "content": resp.text or ""})
                    messages.append({"role": "user", "content": (
                        "（系统提示）你上一条回复没有产生有效结果：既没有可执行的结构化工具调用，"
                        "也没有给用户看得懂的文字结论。请重新作答：要么正常发起工具调用，"
                        "要么用文字讲清当前结论与卡点。不要把调用协议原文写在正文里。")})
                    continue
                # 交卷强制（主线会话专属）：清单还挂着未完成项、或打回后只补勾不补活就交
                # 「下一步」报告 → 打回继续做，而不是让它把半成品写成汇报。
                if self.role == "main" and "update_plan" in self.whitelist:
                    open_items = await self._open_plan_items()
                    reason = self._quit_early_reason(open_items, resp.text, plan_nudges)
                    if reason and plan_nudges < MAX_PLAN_NUDGES:
                        plan_nudges += 1
                        if reason == "open_plan":
                            marks = {"in_progress": "进行中", "pending": "未开始"}
                            listed = "；".join(
                                f"{i}. {str(it.get('content') or '')[:40]}"
                                f"（{marks.get(str(it.get('status')), str(it.get('status')))}）"
                                for i, it in enumerate(open_items, 1)) or "（清单为空）"
                            content = _NUDGE_OPEN_PLAN.format(
                                n=len(open_items), items=listed[:900])
                            note = (f"清单还有 {len(open_items)} 项未完成，"
                                    f"打回继续做（第 {plan_nudges}/{MAX_PLAN_NUDGES} 次）")
                        else:
                            content = _NUDGE_HANDOFF
                            note = f"打回：清单已勾完但正文仍在交「下一步」（第 {plan_nudges}/{MAX_PLAN_NUDGES} 次）"
                        log.info("quit-early nudge (%s)", reason,
                                 extra={"run_id": str(self.tctx.run_ctx.run_id)})
                        self._emit("status", {"status": "running", "note": note})
                        messages.append({"role": "assistant", "content": resp.text or ""})
                        messages.append({"role": "user", "content": content})
                        continue
                final_text, fallback = self._final_text(resp.text)
                return AgentResult(text=final_text, usage=total_usage, fallback=fallback)
            messages.append({
                "role": "assistant", "content": resp.text or "",
                "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in resp.tool_calls],
            })
            # 有工具调用 = 这段执行有进展，空回复重问额度重新计算：
            # 后面收尾时再遇到「只吐思考不吐正文」，仍能再要一次正文，
            # 而不是被前面某次空回复把额度一次用光（run 7c35a9af 的实际教训）。
            empty_nudges = 0
            # 一轮里的工具调用数封顶：模型偶尔会把一长串清单拆成几十个并行调用同时发出来，
            # 一轮就把线程池 / 外部连接数 / SQLite 写连接打满，拖慢同进程其它会话。
            # 超出上限的这部分直接判失败（不执行），并提示分批——比整轮一起卡住强。
            overflow = resp.tool_calls[MAX_TOOL_CALLS_PER_ROUND:]
            calls = resp.tool_calls[:MAX_TOOL_CALLS_PER_ROUND]
            if overflow:
                self._emit("status", {"status": "running",
                                      "note": f"一轮发起了 {len(resp.tool_calls)} 个调用，超过上限 "
                                              f"{MAX_TOOL_CALLS_PER_ROUND}，其余 {len(overflow)} 个未执行，"
                                              "请分批继续"})
                log.warning("tool call fan-out capped: %s run, %s dropped",
                            len(resp.tool_calls), len(overflow),
                            extra={"run_id": str(self.tctx.run_ctx.run_id)})
            # 同轮多个工具调用并行执行（读文件/grep 等互不依赖；单 run 持写锁独占工作区），
            # 结果按 tool_calls 原序回填，缩短每轮 wall time。
            # 用信号量限制并发度：一轮里真发十几个也只同时跑有限的几个，排队进线程池，
            # 避免把 asyncio 默认线程池（几十个槽）和外部连接数瞬间抽干。
            for tc in calls:
                self._emit("tool_call", {"agent_key": self.role, "round": rnd,
                                         "tool": tc.name, "args": tc.arguments,
                                         **({"arguments_error": tc.arguments_error}
                                            if tc.arguments_error else {})})
            sem = asyncio.Semaphore(_TOOL_PARALLELISM)

            async def _run_tool(tc):  # type: ignore[no-untyped-def]
                async with sem:
                    # 逐条计时：用户要能看清「每条命令各自跑了多久」。进程内 monotonic
                    # 最准（不受时钟精度/批量回填影响），记进 tool_result 的 payload。
                    _t0 = time.monotonic()
                    # 参数 JSON 没解析出来：**不执行**，直接回一条可执行的纠正。
                    # 以前这里会拿空参数去调工具（write_file 于是报「'path'」这种没营养的
                    # KeyError），模型原样重发同样的巨型调用 → 三连相同签名被死循环检测掐停。
                    if tc.arguments_error:
                        text = f"工具错误：{tc.name} 的参数没能解析（{tc.arguments_error}），" \
                               "本次未执行。不要原样重发，按参数过长/转义问题改写后再试。"
                        self._emit("tool_result", {"agent_key": self.role, "round": rnd,
                                                   "tool": tc.name, "result": text[:2000],
                                                   "duration_ms": int((time.monotonic() - _t0) * 1000)})
                        return text
                    # MCP 动态工具不在 whitelist 字面量里（单独挂在 extra_specs），按已挂工具放行
                    if tc.name not in self.whitelist and tc.name not in self.extra_specs:
                        text = f"工具 {tc.name} 不在你的权限白名单内"
                        self._emit("tool_result", {"agent_key": self.role, "round": rnd,
                                                   "tool": tc.name, "result": text[:2000],
                                                   "duration_ms": int((time.monotonic() - _t0) * 1000)})
                        return text
                    # 硬超时兜底（「无进展」语义）：工具内部各有自己的单张时限（命令 300s /
                    # 出图 600s / TTS 600s）。批量出图/配音会一张张出、每出一张推进
                    # t.progress_seq；execute_tool_with_timeout 只在「连续这么久毫无进展」
                    # 时才中止，所以十几张正常出图不会被整批误杀，只有真挂死才收。
                    result = await execute_tool_with_timeout(
                        tc.name, tc.arguments, self.tctx, TOOL_HARD_TIMEOUT_S)
                    # 谁先跑完谁先落 tool_result 事件。以前是整批 gather 完才统一发：
                    # 同一轮里「查看目录」（毫秒）和「派发美术」（几分钟）并行时，前者明明早完了
                    # 却在轨迹上一直挂「执行中…」（任务一停，那两条结果就永远没记录，用户实测困惑）。
                    self._emit("tool_result", {
                        "agent_key": self.role, "round": rnd, "tool": tc.name,
                        "result": _tail_with_sources(result.text if isinstance(result, ToolResult)
                                                    else str(result)),
                        "duration_ms": int((time.monotonic() - _t0) * 1000)})
                    return result

            results = await asyncio.gather(*(_run_tool(tc) for tc in calls))
            # 超出上限没执行的调用：补一条明确的失败结果，原序回填，模型下一轮就会分批
            overflow_results = [
                f"工具 {tc.name} 未执行：同一轮调用超过上限 {MAX_TOOL_CALLS_PER_ROUND} 个，请分批继续"
                for tc in overflow]
            for tc in overflow:
                self._emit("tool_result", {"agent_key": self.role, "round": rnd,
                                           "tool": tc.name,
                                           "result": f"未执行：本轮调用超过上限 {MAX_TOOL_CALLS_PER_ROUND} 个"})
            all_calls = calls + overflow
            all_results = list(results) + overflow_results
            shot_images: list[str] = []
            for tc, result in zip(all_calls, all_results):
                if isinstance(result, ToolResult):
                    text = result.text
                    shot_images.extend(result.images)
                else:
                    text = str(result)
                # 记账：真实落地动作（改文件/出素材/验证），供空收尾时生成事实清单
                self._note_trace(tc.name, tc.arguments, text)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
            # 标记让上下文在模型回复后移除像素，文件路径仍在工具结果中。
            if shot_images:
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            f"{VIEWBACK_MARK}工具请求的图片或合并预览已附在本条消息中。"
                            "请按工具返回的路径及格号核对画面，把观察结论写进正文；"
                            "这些预览不一定是页面截图，不能据此声称页面已验收。")},
                        *[{"type": "image_url", "image_url": {"url": u}} for u in shot_images],
                    ],
                })
            # 汇报即收尾（关键）：report_to_user 以前只是个「把正文存起来」的普通工具，
            # 存完循环继续，模型可以接着读文件/跑命令——用户看到轨迹上「✓汇报」下面还
            # 跟着一大串动作，自然会问「已汇报？然后下面继续的？」。而且 orchestrator
            # 收尾优先用 _summaries 里这段较早的汇报当 final_summary，后面做的事根本没被
            # 写进总结。现在把汇报当成交卷信号：
            #   · 清单已无未完成项 → 直接以这段汇报结束本轮（或交给 _final_text 兜底）；
            #   · 清单还挂着未完成项 → 退化为「阶段汇报」，提示它继续做，做完再汇报一次。
            if self.role == "main":
                reported = next(
                    (tc for tc in resp.tool_calls if tc.name == "report_to_user"), None)
                if reported is not None:
                    # 工具层的失败结果不会被当成汇报：只有「已汇报给用户」才算数
                    rep_text = ""
                    for tc, r in zip(all_calls, all_results):
                        if tc is reported:
                            rep_text = (r.text if isinstance(r, ToolResult) else str(r))
                    ok = rep_text.startswith("已汇报")
                    if ok:
                        open_items = await self._open_plan_items() \
                            if "update_plan" in self.whitelist else []
                        if open_items and stage_reports < MAX_PLAN_NUDGES:
                            stage_reports += 1
                            listed = "；".join(
                                f"{i}. {str(it.get('content') or '')[:40]}"
                                for i, it in enumerate(open_items, 1))[:600]
                            log.info("stage report with %s open plan items, continue",
                                     len(open_items),
                                     extra={"run_id": str(self.tctx.run_ctx.run_id)})
                            # 阶段汇报也要像最终汇报那样在会话流里出一份完整的 Markdown 报告
                            # （此前只在轨迹里留一行被截断的纯文本，用户看不到排版后的正文）。
                            # 正文取自本次 report_to_user 入参的 report 字段，落成一条带
                            # meta.kind=stage_report 的 agent 消息；前端照常 Markdown 渲染。
                            stage_body = ""
                            try:
                                a = reported.arguments or {}
                                if isinstance(a, str):
                                    a = json.loads(a)
                                stage_body = str(a.get("report") or "")
                            except Exception:  # noqa: BLE001 - 取不到正文就跳过落消息
                                stage_body = ""
                            if stage_body.strip():
                                await self._post_stage_report(
                                    stage_body, round_no=rnd,
                                    open_count=len(open_items))
                            self._emit("status", {"status": "running",
                                                  "note": f"这是阶段汇报，清单还有 {len(open_items)} 项没做完，继续执行"})
                            messages.append({"role": "user", "content":
                                _NUDGE_STAGE_REPORT.format(n=len(open_items), items=listed)})
                            continue
                        # 清单已完成（或预算用尽）→ 以这段汇报作为本轮最终结论收尾。
                        # 注意不能用工具回执「已汇报给用户。」当正文——那是工具的应答，
                        # 真正写给用户的汇报正文在工具入参 report 里（orchestrator 也已存进
                        # _summaries）。这里取回来当 AgentResult.text，_prefer_summary 才会
                        # 直接用它当 final_summary（而不是被那句回执顶掉）。
                        report_body = ""
                        try:
                            a = reported.arguments or {}
                            if isinstance(a, str):
                                a = json.loads(a)
                            report_body = str(a.get("report") or "")
                        except Exception:  # noqa: BLE001 - 取不到正文就退回模型文字
                            report_body = ""
                        final_text, fallback = self._final_text(report_body or resp.text)
                        log.info("run finalized by report_to_user (open_plan=%s)",
                                 len(open_items),
                                 extra={"run_id": str(self.tctx.run_ctx.run_id)})
                        return AgentResult(text=final_text, usage=total_usage,
                                           fallback=fallback)
            # 死循环检测：同一「工具+参数」连续出现 3 次 → 掐停收尾。
            # 有进展的长任务（每次调用都不同）不受影响，这是唯一的次数相关止损。
            # 参数解析失败的调用**不计入签名**：它们参数是空 {}，连续几次都长得一模一样，
            # 会被误判成「重复调用」把 run 掐停——但它其实是「参数太大被截断」，要的是
            # 纠正写法（改成分块 append），不是收尾。给模型有上限的几次重试机会。
            malformed = [tc for tc in resp.tool_calls if tc.arguments_error]
            if malformed and len(malformed) == len(resp.tool_calls):
                # 整轮都传不完整：不执行（empty args 调了也只会报 '_path_'），直接纠正。
                # 混合轮（有正常调用）不在此列：正常调用照常执行，坏的那条由 _run_tool
                # 单独回一条纠正结果。
                arg_retries += 1
                if arg_retries <= MAX_ARG_RETRIES:
                    first = malformed[0]
                    log.warning("tool args unparseable (%s) round %s, nudge retry %s/%s: %s",
                                first.name, rnd, arg_retries, MAX_ARG_RETRIES,
                                first.arguments_error,
                                extra={"run_id": str(self.tctx.run_ctx.run_id)})
                    self._emit("status", {"status": "running",
                                          "note": f"{first.name} 的参数没传完整，已要求换个写法重做"})
                    messages.append({"role": "user",
                                     "content": _ARG_RETRY_HINT.format(err=first.arguments_error)})
                    continue
                # 反复几次都传不完整：别无限重试，落到收尾让模型如实说明（不假装写成了）
                log.error("tool args still unparseable after %s retries, finalizing",
                          arg_retries, extra={"run_id": str(self.tctx.run_ctx.run_id)})
                self._emit("status", {"status": "running",
                                      "note": "工具参数多次传不完整，已停止重试并收尾"})
                messages.append({"role": "user", "content":
                    "（系统提示）工具参数连续多次没能完整传输（多为一次内容太长被截断）。"
                    "请停止重试，基于已有信息如实给出当前结论，并说明哪部分还没落盘。"})
                break
            arg_retries = 0  # 这一轮参数都正常：把截断重试额度清零（长任务里偶发两次不该累计）
            for tc in resp.tool_calls:
                if tc.arguments_error:  # 混合轮里的坏调用：不参与死循环签名
                    continue
                recent_sigs.append(_call_signature(tc.name, tc.arguments))
            if len(recent_sigs) >= 3 and recent_sigs[-1] == recent_sigs[-2] == recent_sigs[-3]:
                log.info("loop detected (3x identical tool call), finalizing early",
                         extra={"run_id": self.tctx.run_ctx.run_id})
                self._emit("status", {"status": "running",
                                      "note": "检测到重复的调用，已掐停并收尾"})
                messages.append({"role": "user", "content":
                    "（系统提示）检测到你连续重复执行完全相同的工具调用，已陷入循环。"
                    "请停止重复，换一种方法，或基于已有信息直接给出最终结论。"})
                break
            # 「只排查不动手」刹车：只看代码/写探针脚本不算进展。见 EXPLORE_BRAKE_AT 注释。
            # analyze 模式用户明确只要分析，不该被逼着改代码；出图/配音/跑批也不是修 bug。
            if self.role == "main" and self.tctx.run_ctx.run_type != "analyze" \
                    and _is_fix_task(self.instruction):
                if _landed_in_round(resp.tool_calls):
                    explore_calls = 0
                    explore_brakes = 0
                else:
                    # 看图不是「勘察」：美术/页面验收本来就要一轮轮对照图，把它算进
                    # 只读调用里，一个「6 角色 × 6 资产」的验收任务必然撞上 90 次硬停
                    # （实测 kpgame 2026-10-29：强制收尾时人还在验图，结论没写完）。
                    explore_calls += _explore_weight([tc.name for tc in resp.tool_calls])
                if explore_calls >= EXPLORE_HARD_STOP:
                    log.warning("exploration-only hard stop after %s calls", explore_calls,
                                extra={"run_id": str(self.tctx.run_ctx.run_id)})
                    self._emit("status", {"status": "running",
                                          "note": f"排查 {explore_calls} 次仍无改动，已强制收尾"})
                    messages.append({"role": "user",
                                     "content": _HARD_STOP_MSG.format(n=explore_calls)})
                    break
                next_brake = EXPLORE_BRAKE_AT + explore_brakes * EXPLORE_BRAKE_STEP
                if explore_brakes < EXPLORE_BRAKE_MAX and explore_calls >= next_brake:
                    explore_brakes += 1
                    log.info("exploration brake #%s after %s calls", explore_brakes, explore_calls,
                             extra={"run_id": str(self.tctx.run_ctx.run_id)})
                    self._emit("status", {"status": "running",
                                          "note": f"已排查 {explore_calls} 次仍无改动，要求直接动手修"})
                    messages.append({"role": "user", "content": _BRAKE_MSG.format(n=explore_calls)})
            # D 收敛守卫：上面的刹车只管「修 bug」类需求（_is_fix_task），创建/生成类
            # 完全落在它外面——实测「新建两个 ur 角色」连着 84 次往返只读不写，
            # 12 分多钟一张图都没出，全程没有任何东西拦它。
            # 这段只催不停：任务没有次数上限，掐停只有停止/写锁/死循环三种。
            elif self.role == "main" and self.tctx.run_ctx.run_type != "analyze":
                committed = any(tc.name in ("update_plan", "ask_user", "report_to_user")
                                for tc in resp.tool_calls)
                if _landed_in_round(resp.tool_calls):
                    probe_rounds = 0
                    probe_nudges = 0
                elif committed:
                    probe_rounds = 0  # 立了清单/问了人：这段勘察算有了交代，重新数
                else:
                    probe_rounds += 1
                if probe_nudges < CONVERGE_NUDGE_MAX \
                        and probe_rounds >= CONVERGE_NUDGE_AT + probe_nudges * CONVERGE_NUDGE_STEP:
                    probe_nudges += 1
                    content = (_CONVERGE_PLAN_MSG if probe_nudges == 1 else _CONVERGE_WRITE_MSG
                               ).format(n=probe_rounds)
                    log.info("converge nudge #%s after %s read-only calls",
                             probe_nudges, probe_rounds,
                             extra={"run_id": str(self.tctx.run_ctx.run_id)})
                    self._emit("status", {"status": "running",
                                          "note": f"连着 {probe_rounds} 次只读没落地，"
                                                  f"已要求"
                                                  f"{'先给结论并列清单' if probe_nudges == 1 else '直接产出第一版'}"})
                    messages.append({"role": "user", "content": content})
            # 看图不落字守卫：与上面两类分开判——美术验收既不是「修 bug 的只读勘察」，
            # 也不该被催着立清单，它缺的是「把看过什么写下来」。
            if self.role == "main" and self.tctx.run_ctx.run_type != "analyze":
                names = [tc.name for tc in resp.tool_calls]
                if names and all(n in _VIEW_TOOL_NAMES for n in names):
                    view_rounds += 1
                else:
                    view_rounds = 0
                if view_rounds >= VIEW_NUDGE_AT and view_nudges < VIEW_NUDGE_MAX:
                    view_nudges += 1
                    view_rounds = 0
                    log.info("view-without-verdict nudge #%s", view_nudges,
                             extra={"run_id": str(self.tctx.run_ctx.run_id)})
                    self._emit("status", {"status": "running",
                                          "note": "连着 3 轮只看图没留结论，已要求写成 "
                                                  ".trace/art-review.md"})
                    messages.append({"role": "user", "content":
                                     _VIEW_VERDICT_MSG.format(n=VIEW_NUDGE_AT)})
        # 只有上面死循环 break 才会走到这里（正常任务是模型自己交卷后 return）。
        # 措辞刻意避开「本阶段/信息收集到此为止」这类说法：实测模型会照着它交回一份
        # 「本轮汇报（截至信息收集与执行阶段结束）」+「四、下一步」，把半成品当交付。
        messages.append({"role": "user", "content":
            "（系统提示）请立即基于已获取的信息给出当前成果，不要再调用任何工具。"
            "如实汇报：做完的列清楚，没做完的还剩多少、卡在哪一步。"
            "不要声称已完成未完成的工作，也不要把剩下的活儿写成一份给用户看的操作指南——"
            "剩下的由系统自动接着做。"})
        self.ctx.llm_round = -1  # -1 = 掐停后的强制收尾调用（上下文视图里据此区分）
        resp: LLMResponse = await self._chat_with_heartbeat(
            messages, [], label="模型收尾",
            on_delta=self._make_thinking_streamer(-1))
        await self._record_llm_call(resp)
        if resp.reasoning:
            self._emit("agent_thinking", {"agent_key": self.role, "round": -1,
                                          "reasoning": _tail_keep_head(resp.reasoning, 4000)})
        total_usage["prompt_tokens"] += resp.usage.get("prompt_tokens", 0)
        total_usage["completion_tokens"] += resp.usage.get("completion_tokens", 0)
        total_usage["cached_tokens"] += int(resp.usage.get("cached_tokens") or 0)
        total_usage["latency_ms"] += resp.latency_ms
        total_usage["calls"] += 1
        final_text, fallback = self._final_text(resp.text)
        return AgentResult(text=final_text, usage=total_usage, fallback=fallback)
