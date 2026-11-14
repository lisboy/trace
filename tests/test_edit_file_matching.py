"""edit_file 长编辑命中率的回归测试（2026-11-11 用户截图：文档长段落 old_string 反复未命中）。

模型凭印象重构 old_string 时全角标点/空格/换行有细微出入 → 精确匹配 0 处 →
整段重贴再赌 → 连烧 3-5 轮。修复三层：
1. 空白不敏感匹配：只差缩进/行尾空格/连续空格/全角空格/换行位置时自动命中并替换；
2. 真未命中：错误信息里回传文件里最接近的真实片段 + 行号，模型照抄一轮收敛；
3. 多处命中：错误信息明确教「带上下文」。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.agents.tools.native import (
    _closest_window,
    _ws_fuzzy_match,
    edit_file,
    read_file,
    write_file,
)
from game_agent.core.context import RunContext


def _tool_ctx(tmp_path) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None)  # type: ignore[arg-type]
    return ToolContext(run_ctx=rc, workspace=tmp_path, role="main", task_id="t")  # type: ignore[arg-type]


DOC = (
    "= F-7 | 项目/工程列表查询（`queryItemInfoList` / `queryItemInfoCount`：截图测点 6.5s/3.0s（锚路））\n"
    "| `ItemProjectMapper.xml` 原 SQL 无条件 `left join sxzw_item_project_extend_info` + `left join sxzw_cost_item_info ... is null` 伪\n"
    "反连接，且列表排序无索引；`EXPLAIN` 为 95588 行全表扫描 + Using filesort；加索引后列表已 0.06s，\n"
    "但 count 仍全表扫 95599 行并查列预成本表。\n"
    "\n"
    "结论：COUNT/IDS 全部一致；加密后 list 单页 0.5~0.8s≈约 0.13s（EXPLAIN 由 95588 行 filesort→18 行索引）。\n"
)


def _setup(tmp_path, content: str):
    async def run():
        t = _tool_ctx(tmp_path)
        await write_file({"path": "doc.md", "content": content}, t)
        await read_file({"path": "doc.md"}, t)  # 解锁 edit
        return t
    return asyncio.run(run())


def test_ws_fuzzy_unique_hit(tmp_path) -> None:
    """只差行尾空格/缩进/连续空格/换行位置：唯一命中，返回原文区间。"""
    # 文件里第 3 行行尾多了一个空格，模型贴的时候没有
    text = "alpha line\nsecond line \nthird line\n"
    old = "second line\nthird line"
    span = _ws_fuzzy_match(text, old)
    assert span is not None
    s, e = span
    assert text[s:e] == "second line \nthird line"


def test_ws_fuzzy_fullwidth_space(tmp_path) -> None:
    """全角空格 vs 半角空格：自动命中。"""
    text = "项目  工程列表查询\n后续行\n"
    old = "项目\u3000工程列表查询\n后续行"
    span = _ws_fuzzy_match(text, old)
    assert span is not None


def test_ws_fuzzy_rejects_content_diff(tmp_path) -> None:
    """实质内容不同（措辞/标点差异）：不命中，不能闭眼替换。"""
    text = "锚路测点 6.5s\n"
    old = "瞄路测点 6.5s\n"  # 锚→瞄
    assert _ws_fuzzy_match(text, old) is None


def test_ws_fuzzy_multi_hit_returns_none() -> None:
    """归一化后仍多处命中：返回 None（走精确路径的多处命中分支）。"""
    text = "same line\nmiddle\nsame line\n"
    old = "same line"
    assert _ws_fuzzy_match(text, old) is None


def test_edit_ws_diff_auto_aligned(tmp_path) -> None:
    """端到端：old_string 行尾空格差异 → 自动对齐并成功替换。"""
    t = _setup(tmp_path, "first\nsecond line \nthird\n")
    res = asyncio.run(edit_file(
        {"path": "doc.md", "old_string": "second line\nthird", "new_string": "second line X\nthird"}, t))
    assert "空白差异已自动对齐" in res
    assert "second line X" in (tmp_path / "doc.md").read_text(encoding="utf-8")


def test_edit_miss_returns_closest_window(tmp_path) -> None:
    """端到端：实质不同 → 错误信息里带最接近段落与行号，供照抄重试。"""
    t = _setup(tmp_path, DOC)
    # 模型把「锚路」写成「瞄路」，还漏了一个反引号
    bad_old = "= F-7 | 项目/工程列表查询（`queryItemInfoList` / `queryItemInfoCount`：截图测点 6.5s/3.0s（瞄路））"
    with pytest.raises(ToolError) as ei:
        asyncio.run(edit_file({"path": "doc.md", "old_string": bad_old, "new_string": "x"}, t))
    msg = str(ei.value)
    assert "未命中" in msg
    assert "第 1-" in msg          # 带行号
    assert "queryItemInfoList" in msg  # 带真实内容（照抄即可重试）


def test_edit_multi_hit_teaches_context(tmp_path) -> None:
    """多处命中：错误信息教「前后带 1-2 行上下文」。"""
    t = _setup(tmp_path, "dup line\na\ndup line\n")
    with pytest.raises(ToolError) as ei:
        asyncio.run(edit_file({"path": "doc.md", "old_string": "dup line", "new_string": "x"}, t))
    assert "不唯一" in str(ei.value)


def test_closest_window_low_similarity_empty(tmp_path) -> None:
    """文件里根本没有相似段落：hint 为空，不给模型误导性材料。"""
    out = _closest_window("完全无关的内容\n别的行\n", " totally different words here ")
    assert out == ""
