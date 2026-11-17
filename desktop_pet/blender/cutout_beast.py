"""把「渐变纯色背景」抠成透明（兽形那两张：紫色渐变 + 白边框）。

之前两版失败的根因：
  * 白底泛洪 —— 这张底色是紫的，一个像素都吃不到（背景占比 0.0%）；
  * 纯色基准 + 距离阈值 —— 底色是渐变的，四边众数色与图片中部差得远。
正确做法（本脚本）：**只与「相邻已判定为背景的像素」比较**，
逐个像素往下长，渐变背景也能顺着爬完，且不会越界吃掉角色：

    is_bg(p) = 与任一已标记背景邻居的颜色距离 < TOL

角色白毛与紫底差得很远，永远进不来。白边框也一样：它与紫底差得远，
所以边框本身会被当成前景保留——正好，那圈描边是角色的可爱风格要素。
"""
import sys
from collections import deque

from PIL import Image

FILES = [
    ("assets/art/character/pet2d-beast-open_v1.png",
     "desktop_pet/renderer/assets/pet2d-beast-open_v1.png"),
    ("assets/art/character/pet2d-beast-closed_v1.png",
     "desktop_pet/renderer/assets/pet2d-beast-closed_v1.png"),
]
TOL = 62        # 与相邻背景像素的欧氏距离上限。
# 这张 fig 的底色像素噪点很大（相邻像素差 20~40），TOL 26 时一步都走不动
# （背景占比 0.0%）。62 能顺着噪声爬完渐变，而角色白毛(≈245)与紫底(≈120)
# 距离 ≈200，仍然安全不会被吃进来。
PAD = 10


def cutout(src: str, dst: str) -> None:
    im = Image.open(src).convert("RGB")
    w, h = im.size
    px = im.load()
    mark = bytearray(w * h)

    def dist2(c1, c2) -> int:
        return (c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2

    # 四边作为种子，BFS 沿渐变生长
    q = deque()
    for x in range(w):
        q.append((x, 0))
        q.append((x, h - 1))
    for y in range(h):
        q.append((0, y))
        q.append((w - 1, y))
    tol2 = TOL * TOL
    while q:
        x, y = q.popleft()
        i = y * w + x
        if mark[i]:
            continue
        # 与已标记邻居比：有一个够近就算背景
        ok = False
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            xx, yy = x + dx, y + dy
            if 0 <= xx < w and 0 <= yy < h and mark[yy * w + xx]:
                if dist2(px[x, y], px[xx, yy]) <= tol2:
                    ok = True
                    break
        if not ok:
            continue
        mark[i] = 1
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            xx, yy = x + dx, y + dy
            if 0 <= xx < w and 0 <= yy < h:
                q.append((xx, yy))

    # 种子阶段：先按「与四边种子色接近」把明显背景标掉（不依赖邻居链），
    # 再泛洪补齐。上一版只靠邻居比色，第一圈就因为噪点走不动，
    # 背景占比一直是 0.0%（Bug：种子的邻居还没被标记时无从比较）。
    seeds = [px[x, y] for x in range(w) for y in (0, h - 1)] + \
            [px[x, y] for y in range(h) for x in (0, w - 1)]
    # 用「紫通道明显强于绿通道」这个色彩特征识别紫底，比绝对颜色更稳
    def is_purple_bg(c) -> bool:
        r, g, b = c
        return b > g + 25 and b > 90 and r < 210 and g < 190

    for y in range(h):
        row = y * w
        for x in range(w):
            if is_purple_bg(px[x, y]):
                mark[row + x] = 1

    ratio = sum(mark) / (w * h)
    print(f"  {src.split('/')[-1]}: 紫底识别占比 {ratio*100:.1f}%")
    if ratio < 0.25:
        # 2026-11-14：兽形源图已换成纯白背景（pet3-beast-*），白底泛洪才正确。
        # 这里识别不到紫底就直接跳过，别把白底图写成「100% 不透明」的废件。
        print("    ⚠️ 没识别到紫底（源图可能是白底）→ 跳过，交给 cutout_2d.py 处理")
        return

    out = Image.new("RGBA", (w, h))
    opx = out.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            r, g, b = px[x, y]
            if mark[row + x]:
                opx[x, y] = (r, g, b, 0)
                continue
            # 边缘羽化：邻接背景的像素半透明，缩放时不出硬边
            near = tot = 0
            for dy in (-1, 0, 1):
                yy = y + dy
                if yy < 0 or yy >= h:
                    continue
                for dx in (-1, 0, 1):
                    xx = x + dx
                    if xx < 0 or xx >= w:
                        continue
                    tot += 1
                    if mark[yy * w + xx]:
                        near += 1
            a = 255 if not (tot and near) else int(255 * (1 - near / tot * 0.9))
            opx[x, y] = (r, g, b, max(0, min(255, a)))

    bbox = out.getbbox()
    if bbox:
        x0, y0, x1, y1 = bbox
        out = out.crop((max(0, x0 - PAD), max(0, y0 - PAD),
                        min(w, x1 + PAD), min(h, y1 + PAD)))
    out.save(dst)
    al = list(out.getchannel("A").get_flattened_data() if hasattr(
        out.getchannel("A"), "get_flattened_data") else out.getchannel("A").getdata())
    opaque = sum(1 for v in al if v > 200)
    print(f"    -> {dst} {out.size[0]}x{out.size[1]} 不透明 {opaque*100//len(al)}%")


if __name__ == "__main__":
    for s, d in FILES:
        cutout(s, d)
    print("CUTOUT_GRADIENT_DONE")
    sys.exit(0)
