"""生成 favicon 的 PNG/ICO 回退（与 public/favicon.svg 同款设计：渐变圆徽 + 白色手柄）。

用法：.venv/Scripts/python scripts/make_favicon.py
输出：frontend/public/favicon-32.png、apple-touch-icon.png、favicon.ico
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "frontend" / "public"
C1, C2 = (47, 125, 223), (25, 194, 168)  # #2f7ddf → #19c2a8
WHITE = (255, 255, 255, 255)


def _gradient_circle(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    for y in range(size):
        t = y / max(1, size - 1)
        for_row = Image.new("RGBA", (size, 1))
        px = [tuple(int(a + (b - a) * t) for a, b in zip(C1, C2)) + (255,)] * size
        for_row.putdata(px)
        grad.paste(for_row, (0, y))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    d.ellipse((1, 1, size - 2, size - 2), outline=(255, 255, 255, 72), width=max(1, size // 42))
    return img


def _badge(size: int) -> Image.Image:
    """按 64 设计稿等比缩放绘制。"""
    s = size / 64.0
    img = _gradient_circle(size)
    d = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1) -> tuple[float, float, float, float]:
        return (x0 * s, y0 * s, x1 * s, y1 * s)

    # 手柄主体
    d.rounded_rectangle(box(14, 24, 50, 41), radius=8.5 * s, fill=WHITE)
    # 十字键
    d.rounded_rectangle(box(19.9, 29.4, 23.5, 38.6), radius=1.3 * s, fill=C1 + (255,))
    d.rounded_rectangle(box(17.1, 32.2, 26.3, 35.8), radius=1.3 * s, fill=C1 + (255,))
    # 按键
    for cx, cy in ((38.4, 30.1), (45.2, 30.1), (41.8, 35.4)):
        r = 2 * s
        d.ellipse((cx * s - r, cy * s - r, cx * s + r, cy * s + r), fill=C1 + (255,))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _badge(32).save(OUT / "favicon-32.png")
    _badge(180).save(OUT / "apple-touch-icon.png")
    _badge(256).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print("written:", OUT)


if __name__ == "__main__":
    main()
