"""运行中「插话」邮箱：把用户补充的一句话送进正在跑的这一轮，而不是取消重开。

背景（用户实测反馈）：「我是插话，不是取消」——以前前端「立即发送」的语义是
先 POST /runs/{id}/cancel 再新开一个 run，界面上立刻掉一句
「已按你的要求取消本次执行」，组长还得从零把已经探过的路重走一遍。

现在：插话只是往这轮的对话里追加一条 user 消息（下一轮 LLM 调用前由 runtime 取走），
当前进度、工具结果、计划全部保留，组长接着往下做。

2026-10-29 补：插话也能带图。以前这里只存字符串，带图片的消息在前端就被拦成
「没法插话，等这轮结束再发」——而截图往往正是插话最有用的形式（「翻牌失败，
就是这样」）。现在一条插话 = {text, images}，由 runtime 拼成带 image parts 的
user 消息；图片和普通附图一样留在后续轮次里（不做视觉回灌的过期处理）。
"""
from __future__ import annotations

from collections import defaultdict

_boxes: dict[str, list[dict]] = defaultdict(list)
# 运行中「切换模型」：和插话走同一条邮箱（同一个 run 的活），但语义不是「多说一句话」，
# 而是「下一轮 LLM 调用换个大脑」。单独存一份，runtime 每轮开头先应用再清空。
# 存的是「用户意图」{profile_key, effort}：空串表示显式清掉（回到跟随角色默认），
# None 表示这条通道没设过。真正落到哪份档位由 runtime 用 _load_profile 解析——
# 不能让前端塞完整 profile（含 key），否则等于让浏览器指定用哪个端点。
_model_switches: dict[str, dict] = {}

# 单条子agent 的插话邮箱（按 task_id 分）：主agent 一条插话到达后，由大模型判断
# 「要不要同步给哪些正在跑的子agent」，命中的子任务各自从这条邮箱取走（见 interject.py）。
# 和主邮箱分开：子agent 只 drain 自己这个 task 的信箱，绝不抢主agent 的插话。
_task_boxes: dict[str, list[dict]] = defaultdict(list)

# 运行中「改运行选项」：能力开关（自动化测试/联网/出图与配音）、会话权限（仅工作区/全部权限）、
# 仅分析。和模型切换一样走这条邮箱，但语义是「改这一轮的运行约束」：下一轮开头由 runtime
# 取走、重新裁剪工具白名单 / 覆盖 ToolContext.permission_scope / 换系统提示里的约束块。
# 背景（用户实测）：「我中途调整的时候没有生效，都是以最开始的会话来的」——以前这些值只在
# run 开跑时读一次（run.options_json → run_ctx.meta → 白名单/ToolContext），运行期改了不传导。
# 存的是**目标值**（不是增量）：用户连点几次，只该以最后一次的全量为准。
_setting_switches: dict[str, dict] = {}


def push(run_id: str, text: str, images: list[str] | None = None,
         model: dict | None = None) -> None:
    """投递一条插话（只对正在执行中的 run 有意义）。

    文字、图片、模型切换至少有一样。``model`` = {"profile_key": str|None, "effort": str|None}
    （已经过路由层校验/规范化，这里只负责排队）。
    """
    body = (text or "").strip()
    imgs = [str(u) for u in (images or []) if str(u or "").strip()]
    note: dict = {"text": body, "images": imgs}
    if model is not None:
        note["model"] = model
    if not body and not imgs and model is None:
        return
    if model is not None:
        # 最新一次选择覆盖旧的：用户在面板里连点几个模型，只该用最后那个。
        _model_switches[str(run_id)] = dict(model)
    # 纯切模型（没有文字/图）：只排进模型切换通道，不塞进插话邮箱——否则 runtime 会
    # 把它当成一条空白的 user 消息追加进对话。
    if body or imgs:
        _boxes[str(run_id)].append(note)


def drain(run_id: str) -> list[dict]:
    """取走该 run 目前的全部插话（runtime 每轮开头调用一次）。模型切换不在这里取。"""
    return _boxes.pop(str(run_id), [])


def take_model_switch(run_id: str) -> dict | None:
    """取走待应用的模型切换（runtime 每轮 LLM 调用前调用一次）。

    返回 {"profile_key": str|None, "effort": str|None} 或 None（没有待应用的切换）。
    取走即清空：同一次选择只生效一次，避免每轮都重建 LLM。
    """
    return _model_switches.pop(str(run_id), None)


def push_settings(run_id: str, settings: dict) -> None:
    """投递一次「运行选项」变更（能力开关 / 会话权限 / 仅分析）。

    ``settings`` 是目标值全量：``{"caps"?: {test,web,media}, "permission_scope"?: str,
    "analyze"?: bool}``。每次覆盖旧的（最新一次为准），与模型切换同语义。
    """
    if not isinstance(settings, dict) or not settings:
        return
    _setting_switches[str(run_id)] = dict(settings)


def take_settings_switch(run_id: str) -> dict | None:
    """取走待应用的运行选项变更（runtime 每轮开头调用一次）。取走即清空。"""
    return _setting_switches.pop(str(run_id), None)


def pending(run_id: str) -> int:
    return len(_boxes.get(str(run_id), []))


def push_to_task(task_id: str, text: str) -> None:
    """给**单个子agent**投一条补充要求（只有它自己 drain 得到）。"""
    body = (text or "").strip()
    if not body:
        return
    _task_boxes[str(task_id)].append({"text": body})


def drain_task(task_id: str) -> list[dict]:
    """取走某个子agent task 目前累积的插话（子 runtime 每轮开头调用一次）。"""
    return _task_boxes.pop(str(task_id), [])


def clear(run_id: str) -> None:
    _boxes.pop(str(run_id), None)
    _model_switches.pop(str(run_id), None)
    _setting_switches.pop(str(run_id), None)
