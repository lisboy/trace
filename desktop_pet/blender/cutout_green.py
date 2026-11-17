"""把「纯绿幕」立绘抠成透明（兽形专用，替代会吃掉白毛的白色泛洪）。

为什么必须色键而不是白底泛洪：
  白狐本体就是白的，`亮度 >= 阈值` 的泛洪会顺着白毛边缘一路漫进去。
  实测 pet3-beast 在阈值 236 时「身体中轴被吃掉 22.8%」，阈值 230 时 35.7%
  —— 这就是用户看到的「身体都没一半了」。

做法：
  1) 判定绿幕像素：绿通道显著强于红、蓝（g - max(r,b) > KEY_DOM），
     并且从图片四边泛洪，保证只吃「连到画外的绿」；
  2) 角色身上的白毛、粉垫、蓝眼都不满足绿主导，永远进不来；
  3) 边缘按绿残留量做半透明过渡 + 去绿边（把边缘像素的绿压到与红蓝同量级），
     否则缩放到桌面小尺寸会有一圈绿胡子；
  4) 裁到不透明外接框 + 留白。
"""
import sys
from collections import deque
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "assets" / "art" / "character"
DST = ROOT / "desktop_pet" / "renderer" / "assets"

KEY_DOM = 42      # g - max(r,b) 超过它才可能是绿幕
KEY_G = 90        # 绿通道本身要够亮，避免暗部误判
SOFT = 18         # 边缘软过渡带宽（绿主导度落在 [KEY_DOM-SOFT, KEY_DOM] 之间算半透明）
ENCLOSED_MIN = 40 # 封闭绿块最小像素数
PAD = 10

FILES = [
    ("pet4-beast-open_v1.png", "pet2d-beast-open_v1.png"),
    # 闭眼帧以 open 帧 img2img 出：兽形 denoise 0.62 眼睛没闭上，0.80 才真正闭眼
    ("pet7-beast-closed_v1.png", "pet2d-beast-closed_v1.png"),
]


def cutout(src: Path, dst: Path) -> None:
    im = Image.open(src).convert("RGB")
    w, h = im.size
    px = im.load()

    def dom(x, y):
        r, g, b = px[x, y]
        return g - max(r, b), g

    mark = bytearray(w * h)
    q = deque()
    for x in range(w):
        q.append((x, 0))
        q.append((x, h - 1))
    for y in range(h):
        q.append((0, y))
        q.append((w - 1, y))

    while q:
        x, y = q.popleft()
        i = y * w + x
        if mark[i]:
            continue
        d, g = dom(x, y)
        if d <= KEY_DOM or g <= KEY_G:
            continue
        mark[i] = 1
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            xx, yy = x + dx, y + dy
            if 0 <= xx < w and 0 <= yy < h:
                q.append((xx, yy))

    ratio = sum(mark) / (w * h)
    print(f"  {src.name}: 绿幕占比 {ratio*100:.1f}%")

    # 1b) 封闭绿块：被身体围住的绿幕（比如尾巴绕成的圈、抬起的手臂与身体之间），
    #     四边泛洪同样到不了，会残留成一块绿斑。绿幕没有「本体也是绿」的歧义，
    #     所以判据可以直接用绿主导度，不必像白底那样附加亮度/平坦度条件。
    #     绿幕的软边本来就是按绿主导度算的（不看相邻关系），所以这里不用担心
    #     「封闭块旁边没有边缘绿幕、软边失效」。
    seen = bytearray(w * h)
    enclosed = 0
    for y0 in range(h):
        for x0 in range(w):
            i = y0 * w + x0
            if seen[i] or mark[i]:
                continue
            d0, g0 = dom(x0, y0)
            if d0 <= KEY_DOM or g0 <= KEY_G:
                continue
            stack = [(x0, y0)]
            seen[i] = 1
            pts = []
            while stack:
                x, y = stack.pop()
                pts.append((x, y))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < w and 0 <= yy < h:
                        j = yy * w + xx
                        if seen[j] or mark[j]:
                            continue
                        dd, gg = dom(xx, yy)
                        if dd > KEY_DOM and gg > KEY_G:
                            seen[j] = 1
                            stack.append((xx, yy))
            if len(pts) >= ENCLOSED_MIN:
                for x, y in pts:
                    mark[y * w + x] = 1
                enclosed += len(pts)
    print(f"    封闭绿块：{enclosed}px")

    out = Image.new("RGBA", (w, h))
    opx = out.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            r, g, b = px[x, y]
            if mark[row + x]:
                opx[x, y] = (r, g, b, 0)
                continue
            d = g - max(r, b)
            if d > KEY_DOM - SOFT:
                # 半透明过渡：越接近绿幕阈值越透明；同时去绿边
                # （把绿压到不超过红蓝的最大值），否则缩小到桌面尺寸时有一圈绿胡子。
                # 这一支对「封闭绿块」边缘同样生效：那里的近绿像素也需要去绿。
                a = int(255 * max(0.0, (KEY_DOM - d) / SOFT))
                g2 = min(g, max(r, b) + 6)
                opx[x, y] = (r, g2, b, a)
            else:
                opx[x, y] = (r, g, b, 255)

    bbox = out.getbbox()
    if bbox:
        x0, y0, x1, y1 = bbox
        out = out.crop((max(0, x0 - PAD), max(0, y0 - PAD),
                        min(w, x1 + PAD), min(h, y1 + PAD)))

    out.save(dst)
    alpha = out.getchannel("A").tobytes()
    opaque = sum(1 for v in alpha if v > 200) / len(alpha)
    print(f"    -> {dst.relative_to(ROOT)} {out.size[0]}x{out.size[1]} 不透明 {opaque*100:.1f}%")


def main() -> int:
    for s, d in FILES:
        cutout(SRC / s, DST / d)
    print("CUTOUT_GREEN_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
