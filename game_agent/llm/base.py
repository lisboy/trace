"""LLM 适配层基础类型（设计 §2.4 / §7.1）。

统一抽象：messages + tools → text + tool_calls + usage。
真实实现走 OpenAI 兼容 / Anthropic 接口。程序只调用真实大模型（没有 mock 模式）；
测试用的脚本化假大脑见 mock_brain.py。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class LlmAuthError(RuntimeError):
    """LLM 端点认证/额度类错误（HTTP 401/403）：不可重试、不可降级继续，
    必须让 run 直接失败并明确提示用户。"""


class LlmContextOverflow(RuntimeError):
    """端点明说「这次请求超过它的上下文上限」（HTTP 400 + 长度类报错）。

    和参数写错不一样：这是可恢复的——窗口配大了或估算偏乐观时，按更小的预算
    重压一次就能发出去。单独一个类型，适配层才敢只重试这一次而不落入通用重试。
    """


class LlmPayloadTooLarge(RuntimeError):
    """前置网关把请求体拒了（HTTP 413/414）——它看的是**字节数**，不是 token 数。

    一张附图的 base64 在 token 口径里只值 900，字节却是几 MB，所以 token 预算
    管不住它（实测 4.72MB 附图每轮重发）。可恢复：把图片像素剥掉再发一次。
    """


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # 参数 JSON 解析失败的原因（截断 / 裸换行等控制字符 / 非法转义…）。为空 = 解析正常。
    # 以前适配层解析失败会**静默**回退成 arguments={}，模型看到的是「工具错误：'path'」
    # 这种毫无信息量的报错，于是原样重发同一个巨型写入 → 三次相同签名被死循环检测掐停，
    # 最终文档根本没落盘（实测 2026-11-06 山西可研 run：write_file 三连 0ms 失败，模型
    # 改口「文件尚未落盘，这是没做完的部分」）。带上原因，runtime 才能给出可执行的纠正。
    arguments_error: str = ""


def parse_tool_arguments(raw: Any) -> tuple[dict[str, Any], str]:
    """解析工具调用参数的 JSON 字符串 → (args, error)。error 非空表示没解析出来。

    中转/模型吐 parameters 时常带两个毛病，标准 json 直接拒：字符串里有未转义的
    真实换行/制表符（大段文档正文最常见），或输出被长度上限截断（JSON 不完整）。
    - 先按标准解析（有错就换 strict=False 再试一次，放过裸控制字符）；
    - 都失败 → 返回 ({}，原因)，绝不把半截参数当合法参数下发。
    """
    if isinstance(raw, dict):  # 少数端点直接给对象，不按字符串
        return raw, ""
    if not raw or not str(raw).strip():
        return {}, ""
    try:
        v = json.loads(raw)
        return (v if isinstance(v, dict) else {}), (
            "" if isinstance(v, dict) else f"参数不是 JSON 对象（{type(v).__name__}）")
    except json.JSONDecodeError:
        pass
    try:  # 裸换行/制表符：严格模式拒、放宽后能收
        v = json.loads(raw, strict=False)
        if isinstance(v, dict):
            return v, ""
    except json.JSONDecodeError:
        pass
    n = len(raw)
    tail = raw.rstrip()[-1:] if raw else ""
    hint = "（大概率被长度上限截断了，参数没有收尾）" if tail not in ("}", "]") \
        else "（JSON 语法不合法，可能是正文里的引号/换行没转义）"
    return {}, f"工具参数 JSON 解析失败{hint}，共 {n} 字符"


@dataclass
class LLMResponse:
    text: str = ""
    reasoning: str = ""  # 思考过程（OpenAI reasoning_content / DeepSeek-R1 等）
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    # ↑ prompt_tokens / completion_tokens；DeepSeek 风格还会带
    #   prompt_cache_hit_tokens / prompt_cache_miss_tokens（有则透传）
    latency_ms: int = 0  # 本次调用耗时（含重试），用于统计生成速度 tok/s
    # 本次往返的追踪 id：llm_calls.call_id 与 llm_transcripts.call_id 用它互相对齐
    trace_id: str = ""


@dataclass
class Ctx:
    """传给大脑的运行上下文（mock 用；真实 LLM 只用 system prompt）。"""
    role: str = ""
    run_type: str = "modify"  # modify | analyze
    workspace: str = ""       # 项目工作区绝对路径
    requirement: str = ""
    project_name: str = ""
    commands: dict[str, str] = field(default_factory=dict)
    model: str = ""
    payload: dict = field(default_factory=dict)  # 派生子智能体的入参
    images: list[str] = field(default_factory=list)  # 用户随需求附图（dataURL）
    model_profile: dict = field(default_factory=dict)
    # ↑ {"model","base_url","api_key","params":{context_window,vision,...}}，来自 ModelProfile
    llm_round: int = 0  # agent 循环内第几轮（上下文记录用它排序）
    # 每次往返落库前的回调：async def(record: dict) -> None，由 runtime 注入（写 llm_transcripts）。
    # 只有适配层知道「压缩后真正发出去」的 messages，所以钩子放在这里而不是 runtime。
    on_llm_request: Any = None
    # 每次退避重试前的回调：def(attempt, max_attempts, delay_s, error) -> None（同步），
    # 由 runtime 注入（发 llm_retry 轨迹事件）。端点 503/限流这类「会自己恢复」的失败
    # 不该只躺在系统日志里，用户要在运行轨迹里直接看到「正在重试、第几次、等多久」。
    on_llm_retry: Any = None


class LLM(ABC):
    role: str = ""

    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[ToolSpec], ctx: Ctx,
                   on_delta: Optional[Any] = None, summarizer: Optional[Any] = None,
                   *, timeout_s: Optional[int] = None,
                   max_attempts: Optional[int] = None) -> LLMResponse:
        """on_delta(reasoning_delta: str, text_delta: str)：流式增量回调（可选）。
        实现方可忽略该参数走非流式；支持流式的实现逐段回调并返回聚合后的完整 LLMResponse。
        summarizer(text)->str（可选）：压缩历史时的摘要生成器（async），实现方可忽略。
        timeout_s / max_attempts（可选）：按用途收紧单次调用预算（摘要压缩用更短超时、
        更少重试，避免非正文调用卡住时按正文的 5×360s 把整个 run 拖死）。"""
        ...
