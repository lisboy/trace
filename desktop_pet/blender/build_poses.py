"""点击反应姿势帧：抠图 + 对齐到与「睁眼/闭眼」完全相同的画布。

为什么单独一个脚本：
  `cutout_2d.py` / `cutout_green.py` 只负责「白底/绿幕 → 透明」，`align_frames.py`
  只负责把「睁眼 vs 闭眼」两帧并到同一张画布。现在多出「捂头」「捂脸」「护尾」
  三类姿势帧，数量从 2 变成 5，且姿势帧的轮廓比常驻帧更宽（捂头时手肘外扩、
  抱尾巴时尾巴被抱到身前），必须五个帧一起算一次并集画布——否则姿势帧单独一张
  尺寸不同，渲染层换 <img src> 的瞬间会整体缩放跳一下（和当初「眨眼跳位」是同一个坑）。

流程（每个形态）：
  1) 姿势源图走各自的抠图（成女/少女 = 白底泛洪，兽形 = 绿幕色键）；
  2) 五个帧（open / closed / posehead / poseface / posetail）全部按高度对齐到 open 的不透明高度；
  3) 画布 = 五帧「质心对齐后」的并集宽度（沿用 align_frames 的教训：按并集算，
     否则尾巴会被竖直切断）；
  4) 五帧分别合成到同一画布、底部对齐，再统一裁到并集外接框；
  5) 写回 pet2d-{form}-open/closed/posehead/poseface/posetail_v1.png，尺寸严格一致。

用法：
    .venv\\Scripts\\python.exe desktop_pet/blender/build_poses.py
"""
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cutout_2d  # noqa: E402
import cutout_green  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "assets" / "art" / "character"
DST = ROOT / "desktop_pet" / "renderer" / "assets"
PAD = 6

# form -> {"posehead": 捂头源图, "poseface": 捂脸源图, "posetail": 护尾源图}
#
# 为什么 posetail 人形换成了「把尾巴抱到身前」而不是「尾巴消失」：
# img2img 从 denoise 0.58 一路试到 0.90，模型始终把狐尾当成身体固有部件，
# 收起来的都只是「小一圈」；而「双手把蓬松大尾抱到怀里」在 0.86 就能稳定出，
# 点尾巴时画面变化更明显，也更符合「被摸了尾巴 → 立刻护住」的反应。
# 兽形没法抱尾（没有手），保留 v3 那张「把尾巴拢到身前、炸毛瞪人」。
POSE_SRC = {
    "woman": {
        "posehead": "pose-woman-head-v3_v1.png",
        "poseface": "pet12-woman-face_v1_v1.png",
        "posetail": "pet11-woman-tailhug_v1_v1.png",
        # 蹲姿帧：img2img 出「抱脚」动作时模型必定多画一条腿（站姿两条腿的先验改不掉），
        # 所以改成文生图出一张「蹲下抱住膝盖」的完整新构图——腿只有两条，动作反差也更大。
        "posefeet": "pet18-woman-feet_v1.png",
    },
    "teen": {
        "posehead": "pose-teen-head-v3_v1.png",
        "poseface": "pet12-teen-face_v1_v1.png",
        "posetail": "pet11-teen-tailhug_v1_v1.png",
        "posefeet": "pet18-teen-feet_v1.png",
    },
    "beast": {
        "posehead": "pose-beast-head-v3_v1.png",
        "poseface": "pet12-beast-face_v1_v1.png",
        "posetail": "pose-beast-tail-v3_v1.png",
    },
}

# 人形的新姿势源图放在 assets/art/illustration 下，兽形的绿幕源图仍在 character 下
ALT_SRC_DIR = ROOT / "assets" / "art" / "illustration"

GREEN = {"beast"}  # 兽形是绿幕源图，其余是白底

# 蹲姿帧按「站姿身高的这个比例」摆放，而不是像其它帧一样拉齐到 open 的高度。
# 为什么：unify 的口径是「所有帧高度对齐到 open」，这对站/坐/捂头这类同构姿势是对的，
# 但 posefeet 是一张蹲成一团的新构图，按 open 高度拉齐会让蹲着的人比站着还高，
# 切姿势的瞬间人物猛地「膨大」一圈。0.58 ≈ 同身材的人抱膝蹲下后的自然高度比。
FRAME_H_RATIO = {"posefeet": 0.58}


def centroid_x(im: Image.Image) -> float:
    """不透明像是按 alpha 加权的水平质心（复制自 align_frames，口径保持一致）。"""
    a = im.split()[-1]
    w, h = a.size
    data = a.tobytes()
    tot = acc = 0
    for y in range(h):
        row = y * w
        for x in range(w):
            v = data[row + x]
            if v > 100:
                tot += v
                acc += x * v
    return acc / max(tot, 1)


def cut_poses(form: str) -> dict[str, Path]:
    out = {}
    for key, src_name in POSE_SRC[form].items():
        src = SRC / src_name
        if not src.exists():
            src = ALT_SRC_DIR / src_name
        dst = DST / f"pet2d-{form}-{key}_v1.png"
        if form in GREEN:
            cutout_green.cutout(src, dst)
        else:
            cutout_2d.cutout(str(src), str(dst))
        out[key] = dst
    return out


def unify(form: str) -> None:
    """把该形态的所有帧（open/closed/姿势帧）并到同一张画布。"""
    keys = ["open", "closed", "posehead", "poseface", "posetail"]
    # posefeet 是可选帧：兽形没有手抱不住脚，模型也改不动坐姿，所以兽形不出这张
    if "posefeet" in POSE_SRC[form]:
        keys.append("posefeet")
    paths = {k: DST / f"pet2d-{form}-{k}_v1.png" for k in keys}
    frames = {k: Image.open(p).convert("RGBA") for k, p in paths.items()}
    frames = {k: v.crop(v.getbbox()) for k, v in frames.items()}

    base_h = frames["open"].size[1]
    scaled = {}
    for k, im in frames.items():
        # 默认按高度对齐到 open，保证换帧不跳大小；蹲姿帧按 FRAME_H_RATIO 单独定高
        target_h = round(base_h * FRAME_H_RATIO.get(k, 1.0))
        s = target_h / im.size[1]
        scaled[k] = im if abs(s - 1) < 1e-6 else im.resize(
            (max(1, round(im.size[0] * s)), target_h), Image.LANCZOS
        )

    cents = {k: centroid_x(v) for k, v in scaled.items()}
    align_x = max(cents.values()) + PAD
    lefts = {k: round(align_x - cents[k]) for k in scaled}
    W = max(lefts[k] + scaled[k].size[0] for k in scaled) + PAD
    H = base_h + PAD * 2

    canvases = {}
    for k, im in scaled.items():
        c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        c.alpha_composite(im, (lefts[k], H - PAD - im.size[1]))
        canvases[k] = c

    box = None
    for c in canvases.values():
        b = c.getbbox()
        if not b:
            continue
        box = b if box is None else (
            min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3])
        )
    box = box or (0, 0, W, H)

    # 自检必须在「裁到并集外接框之前」做：裁完之后内容必然贴边（外接框的定义如此），
    # 那时再看边缘列没有意义。这里查的是合成画布的边界列——如果那里有不透明像素，
    # 说明这一帧确实被画布切掉了（就是当初「尾巴没显示全」的 bug）。
    clipped = {}
    for k, c in canvases.items():
        a = c.getchannel("A").load()
        clipped[k] = sum(1 for y in range(H) for x in (0, W - 1) if a[x, y] > 60)
    assert not any(clipped.values()), f"{form} 有帧被画布切断: {clipped}"

    sizes = set()
    for k, c in canvases.items():
        out = c.crop(box)
        sizes.add(out.size)
        out.save(paths[k])
    assert len(sizes) == 1, f"{form} 各帧画布不一致: {sizes}"

    print(f"{form}: 画布 {sizes.pop()}  质心 {cents}")
    print(f"  合成画布边界列不透明 {clipped}  （必须全 0；裁完贴边是正常的）")

def main() -> int:
    for form in ("woman", "teen", "beast"):
        print(f"===== {form} 抠姿势帧")
        cut_poses(form)
        print(f"===== {form} 全帧统一画布")
        unify(form)
    print("POSES_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
