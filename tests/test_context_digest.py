"""上下文压缩「摘要段」的回归测试（2026-10-30）。

对着 kpgame run 3292305e 这次真实压缩的四个缺口：
1. 52,778 tokens 的被压历史只有 29,145 字符（12,822 tokens）进了摘要器，其余 76%
   只留下一句「因长度上限省略」——最早的目标与决策直接蒸发。现在分块、旧→新全覆盖。
2. 摘要模型把「压缩历史」理解成「把历史里的事做完」，返回一张 656 字的验收结论表，
   代码只校验了长度（≥250 tokens）就当成摘要写回。现在按骨架校验，不合格重问一次。
3. 上一轮的摘要在下一次压缩时根本没进模型输入（只在兜底路径拼回去），压两次就丢。
   现在旧摘要作为第一块的 prior。
4. 摘要器按「最新在前」序列化，而增量判断是 startswith —— 永远命中不了。
   现在不走那条路：逐块并入。
"""
from __future__ import annotations

import asyncio

import pytest

from game_agent.llm import context_window as cw
from game_agent.llm.context_window import DIGEST_MARK, compress_messages

GOOD_DIGEST = (
    "## 目标\n把六名角色（藤冢夫人/阿汐/褚星阑/青枢/宋知微/辛夷）的立绘重新生成，"
    "要求六个角色动作各不相同，并把新图接进 content/art 与 data/characters。\n"
    "## 已完成（带路径/命令/产物）\n"
    "- 六张 1024x1536 全身立绘已出：assets/art/character/{teng-boss,ax,zxl,qs,szw,xy}-portrait-v3_v1.png\n"
    "- 已复制进 content/art/<角色名>-<id>/全身立绘.png，并跑过 node tools/build.mjs（88 个角色目录，出图资产 616/616）。\n"
    "- 已登记 content/art/ledger.json，并跑过 node tools/render-cards.mjs（135 张卡面）。\n"
    "## 关键事实与数值\n"
    "- 每张 1024x1536；文件大小 1.94MB–2.89MB；md5 逐张与源图 SAME。\n"
    "- 姿态 alt：坐礁石听贝／背身仰头看星／蹲地排铜片／坐廊下提笔回头／石阶跃下／单足点藤网。\n"
    "## 未完成 / 下一步\n"
    "- standee（抠图立绘）与 dynamic（动态立绘）仍是旧图，需要按新 portrait 同动作补出。\n"
    "## 坑与禁令\n"
    "- assets/art/character/*-portrait_v1.png 里混有真人照片与铅笔速写，不可作画风基准。\n"
    "- 不要重出已经落地的六张 portrait。\n"
)


class FakeSummarizer:
    def __init__(self, replies: list[str] | None = None) -> None:
        self.calls: list[dict] = []
        self.replies = list(replies or [])

    async def __call__(self, text: str, target_tokens: int = 0,
                       prior: str = "", strict: bool = False) -> str:
        self.calls.append({"text": text, "target": target_tokens,
                           "prior": prior, "strict": strict})
        if self.replies:
            return self.replies.pop(0)
        return GOOD_DIGEST


def _history(n: int = 40, pad: int = 120) -> list[dict]:
    msgs = [{"role": "system", "content": "系统提示"},
            {"role": "user", "content": "把六张立绘重新出了，动作不要一样"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "run_command",
                                                  "arguments": '{"command": "dir"}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                     "content": f"MARK{i:02d} " + "内容" * pad})
    return msgs


def _run(coro, timeout: float = 60):
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


def _digest_msg(out: list[dict]) -> str:
    m = next(m for m in out if str(m.get("content", "")).startswith(DIGEST_MARK))
    return str(m["content"])


# ---------------------------------------------------------------- 1. 覆盖率
def test_every_evicted_round_reaches_the_summarizer(monkeypatch) -> None:
    """最早被挤出去的那一条也必须进过摘要器（以前 30k 字符一截，最早的全丢）。"""
    monkeypatch.setattr(cw, "SERIAL_CHARS_CHUNK", 800)
    monkeypatch.setattr(cw, "SUMMARY_MAX_CHUNKS", 80)
    fake = FakeSummarizer()
    rep: dict = {}
    out = _run(compress_messages(_history(40), 2000, fake, rep, tools=None))
    seen = "\n".join(c["text"] for c in fake.calls)
    assert "MARK00" in seen, "最早一条没进摘要器"
    assert rep["chunks"] > 1 and rep["chunks_dropped"] == 0
    assert rep["digest_ok"] is True
    assert "分块合并而成" in _digest_msg(out)


def test_chunks_keep_chronological_order() -> None:
    lines = [f"L{i}" for i in range(10)]
    chunks = cw._split_chunks(lines, 12)
    assert [ln for c in chunks for ln in c.split("\n")] == lines


# ---------------------------------------------------------------- 2. 骨架校验
def test_answer_instead_of_summary_is_retried_then_used() -> None:
    """模型把「压缩历史」答成了任务：第一次不合格 → 带 strict 重问一次。"""
    fake = FakeSummarizer(replies=[
        "已按联系表格号核对了这六张正式位全身立绘，动作互不相同，全部合格。",
        GOOD_DIGEST,
    ])
    rep: dict = {}
    out = _run(compress_messages(_history(20), 2000, fake, rep, tools=None))
    assert len(fake.calls) >= 2
    assert fake.calls[1]["strict"] is True
    assert rep["digest_ok"] is True
    assert "## 未完成" in _digest_msg(out)


def test_digest_that_never_follows_the_skeleton_falls_back() -> None:
    fake = FakeSummarizer(replies=["答非所问一", "答非所问二"])
    rep: dict = {}
    out = _run(compress_messages(_history(20), 2000, fake, rep, tools=None))
    assert rep["digest_ok"] is False
    assert "按原序列出要点" in _digest_msg(out)


# ---------------------------------------------------------------- 3. 旧摘要并入
def test_old_digest_is_passed_as_prior() -> None:
    msgs = _history(30)
    old = (f"{DIGEST_MARK} 早前摘要\n\n## 目标\n旧目标：补齐四件套资产\n"
           "## 已完成\n- 已出 portrait ×6\n## 关键事实\n- 616/616\n"
           "## 未完成\n- standee\n## 坑与禁令\n- 别重出 portrait")
    msgs.insert(0, {"role": "system", "content": old})
    fake = FakeSummarizer()
    out = _run(compress_messages(msgs, 2000, fake, {}, tools=None))
    assert fake.calls, "没有调用摘要器"
    assert "旧目标：补齐四件套资产" in fake.calls[0]["prior"]
    # 旧摘要不能再原样留在消息里（否则摘要一轮叠一轮）
    assert not any(m.get("content") == old for m in out)


# ---------------------------------------------------------------- 4. 用户补充保留
def test_latest_user_message_is_pinned_verbatim() -> None:
    msgs = _history(40)
    steer = {"role": "user", "content": "补充：旧的 v1 图不要删，留着对比"}
    msgs.insert(12, steer)  # 夹在很早的历史里，正常会被压掉
    rep: dict = {}
    out = _run(compress_messages(msgs, 2000, FakeSummarizer(), rep, tools=None))
    assert rep.get("pinned") == 1
    assert any(m is steer or m.get("content") == steer["content"] for m in out)
    assert "用户补充/插话的 1 条消息也保留原文" in _digest_msg(out)


# ---------------------------------------------------------------- 5. 清单硬事实
def test_plan_facts_are_injected_only_when_compacting() -> None:
    calls = {"n": 0}

    async def provider() -> str:
        calls["n"] += 1
        return "待办清单（未完成）：\n1. [进行中] 生成 12 张 avatar+card"

    _run(compress_messages(_history(3), 128_000, FakeSummarizer(), {}, tools=None,
                           facts_provider=provider))
    assert calls["n"] == 0, "没触发压缩也去读了清单"

    out = _run(compress_messages(_history(30), 2000, FakeSummarizer(), {}, tools=None,
                                 facts_provider=provider))
    assert calls["n"] == 1
    body = _digest_msg(out)
    assert "当前状态（系统直读，优先于摘要）" in body
    assert "生成 12 张 avatar+card" in body
    assert body.index("生成 12 张 avatar+card") < body.index("## 目标")


# ---------------------------------------------------------------- 6. 兜底不静默
def test_fallback_lists_recent_points_without_silent_loss() -> None:
    out = _run(compress_messages(_history(30), 2000, None, {}, tools=None))
    body = _digest_msg(out)
    assert "以下较早消息未自动摘要" in body
    assert "MARK" in body  # 要点来自真实历史，而不是一句「暂无可用摘要」
