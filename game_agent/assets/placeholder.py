"""占位素材生成（设计 §7.8）。

零 key 也能跑通全链路：占位图与真图同路径同名，配好 key 后重画只覆盖内容，代码零改动。
"""
from __future__ import annotations

from pathlib import Path
import struct
import wave

from PIL import Image, ImageDraw, ImageFont

# 默认底色（若画风圣经未提供）
DEFAULT_BG = (43, 58, 103)       # #2B3A67 主色
DEFAULT_FG = (242, 166, 90)      # #F2A65A 强调


def _parse_size(size: str) -> tuple[int, int]:
    try:
        w, h = size.lower().split("x")
        return max(16, int(w)), max(16, int(h))
    except Exception:  # noqa: BLE001
        return 512, 256


def _font(size: int):
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",        # Windows 中文
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for c in candidates:
        p = Path(c)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def generate_placeholder_image(path: str, slug: str, size: str = "512x256",
                               transparent: bool = False, prompt: str = "",
                               bg: tuple[int, int, int] = DEFAULT_BG,
                               fg: tuple[int, int, int] = DEFAULT_FG) -> dict:
    w, h = _parse_size(size)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0) if transparent else (*bg, 255))
    draw = ImageDraw.Draw(img)
    # 描边边框
    draw.rectangle([2, 2, w - 3, h - 3], outline=fg, width=2)
    f_big = _font(max(14, h // 10))
    f_small = _font(max(10, h // 16))
    label = slug
    draw.text((w // 2, h // 2 - h // 8), label, fill=fg, font=f_big, anchor="mm")
    draw.text((w // 2, h // 2 + h // 8), f"{w}x{h}  PLACEHOLDER", fill=fg, font=f_small, anchor="mm")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return {"path": path, "width": w, "height": h, "transparent": transparent}


def generate_silent_wav(path: str, text: str = "", duration_s: float | None = None) -> dict:
    if duration_s is None:
        duration_s = max(0.8, len(text) * 0.25)
    n = int(duration_s * 44100)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        frames = struct.pack("<%dh" % n, *([0] * n))
        wf.writeframes(frames)
    if text:
        Path(path + ".txt").write_text(text, encoding="utf-8")
    return {"path": path, "duration_s": duration_s}
