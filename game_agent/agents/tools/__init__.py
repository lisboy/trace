"""智能体工具层（设计 §4.3 / §7.3）。

两类：
- 原生 fs/shell（read/write/edit/list/glob/grep/run_command）
- 服务与验收（start_service/stop_service/service_logs/screenshot_page）
- 媒体直连（generate_image/generate_audio）——会话模型直接生成图/音频

工具实现层硬校验（不靠 prompt）：白名单、未读不许写、精确替换、路径前缀、fencing。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from game_agent.core.context import RunContext

# 「过程文件」判定：探针/扫描/验证脚本、截图、临时产物都不算产品改动。
# 真实惨案（2026-10-28 run 27a8a2bf）：49 分钟 348 次工具调用、写了 23 个
# target/_probe_*.mjs，产品代码一行没改，却因为 write_file 被算作「已落地」，
# 「只勘察不动手」守卫全程没触发。落地计数必须排除这些路径。
_SCRATCH_DIRS = ("target/", ".trace/tmp/", ".trace/screenshots/", "tmp/", "scratch/")
_SCRATCH_NAME_RE = re.compile(r"(^|/)_(probe|scan|verify|check|ab|tmp|test)[-_]")


def is_scratch_path(path: str) -> bool:
    """工作区相对路径是不是过程文件（不是产品交付物）。"""
    p = str(path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/").startswith(_SCRATCH_DIRS) or bool(_SCRATCH_NAME_RE.search(p))


if TYPE_CHECKING:  # 避免循环导入
    from game_agent.orchestration.orchestrator import Orchestrator


@dataclass
class ToolContext:
    """一次工具调用的上下文。"""
    run_ctx: RunContext
    workspace: Path
    role: str
    task_id: str
    read_set: set[str] = field(default_factory=set)
    # 读到时的指纹 (mtime, size)：写入前比对，防止「拿旧内容覆盖别人刚改过的文件」
    read_stamp: dict[str, tuple[float, int]] = field(default_factory=dict)
    # 本次 run 的工具调用序号（execute_tool 每调一次 +1），view_image 用它判断
    # 「同一张图是不是刚看过」，避免在同一验证循环里反复回灌相似截图。
    call_seq: int = 0
    # 路径 -> 最近一次查看时的 call_seq（或 (call_seq, (mtime_ns,size))）：同图短间隔
    # 重看且文件未变化时，view_image 不再重复回灌像素，改回一句节流提示。
    last_view: dict[str, object] = field(default_factory=dict)
    agent_state: dict[str, Any] = field(default_factory=dict)
    orchestrator: "Orchestrator | None" = None
    python_bin: str | None = None
    # 会话权限作用域：workspace=仅工作区（默认，安全边界）；full=全部权限（可访问工作区外）。
    # 由 run 发起时传入，工具层据此决定 _resolve 是否放行越界路径。
    permission_scope: str = "workspace"
    # load_skill 的搜索根（project → global → 用户自定义，按优先级排序），run_agent 注入
    skill_roots: list[tuple[str, Path]] = field(default_factory=list)
    # 「活进展」计数：批量出图/配音每出完一张(条)就 +1。execute_tool_with_timeout 用它
    # 区分「整批跑很久但一直在出东西」和「真的卡死」——只看总时长会把 11 张各 1-2 分钟
    # 的正常批量误判成超时（实测：正确一张张出图，到第 900s 被整批中止）。
    progress_seq: int = 0
    # spawn_agents 的后台批次（SpawnBatch）：派发即返回，运行时每轮开头把已回收批次的
    # 结论注入对话；主agent 退出时未回收的批次由 orchestrator 统一取消。
    spawn_batches: list[Any] = field(default_factory=list)


class ToolError(Exception):
    """工具执行失败（返回给 LLM，不算系统崩溃）。"""

    def __init__(self, msg: str, fatal: bool = False) -> None:
        super().__init__(msg)
        self.fatal = fatal


@dataclass
class ToolResult:
    """工具返回值：文本 + 可选图片（data-url）。

    图片用于「截图回灌视觉」：组长调 screenshot_page 后，运行时会把这些图
    作为用户消息附带的图像重新送进模型，模型能真的看见页面效果。
    """
    text: str
    images: list[str] = field(default_factory=list)


from .registry import (TOOL_SPECS, build_tool_specs, execute_tool)  # noqa: E402
