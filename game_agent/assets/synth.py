"""程序化音频合成（音效 / BGM 的真实可听回退实现）。

为什么需要这个模块
------------------
平台原先的 `generate_sfx` / `generate_music` 没有真实供应商：无论 prompt 写什么，
都直接调用 `generate_silent_wav()` 写一段**全零静音 wav**，再登记成 artifact。
用户听到的就是「音频没有声音」。这属于必然事件，不是偶发故障。

这里用纯 Python（标准库 wave + math，无 numpy 依赖）合成真正能听见的声音：
- `synthesize_sfx(prompt)`：按关键词/哈希选择音色轮廓（点击、金币、打击、暴击、
  治疗、胜利、失败、挥砍、魔法、护盾、阶段号角……），产出 0.1~1.2 秒音效；
- `synthesize_music(prompt, duration_s)`：按关键词/哈希选调式与和弦进行，
  叠加 pad、琶音与低音，产出可循环的 BGM 片段。

只在没有外部音效/BGM 供应商时使用；产物会标记 provider="synth"，
与「静音占位」严格区分（placeholder=False）。
"""
from __future__ import annotations

import hashlib
import io
import math
import random
import struct
import wave

SAMPLE_RATE = 22050


def _seed(text: str) -> int:
    return int(hashlib.sha256((text or "sfx").encode("utf-8")).hexdigest()[:16], 16)


def _tone(freq: float, dur: float, amp: float = 0.5, shape: str = "sine",
          sweep_to: float | None = None, attack: float = 0.005,
          release: float = 0.06) -> list[float]:
    """生成一段带起音/释音包络的波形（浮点 -1~1）。"""
    n = max(1, int(dur * SAMPLE_RATE))
    out = [0.0] * n
    phase = 0.0
    for i in range(n):
        t = i / (n - 1) if n > 1 else 1.0
        f = freq if sweep_to is None else freq + (sweep_to - freq) * t
        phase += 2.0 * math.pi * f / SAMPLE_RATE
        if shape == "square":
            v = 1.0 if math.sin(phase) >= 0 else -1.0
        elif shape == "triangle":
            v = (2.0 / math.pi) * math.asin(math.sin(phase))
        elif shape == "saw":
            v = 2.0 * ((phase / (2.0 * math.pi)) % 1.0) - 1.0
        else:
            v = math.sin(phase)
        env = min(1.0, i / max(1.0, attack * SAMPLE_RATE)) if attack > 0 else 1.0
        env *= min(1.0, (n - 1 - i) / max(1.0, release * SAMPLE_RATE)) if release > 0 else 1.0
        out[i] = amp * v * env
    return out


def _noise(dur: float, amp: float = 0.35, seed: int = 1, decay: bool = True) -> list[float]:
    rnd = random.Random(seed)
    n = max(1, int(dur * SAMPLE_RATE))
    out = [0.0] * n
    for i in range(n):
        env = (1.0 - i / n) if decay else 1.0
        out[i] = (rnd.random() * 2.0 - 1.0) * amp * env
    return out


def _add(buf: list[float], start_s: float, samples: list[float], gain: float = 1.0) -> None:
    start = int(max(0.0, start_s) * SAMPLE_RATE)
    end = min(len(buf), start + len(samples))
    for i in range(start, end):
        buf[i] += samples[i - start] * gain


def _mix(*parts: list[float]) -> list[float]:
    n = max((len(p) for p in parts), default=0)
    out = [0.0] * n
    for p in parts:
        for i, v in enumerate(p):
            out[i] += v
    return out


def _to_wav_bytes(buf: list[float]) -> bytes:
    """浮点样本 → 16-bit 单声道 PCM wav 字节；带软削波防爆音。"""
    frames = bytearray()
    for v in buf:
        v = max(-1.0, min(1.0, v * 0.9))
        frames += struct.pack("<h", int(v * 32767))
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(frames))
    return bio.getvalue()


# ---------------------------------------------------------------- 音效
_SFX_PROFILES = (
    ("click", ("click", "tap", "ui", "按钮", "点击", "切换", "tab")),
    ("coin", ("coin", "gold", "金币", "钱", "购买", "shop", "buy")),
    ("hit", ("hit", "attack", "打击", "攻击", "受击", "命中")),
    ("crit", ("crit", "暴击", "会心", "重击")),
    ("heal", ("heal", "治疗", "回复", "回血", "buff")),
    ("death", ("death", "die", "死亡", "倒下", "失败", "defeat")),
    ("victory", ("victory", "win", "胜利", "通关", "升级", "levelup")),
    ("whoosh", ("whoosh", "slash", "挥", "斩", "风", "闪避")),
    ("magic", ("magic", "魔法", "法术", "技能", "spell", "arcane")),
    ("shield", ("shield", "护盾", "格挡", "防御", "guard")),
    ("horn", ("horn", "阶段", "phase", "boss", "号角")),
    ("combo", ("combo", "连击", "consecutive")),
    ("reveal", ("reveal", "翻开", "揭示", "抽卡", "gacha")),
    ("hover", ("hover", "悬停", "轻触")),
)


def _profile(prompt: str) -> str:
    low = (prompt or "").lower()
    for name, keys in _SFX_PROFILES:
        if any(k in low for k in keys):
            return name
    return ("hit", "coin", "magic", "whoosh", "click")[_seed(prompt) % 5]


def synthesize_sfx(prompt: str = "", slug: str = "", duration_s: float | None = None) -> tuple[bytes, dict]:
    """合成一个真实可听的音效，返回 (wav_bytes, info)。"""
    text = f"{prompt} {slug}".strip()
    name = _profile(text)
    seed = _seed(text)
    b = (seed % 300) / 100.0  # 0~3 的音高偏移

    if name == "click":
        buf = _mix(_tone(1500 + b * 120, 0.055, 0.45, "sine", sweep_to=900, release=0.03),
                   _noise(0.012, 0.15, seed, True))
    elif name == "coin":
        buf = _mix(_tone(1318 + b * 40, 0.09, 0.35, "square", release=0.04),
                   _tone(1760 + b * 40, 0.16, 0.4, "square", release=0.12))
    elif name == "hit":
        buf = _mix(_tone(120 + b * 15, 0.16, 0.6, "sine", sweep_to=55, release=0.12),
                   _noise(0.09, 0.4, seed))
    elif name == "crit":
        buf = _mix(_tone(90, 0.22, 0.55, "sine", sweep_to=45, release=0.16),
                   _noise(0.14, 0.5, seed),
                   _tone(2200 + b * 100, 0.28, 0.22, "sine", release=0.2))
    elif name == "heal":
        buf = _mix(_tone(523 + b * 20, 0.5, 0.25, "sine", attack=0.03, release=0.35),
                   _tone(659 + b * 20, 0.5, 0.22, "sine", attack=0.05, release=0.35),
                   _tone(784 + b * 20, 0.5, 0.2, "sine", attack=0.07, release=0.35))
    elif name == "death":
        buf = _tone(440 + b * 30, 0.8, 0.42, "triangle", sweep_to=80, release=0.6)
    elif name == "victory":
        buf = _mix(_tone(523, 0.16, 0.3, "square", release=0.08),
                   _tone(659, 0.16, 0.3, "square", release=0.08),
                   _tone(784, 0.16, 0.3, "square", release=0.08),
                   _tone(1046, 0.42, 0.34, "square", release=0.3))
        # 后三音依次后移
        buf = _mix(_tone(523, 0.16, 0.3, "square", release=0.06),
                   _add_offset(_tone(659, 0.16, 0.3, "square", release=0.06), 0.16),
                   _add_offset(_tone(784, 0.16, 0.3, "square", release=0.06), 0.32),
                   _add_offset(_tone(1046, 0.42, 0.34, "square", release=0.3), 0.48))
    elif name == "whoosh":
        buf = _noise(0.34, 0.35, seed, True)
        # 噪声加音高扫频，听感更像挥砍
        buf = _mix(buf, _tone(400, 0.3, 0.12, "sine", sweep_to=1600, release=0.25))
    elif name == "magic":
        buf = _mix(_tone(880 + b * 50, 0.4, 0.22, "sine", sweep_to=1760, release=0.3),
                   _tone(1320 + b * 50, 0.5, 0.18, "sine", sweep_to=2200, release=0.4),
                   _noise(0.3, 0.06, seed))
    elif name == "shield":
        buf = _mix(_tone(220, 0.3, 0.35, "square", release=0.25),
                   _tone(440, 0.3, 0.2, "triangle", release=0.25),
                   _noise(0.05, 0.1, seed))
    elif name == "horn":
        buf = _mix(_tone(196 + b * 10, 0.75, 0.35, "saw", attack=0.05, release=0.5),
                   _tone(294 + b * 10, 0.75, 0.25, "saw", attack=0.07, release=0.5))
    elif name == "combo":
        buf = _mix(_tone(1568, 0.07, 0.3, "square", release=0.04),
                   _add_offset(_tone(2093, 0.12, 0.33, "square", release=0.08), 0.08))
    elif name == "reveal":
        buf = _tone(600 + b * 30, 0.55, 0.3, "sine", sweep_to=1800, release=0.4)
    elif name == "hover":
        buf = _tone(900 + b * 30, 0.05, 0.18, "sine", release=0.03)
    else:
        buf = _tone(440, 0.25, 0.3, "triangle", release=0.2)

    # 统一限长与淡入淡出，避免爆音
    max_n = int((duration_s or min(1.2, max(0.08, len(buf) / SAMPLE_RATE))) * SAMPLE_RATE)
    if len(buf) > max_n:
        buf = buf[:max_n]
    if not buf:
        buf = [0.0] * int(0.08 * SAMPLE_RATE)
    fade = min(len(buf) // 8, int(0.01 * SAMPLE_RATE))
    for i in range(fade):
        buf[i] *= i / fade
        buf[-1 - i] *= i / fade

    data = _to_wav_bytes(buf)
    info = {"profile": name, "duration_s": round(len(buf) / SAMPLE_RATE, 3), "seed": seed}
    return data, info


def _add_offset(samples: list[float], offset_s: float) -> list[float]:
    """给样本前面补静音，供 _mix 叠加出先后顺序。"""
    pad = int(offset_s * SAMPLE_RATE)
    return [0.0] * pad + samples


# ---------------------------------------------------------------- BGM
_MAJOR = (0, 2, 4, 5, 7, 9, 11)
_MINOR = (0, 2, 3, 5, 7, 8, 10)
_MINOR_KEYS = ("minor", "sad", "dark", "悲伤", "阴暗", "紧张", "战斗", "battle", "boss", "恐怖", "悬疑")


def _degree(root: float, scale: tuple[int, ...], degree: int, octave: int = 0) -> float:
    d = degree % 7
    o = octave + degree // 7
    semitones = scale[d] + 12 * o
    return root * (2 ** (semitones / 12.0))


def synthesize_music(prompt: str = "", duration_s: float = 30.0,
                     slug: str = "") -> tuple[bytes, dict]:
    """合成一段真实可听的循环 BGM（和弦 pad + 琶音 + 低音）。"""
    text = f"{prompt} {slug}".strip()
    seed = _seed(text)
    rnd = random.Random(seed)
    duration = max(1.0, min(120.0, float(duration_s or 30.0)))
    minor = any(k in text.lower() for k in _MINOR_KEYS) or (seed % 3 == 0)
    scale = _MINOR if minor else _MAJOR
    root = 196.0 if minor else 220.0
    root *= 2 ** ((seed % 12) / 12.0)
    bpm = 72 + (seed % 30)
    bar = 4 * 60.0 / bpm                      # 4/4 小节时长
    beat = 60.0 / bpm
    progression = (0, 5, 2, 6) if minor else (0, 4, 5, 3)
    buf = [0.0] * int(duration * SAMPLE_RATE)

    bar_idx = 0
    t = 0.0
    while t < duration:
        deg = progression[bar_idx % len(progression)]
        chord_root = _degree(root, scale, deg)
        third = _degree(root, scale, deg + 2)
        fifth = _degree(root, scale, deg + 4)
        # pad：整小节铺底，慢起慢落
        _add(buf, t, _tone(chord_root, bar, 0.075, "sine", attack=0.35, release=0.45))
        _add(buf, t, _tone(third, bar, 0.055, "sine", attack=0.4, release=0.5))
        _add(buf, t, _tone(fifth, bar, 0.05, "triangle", attack=0.4, release=0.5))
        # 低音：每拍根音
        for k in range(4):
            _add(buf, t + k * beat, _tone(chord_root / 2, beat * 0.9, 0.11, "sine",
                                          attack=0.01, release=0.12))
        # 琶音：八分音符
        notes = (0, 2, 4, 7, 4, 2, 7, 4)
        for k, nd in enumerate(notes):
            freq = _degree(root, scale, deg + nd, octave=1)
            _add(buf, t + k * (beat / 2), _tone(freq, beat * 0.45, 0.035, "triangle",
                                                attack=0.005, release=0.08))
        bar_idx += 1
        t += bar

    # 整体淡入/淡出，避免循环接缝爆音
    fade_in = min(len(buf), int(0.8 * SAMPLE_RATE))
    fade_out = min(len(buf), int(1.2 * SAMPLE_RATE))
    for i in range(fade_in):
        buf[i] *= i / fade_in
    for i in range(fade_out):
        buf[len(buf) - 1 - i] *= i / fade_out

    data = _to_wav_bytes(buf)
    info = {"minor": minor, "bpm": bpm, "duration_s": round(len(buf) / SAMPLE_RATE, 2), "seed": seed}
    return data, info


# ---------------------------------------------------------------- 质量检查
def analyze_wav(data: bytes) -> dict:
    """读取 wav 的时长/峰值/RMS，判断是否静音。

    `_tts_with_meta` 用它兜底：IndexTTS2 对空文本/异常参考音频可能返回一段
    全零 wav，若不检查就会被当成「真实配音」交付——用户听到的就是没声音。
    """
    with wave.open(io.BytesIO(data), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        n = wf.getnframes()
        frames = wf.readframes(n)
    if width == 2:
        vals = struct.unpack("<%dh" % (len(frames) // 2), frames)
        max_abs = 32768.0
    elif width == 1:
        vals = tuple(b - 128 for b in frames)
        max_abs = 128.0
    elif width == 4:
        vals = struct.unpack("<%di" % (len(frames) // 4), frames)
        max_abs = 2147483648.0
    else:
        return {"duration_s": n / rate if rate else 0, "channels": channels,
                "sample_rate": rate, "peak": 1.0, "rms": 1.0, "silent": False}
    if not vals:
        return {"duration_s": 0.0, "channels": channels, "sample_rate": rate,
                "peak": 0.0, "rms": 0.0, "silent": True}
    peak = max(abs(v) for v in vals) / max_abs
    rms = math.sqrt(sum((v / max_abs) ** 2 for v in vals) / len(vals))
    return {"duration_s": round(n / rate, 3) if rate else 0, "channels": channels,
            "sample_rate": rate, "peak": round(peak, 4), "rms": round(rms, 4),
            "silent": peak < 0.002}
