"""把「闭眼帧」对齐到与「睁眼帧」同一张画布，保证眨眼时人物不跳位、不被裁。

为什么需要：睁眼/闭眼是两次独立出图，抠图后的外接框尺寸差得不少
（本轮实测成女 open 412x1018 / closed 347x969，兽形 open 653x775 / closed 803x863）。
直接换 `<img src>` 会有两个 bug：
  * 眨眼瞬间人物突然缩小再弹回（尺寸不一致）；
  * 兽形闭眼帧比睁眼宽，塞进睁眼画布会把尾巴右半边裁掉（第一版就栽在这）。

做法（每个形态）：
  1) 两帧各自先裁到不透明外接框；
  2) 闭眼帧按「高度对齐」等比缩放；
  3) 画布取两帧缩放后的**并集尺寸**（宁可多一圈透明，绝不能裁内容）；
  4) 底部对齐 + 水平按不透明像素质心对齐（脚踩同一处、身体中轴同一处）；
  5) 两帧一起写回 pet2d-*-open/closed_v1.png，尺寸从此完全一致。
"""
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "desktop_pet" / "renderer" / "assets"

FORMS = ["woman", "teen", "beast"]
PAD = 6  # 并集画布四周留一点余量，避免羽化边被切


def centroid_x(im):
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


def align(form: str) -> None:
    op_path = ASSETS / f"pet2d-{form}-open_v1.png"
    cl_path = ASSETS / f"pet2d-{form}-closed_v1.png"
    op = Image.open(op_path).convert("RGBA")
    cl = Image.open(cl_path).convert("RGBA")

    op = op.crop(op.getbbox())
    cl = cl.crop(cl.getbbox())

    s = op.size[1] / cl.size[1]  # 闭眼帧按高度对齐
    cl2 = cl.resize((max(1, round(cl.size[0] * s)), op.size[1]), Image.LANCZOS)

    cx_op = centroid_x(op)
    cx_cl = centroid_x(cl2)

    # 画布宽度必须容纳「质心对齐后」两帧各自的完整宽度。
    # 旧版只取 max(op.w, cl2.w) + PAD*2，可一旦质心不在正中（白狐尾巴偏右，
    # 会把质心往右拉），左贴边量 W/2-cx 就变成正数，右侧尾巴直接被画布边界切掉
    # —— 用户看到的「尾巴没显示全」就是这个。这里先按最坏情况给足宽度。
    align_x = max(cx_op, cx_cl) + PAD  # 对齐基准：画布里的质心横坐标
    left_op = round(align_x - cx_op)
    left_cl = round(align_x - cx_cl)
    W = max(left_op + op.size[0], left_cl + cl2.size[0]) + PAD
    H = max(op.size[1], cl2.size[1]) + PAD * 2

    # 两帧必须各自单独合成到同一套画布上，不能把两帧叠在一起写两份
    # （第一版就是叠成一张同时存给 open/closed，结果睁眼帧也被画成了闭眼）。
    open_canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    open_canvas.alpha_composite(op, (left_op, H - PAD - op.size[1]))
    closed_canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    closed_canvas.alpha_composite(cl2, (left_cl, H - PAD - cl2.size[1]))

    # 再整体裁一次透明边（两帧共用同一个外接框，尺寸严格一致）
    box = open_canvas.getbbox()
    bc = closed_canvas.getbbox()
    if box and bc:
        box = (min(box[0], bc[0]), min(box[1], bc[1]), max(box[2], bc[2]), max(box[3], bc[3]))
    box = box or (0, 0, W, H)
    open_canvas = open_canvas.crop(box)
    closed_canvas = closed_canvas.crop(box)
    open_canvas.save(op_path)
    closed_canvas.save(cl_path)

    canvas = open_canvas
    a = canvas.getchannel("A").tobytes()
    opaque = sum(1 for v in a if v > 200) / len(a)
    print(f"{form}: open {op.size} / closed {cl.size}×{s:.3f} → 共同画布 {canvas.size}"
          f"  质心差 {cx_op - cx_cl:+.1f}px  不透明 {opaque*100:.1f}%")


def main() -> int:
    for f in FORMS:
        align(f)
    print("ALIGN_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
