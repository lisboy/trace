"""带图插话的回归测试（2026-10-29）。

现场：用户排队了一条「翻牌失败了…（图片 ×1）」，点「插话」直接被拦——
「带图片/附件的消息没法插话，等这轮结束再发」。原因是插话通道只收字符串
（orchestration/steering 的内存邮箱），图片根本没有入口；而截图往往正是插话最有用
的形式。这里钉死：插话能带图、图进的是 user 消息而不是会过期的视觉回灌。
"""
from __future__ import annotations

import pytest

from game_agent.agents.runtime import _steer_message
from game_agent.orchestration import steering


IMG = "data:image/jpeg;base64,/9j/AAAA"


def test_steer_mailbox_carries_text_and_images() -> None:
    steering.clear("run-1")
    steering.push("run-1", "翻牌失败了", [IMG])
    notes = steering.drain("run-1")
    assert notes == [{"text": "翻牌失败了", "images": [IMG]}]
    assert steering.pending("run-1") == 0  # drain 取走


def test_steer_mailbox_allows_image_only() -> None:
    steering.clear("run-2")
    steering.push("run-2", "", [IMG])
    assert steering.drain("run-2") == [{"text": "", "images": [IMG]}]


def test_steer_mailbox_ignores_empty() -> None:
    steering.clear("run-3")
    steering.push("run-3", "   ", [])
    steering.push("run-3", "", ["  "])
    assert steering.drain("run-3") == []


def test_steer_message_becomes_vision_parts() -> None:
    msg = _steer_message({"text": "翻牌失败，长这样", "images": [IMG]})
    parts = msg["content"]
    assert msg["role"] == "user"
    assert isinstance(parts, list) and len(parts) == 2
    assert parts[0]["type"] == "text" and "翻牌失败，长这样" in parts[0]["text"]
    assert parts[0]["text"].startswith("（用户在你执行过程中插话")
    assert parts[1] == {"type": "image_url", "image_url": {"url": IMG}}


def test_steer_message_plain_text_stays_a_string() -> None:
    msg = _steer_message({"text": "改成 A 方案", "images": []})
    assert isinstance(msg["content"], str) and "改成 A 方案" in msg["content"]


@pytest.mark.parametrize("images", [None, [], [""], ["  "]])
def test_steer_message_no_images(images) -> None:
    msg = _steer_message({"text": "x", "images": images})
    assert isinstance(msg["content"], str)


# ---- 运行中切换模型（2026-11-01）：和插话共用邮箱，但不变成一条空白 user 消息 ----

def test_model_switch_only_does_not_add_chat_message() -> None:
    steering.clear("run-m1")
    steering.push("run-m1", "", [], model={"profile_key": "p", "effort": ""})
    assert steering.drain("run-m1") == []  # 纯切模型不该冒出插话消息
    assert steering.take_model_switch("run-m1") == {"profile_key": "p", "effort": ""}


def test_model_switch_with_text_keeps_both() -> None:
    steering.clear("run-m2")
    steering.push("run-m2", "换个模型继续", [], model={"profile_key": "p2", "effort": "high"})
    assert steering.drain("run-m2") == [{"text": "换个模型继续", "images": [],
                                         "model": {"profile_key": "p2", "effort": "high"}}]
    assert steering.take_model_switch("run-m2") == {"profile_key": "p2", "effort": "high"}


def test_model_switch_taken_once() -> None:
    steering.clear("run-m3")
    steering.push("run-m3", "", [], model={"profile_key": "p", "effort": ""})
    assert steering.take_model_switch("run-m3") == {"profile_key": "p", "effort": ""}
    assert steering.take_model_switch("run-m3") is None  # 同一次选择只生效一次


def test_model_switch_last_wins_and_empty_means_follow_default() -> None:
    steering.clear("run-m4")
    steering.push("run-m4", "", [], model={"profile_key": "a", "effort": ""})
    steering.push("run-m4", "", [], model={"profile_key": "", "effort": "low"})
    # 连点几个模型只该用最后一次；空 profile_key = 回到跟随角色默认
    assert steering.take_model_switch("run-m4") == {"profile_key": "", "effort": "low"}


def test_clear_drops_model_switch() -> None:
    steering.clear("run-m5")
    steering.push("run-m5", "", [], model={"profile_key": "p", "effort": ""})
    steering.clear("run-m5")
    assert steering.take_model_switch("run-m5") is None
