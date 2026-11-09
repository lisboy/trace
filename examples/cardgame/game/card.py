"""最小卡牌数据模型（示例项目，供 循迹（Trace） 练手）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Rarity(str, Enum):
    COMMON = "common"
    RARE = "rare"
    EPIC = "epic"
    LEGENDARY = "legendary"


RARITY_COLORS: dict[Rarity, str] = {
    Rarity.COMMON: "#9aa0a6",
    Rarity.RARE: "#3b82f6",
    Rarity.EPIC: "#a855f7",
    Rarity.LEGENDARY: "#f59e0b",
}


@dataclass
class Card:
    name: str
    rarity: Rarity
    attack: int = 0
    health: int = 0
    skill: str = ""
    tags: list[str] = field(default_factory=list)

    def color(self) -> str:
        return RARITY_COLORS[self.rarity]


def sort_by_rarity(cards: list[Card]) -> list[Card]:
    order = {Rarity.LEGENDARY: 0, Rarity.EPIC: 1, Rarity.RARE: 2, Rarity.COMMON: 3}
    return sorted(cards, key=lambda c: order[c.rarity])
