"""卡牌游戏命令行入口（最小可用）。"""
from __future__ import annotations

from game.card import Card, Rarity, sort_by_rarity


def main() -> None:
    cards = [
        Card("铁卫", Rarity.COMMON, attack=2, health=5),
        Card("寒霜法师", Rarity.RARE, attack=4, health=3, skill="冰冻"),
        Card("龙后", Rarity.LEGENDARY, attack=9, health=9, skill="龙息"),
    ]
    for c in sort_by_rarity(cards):
        print(f"[{c.rarity.value}] {c.name} ATK={c.attack} HP={c.health} {c.skill} ({c.color()})")


if __name__ == "__main__":
    main()
