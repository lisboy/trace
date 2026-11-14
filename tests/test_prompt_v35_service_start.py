"""出厂词 v3.5：把「起服务验收」降级成重手段（2026-11-14 用户口径）。

用户原话：「把起服务验收这个优先级调低，除非是用户说了或者是多轮对话都没修改好问题才执行这个操作」。
锁两件事：
1. 默认提示词里默认只做静态验证，起服务 + 截图只在「用户明确要看效果」或「同一问题多轮没改好」时用；
2. 存量库升级只替换那几段出厂旧文案，管理员自定义过的正文一个字都不动。
"""
from __future__ import annotations

from game_agent.agents.roles import ROLE_PROMPTS
from game_agent.db.migrate import (
    _V35_OLD_EVIDENCE, _V35_OLD_SELFTEST_V30, _V35_OLD_SELFTEST_V34,
    _V35_OLD_UNITTEST, _apply_v35_pairs, _v35_prompt_pairs,
)


def test_default_prompt_lowers_service_start() -> None:
    newp = ROLE_PROMPTS["main"]
    # 新口径：默认不起服务，静态验证过关就收工
    assert "默认不起服务，静态验证过关就收工" in newp
    # 两个例外写死，模型才照做
    assert "用户明确要看效果" in newp
    assert "多轮没改好" in newp
    # 旧的「必须看真实页面效果才起服务」那种默认口径不能再出现
    assert "必须看真实页面效果" not in newp


def test_pairs_are_derived_from_current_default() -> None:
    pairs = _v35_prompt_pairs()
    # v3.4 自测段 / v3.0 自测段（缩进 + 不缩进两变体）/ 单测证据行 / 质量底线证据行
    assert len(pairs) == 5
    newp = ROLE_PROMPTS["main"]
    for old_b, new_b in pairs:
        assert old_b not in newp, "旧段不该还留在默认提示词里"
        assert new_b in newp, "新段必须来自当前默认提示词"
        assert new_b != old_b


def test_upgrade_rewrites_stock_prompt() -> None:
    """v3.4 / v3.0（缩进与不缩进）三代出厂文案都能升到新口径。"""
    newp = ROLE_PROMPTS["main"]
    v30_variants = (_V35_OLD_SELFTEST_V30, _V35_OLD_SELFTEST_V30.replace("\n   ", "\n"))
    for old_block in (_V35_OLD_SELFTEST_V34, *v30_variants):
        stock = (
            "你是本项目唯一对用户负责的助手。\n\n"
            "═══ 二、写代码类任务的节奏 ═══\n"
            f"{old_block}\n"
            f"{_V35_OLD_UNITTEST}\n"
            "6. 和素材相关：代码里引用真实路径。\n\n"
            "═══ 三、质量底线 ═══\n"
            "1) 产物真实存在；\n"
            f"{_V35_OLD_EVIDENCE}\n"
            "3) 汇报能一句话说清。\n"
        )
        out = _apply_v35_pairs(stock, _v35_prompt_pairs())
        assert out != stock
        assert "默认不起服务，静态验证过关就收工" in out
        assert "起服务自测" not in out and "先判断这一步需不需要起服务" not in out
        assert "服务跑起来 + 截图" not in out
        assert "服务真的起来并看过截图" not in out
        # 别的章节不受影响
        assert "你是本项目唯一对用户负责的助手。" in out
        assert "6. 和素材相关：代码里引用真实路径。" in out
        # 新默认词里的这两段确实落进去了
        for _old, new_b in _v35_prompt_pairs():
            assert new_b in out
        assert newp.count("默认不起服务") >= 1


def test_upgrade_leaves_custom_prompt_untouched() -> None:
    """管理员自己写的口径（没有出厂旧段）→ 一个字都不改，交回原样。"""
    custom = (
        "你是本项目助手。本团队规定：每次改完必须起服务截图，并且用 pnpm 不用 npm。\n"
        "4. 自测与验收：改完起服务 + 截图，截图存档到 docs/shots/。\n"
        "5. 有单测就跑单测。\n"
    )
    assert _apply_v35_pairs(custom, _v35_prompt_pairs()) == custom


def test_upgrade_is_idempotent_on_new_prompt() -> None:
    newp = ROLE_PROMPTS["main"]
    assert _apply_v35_pairs(newp, _v35_prompt_pairs()) == newp
