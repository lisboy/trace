"""「要不要动手」的最终裁决在模型自己手里——回归测试（2026-11-14 改）。

历史：这里曾经钉的是意图关键词层（_classify_by_keyword）的「要方案 ≠ 要动手」规则，
因为当时由系统预判意图。现在预判已停用（见 tests/test_intent_inspect.py），
**判断权回到模型**：系统只要求主提示词里把四类任务的口径讲清楚，
让模型自己能区分「只回答」和「检查完落地改」。

本文钉的是这条前提还在，避免有人把那段口径删掉后，模型又变成见到什么都要改文件。
"""
from __future__ import annotations


def test_main_prompt_lets_model_choose_act_or_answer() -> None:
    """主提示词必须讲清四类口径（要改 / 要排查 / 排查后改 / 只回答）。"""
    from game_agent.agents.roles import ROLE_PROMPTS

    main = ROLE_PROMPTS["main"]
    # A 纯问答/调研 → 只回答不改文件
    assert "纯问答/调研" in main and "只回答" in main
    # B 排查类 → 只定位给结论
    assert "排查" in main
    # 一句话里既有排查又有修复 → 按修复处理（检查是为了改）
    assert "按「修复」处理" in main


def test_main_prompt_has_shortest_path_for_survey() -> None:
    """纯问答/调研要给最短路径——否则会把仓库翻一遍再回答。

    实测 2026-11-14 02:06「看看这个项目是干嘛的」：8 轮、16 次工具调用、
    README 按 4 个区间读了 4 遍。
    """
    from game_agent.agents.roles import ROLE_PROMPTS

    main = ROLE_PROMPTS["main"]
    assert "最短路径" in main
    assert "同一个文件不许换着 offset 反复读" in main
    # 调研类汇报别被六段模板绑住
    assert "纯调研/分析类任务" in main
