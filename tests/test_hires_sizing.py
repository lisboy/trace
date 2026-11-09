"""高分辨率出图规划（_plan_sizes / _apply_hires）的回归测试。

背景：8G 显存跑 2360x1640（约 3.87MP）可出但慢（实测约 5 分钟）；再大（4K≈8MP）
单次采样放不下。这里锁住尺寸规划与工作流改造的核心不变式：
- 小图完全不动（<1.6MP 不做任何改写，保持历史行为）；
- 2K 级直接出图：采样按 16 对齐，解码走分块（tiled），尺寸零头用像素态 ImageScale 精确落位；
- 4K 级两段式：基准分辨率采样 + 像素态放大，避免 8G OOM；
- Qwen VAE 是 16x 下采样，绝不能再用 LatentUpscale 按 8x 语义缩放（会放大 2 倍）。
"""
from __future__ import annotations

from game_agent.assets import comfyui as c


def _wf(width: int, height: int):
    wf = c._dit_workflow()
    wf["4"]["inputs"]["unet_name"] = "u.safetensors"
    wf["11"]["inputs"]["clip_name"] = "c.safetensors"
    wf["12"]["inputs"]["vae_name"] = "v.safetensors"
    return c._patch_workflow(wf, "p", "n", width, height, batch=1)


def test_plan_2360x1640_direct_exact():
    p = c._plan_sizes(2360, 1640)
    assert p["two_stage"] is False
    assert (p["sample_w"], p["sample_h"]) == (2368, 1632)   # 16 对齐
    assert (p["out_w"], p["out_h"]) == (2360, 1640)         # 8 对齐、精确目标


def test_plan_4k_is_two_stage():
    p = c._plan_sizes(3840, 2160)
    assert p["two_stage"] is True
    assert (p["out_w"], p["out_h"]) == (3840, 2160)
    # 基准采样不超过阈值（4MP）
    assert p["sample_w"] * p["sample_h"] / 1e6 <= 4.0 + 0.001


def test_plan_small_unchanged():
    for w, h in [(512, 512), (1024, 1024), (1024, 1536)]:
        p = c._plan_sizes(w, h)
        assert p["two_stage"] is False
        assert (p["sample_w"], p["sample_h"]) == (p["out_w"], p["out_h"])


def test_apply_hires_2k_uses_tiled_and_exact_scale():
    wf = _wf(2360, 1640)
    ow, oh = c._apply_hires(wf, 2360, 1640)
    assert (ow, oh) == (2360, 1640)
    classes = {n["class_type"] for n in wf.values()}
    assert "VAEDecodeTiled" in classes
    assert "VAEDecode" not in classes
    # 采样 latents 尺寸 16 对齐
    empty = next(n for n in wf.values() if n["class_type"] == "EmptySD3LatentImage")
    assert (empty["inputs"]["width"], empty["inputs"]["height"]) == (2368, 1632)
    # 像素态 ImageScale 落到精确目标（而非 LatentUpscale，避免 16x/8x VAE 语义错配）
    scale = next(n for n in wf.values() if n["class_type"] == "ImageScale")
    assert (scale["inputs"]["width"], scale["inputs"]["height"]) == (2360, 1640)
    assert "LatentUpscale" not in classes


def test_apply_hires_small_untouched():
    wf = _wf(1024, 1024)
    ow, oh = c._apply_hires(wf, 1024, 1024)
    assert (ow, oh) == (1024, 1024)
    classes = {n["class_type"] for n in wf.values()}
    # 小图不强制 tiled（保持原行为，避免无谓改变）
    assert "VAEDecodeTiled" not in classes
    assert "VAEDecode" in classes


def test_apply_hires_4k_pixel_upscale_present():
    wf = _wf(3840, 2160)
    ow, oh = c._apply_hires(wf, 3840, 2160)
    assert (ow, oh) == (3840, 2160)
    classes = {n["class_type"] for n in wf.values()}
    assert "VAEDecodeTiled" in classes
    scale = next(n for n in wf.values() if n["class_type"] == "ImageScale")
    assert (scale["inputs"]["width"], scale["inputs"]["height"]) == (3840, 2160)
