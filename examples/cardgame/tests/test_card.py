from game.card import Card, Rarity, RARITY_COLORS, sort_by_rarity


def test_rarity_color():
    assert RARITY_COLORS[Rarity.LEGENDARY] == "#f59e0b"


def test_sort_by_rarity():
    cards = [
        Card("a", Rarity.COMMON),
        Card("b", Rarity.LEGENDARY),
        Card("c", Rarity.RARE),
    ]
    ordered = sort_by_rarity(cards)
    assert ordered[0].name == "b"
    assert ordered[-1].name == "a"


def test_card_color():
    assert Card("x", Rarity.EPIC).color() == "#a855f7"
