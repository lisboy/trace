"""把立绘的白底抠成透明（边缘连通泛洪，保住角色自身的白毛）。

为什么不能直接 `白色→透明`：
  这只白狐本体就是白的（毛、裤子、鞋都接近纯白）。按颜色阈值抠，
  角色身上会被打出一堆洞。正确做法是**从图片四边泛洪**：
  只有与边框相连的白像素才算背景，角色内部被轮廓包围的白像素一律保留。

再做三件事提升成品质感：
  1) 封闭背景块：抬臂时手臂与头发围出的白块、发丝之间、两腿之间的白缝，
     四边不连通、泛洪到不了，会残留成白斑（用户圈出的就是手肘内侧那块）。
     这里按「颜色等于已知背景基准 + 几乎没有阴影梯度」把它们也判成背景，
     衣服高光（带褶皱阴影，mean 低于背景 5 个点以上）不在判据内，不会被误删。
  2) 羽化：对前景边缘 1px 做半透明过渡，缩放显示时不会出现白边
     （只对「与图片四边连通的背景」羽化，避免封闭小块把细发丝羽化没）；
  3) 自动裁剪：裁到角色外接框 + 少量留白，让三种形态在桌宠里视觉大小一致。
"""
import sys

from PIL import Image

EDGE_THRESHOLD = 236   # 泛洪时认为「是背景白」的亮度下限
FEATHER = 1            # 边缘羽化像素
PAD = 8                # 裁剪后四周留白
ENCLOSED_MIN = 60      # 封闭背景块的最小像素数（更小的当噪声/高光留着）
ENCLOSED_FLAT = 12     # 封闭块亮度 p05~p95 跨度上限：真背景平坦，衣服高光有褶皱梯度
ENCLOSED_TOL = 3       # 封闭块中位亮度允许低于背景中位数的幅度
# 「可能没删干净」的告警阈值（只打日志，不自动删）。
# 实测：真衣物/毛的高光 R-B 为 +0.2~+1.5（发丝、银外套高光同样接近中性），
# 与残留背景（R-B 0.2~1.4）在色度上也重叠 —— 单靠颜色分不开。
# 唯一稳的差别是**面积**：用户实际能看见的残留白斑都在 1500px 以上，
# 而发丝/高光的碎块多在几百 px。所以这里只点名「大 + 极中性 + 平整」的块。
SUSPECT_MIN = 1500     # 面积到这个量级才值得人看一眼
SUSPECT_CHROMA = 1.5   # |R-B| 不超过这个值算「中性白」
SUSPECT_DARK = 12      # 中位亮度比背景基准低不超过这个值
FILES = [
    # 2026-11-14：用户给了三联参考图（#1 七头身成女 / #2 明显矮一截的少女 / #3 坐姿白狐），
    # 三个形态全部按它重出。源图是 pet3-*，**输出仍用 pet2d-* 这套老文件名**，
    # 这样渲染层、主进程、历史记忆里的路径全都不用改。
    # 2026-11-14 插话二：用户指出 #1 成女「头发盖眼睛」、#3 兽形「抠图把身体吃了一半」。
    #   → 成女重出为 pet4-woman-*（刘海中分、双眼完全露出），仍走本脚本白底泛洪；
    #   → 兽形改出绿幕版 pet4-beast-*，走 cutout_green.py 色键抠图
    #     （白色泛洪实测会把身体中轴吃掉 22.8%，白狐本体就是白的，此法不可用）。
    "assets/art/character/pet3-teen-open_v1.png::pet2d-teen-open_v1.png",
    "assets/art/character/pet3-teen-closed_v1.png::pet2d-teen-closed_v1.png",
    "assets/art/character/pet4-woman-open_v1.png::pet2d-woman-open_v1.png",
    # 闭眼帧一律以 open 帧 img2img 出（denoise≈0.3），避免单出换脸换姿势
    "assets/art/character/pet5-woman-closed_v1.png::pet2d-woman-closed_v1.png",
]


def cutout(src: str, dst: str) -> tuple[int, int]:
    im = Image.open(src).convert("RGB")
    w, h = im.size
    px = im.load()

    def white_at(th: int):
        def is_white(x: int, y: int) -> bool:
            r, g, b = px[x, y]
            return r >= th and g >= th and b >= th
        return is_white

    def flood(is_white) -> bytearray:
        bg = bytearray(w * h)
        stack = []
        for x in range(w):
            stack.append((x, 0))
            stack.append((x, h - 1))
        for y in range(h):
            stack.append((0, y))
            stack.append((w - 1, y))
        while stack:
            x, y = stack.pop()
            i = y * w + x
            if bg[i] or not is_white(x, y):
                continue
            bg[i] = 1
            if x > 0:
                stack.append((x - 1, y))
            if x < w - 1:
                stack.append((x + 1, y))
            if y > 0:
                stack.append((x, y - 1))
            if y < h - 1:
                stack.append((x, y + 1))
        return bg

    # 阈值自适应：有些图的背景是淡灰（不是纯 255），固定阈值会一个像素都吃不到，
    # 抠图静默失败（输出仍是 100% 不透明）。这里从高到低试，
    # 取「背景占比合理（35%~92%）」的第一个阈值。
    ratio = 0.0
    bg = flood(white_at(EDGE_THRESHOLD))
    best_th = EDGE_THRESHOLD
    for th in (EDGE_THRESHOLD, 230, 224, 218, 212, 206, 200, 192, 184):
        bg = flood(white_at(th))
        ratio = sum(bg) / (w * h)
        best_th = th
        if 0.30 <= ratio <= 0.92:
            break
    print(f"  {src.split('/')[-1]}: 阈值 {best_th}, 背景占比 {ratio*100:.1f}%")

    # 1b) 封闭背景块：白但不是「与图片四边连通」的白。抬臂/发丝/腿缝围出的白块
    #     四边泛洪到不了，会残留成白斑。判据必须是「像背景」而非「像白」——
    #     白狐的衣服本身就是白的，只按亮度会连衣服一起删。
    #     实测（pose-woman-head-v3）：真背景块亮度中位 247~250、p05~p95 跨度 ≤10；
    #     衣服高光中位 ≤242（褶皱阴影把它压下去）、跨度常在 6~12。
    #     因此用「中位亮度贴近背景基准」+「跨度小」两条一起卡。
    bg_vals = sorted(
        min(px[x, y]) for y in range(0, h, 3) for x in range(0, w, 3) if bg[y * w + x]
    )
    bg_med = bg_vals[len(bg_vals) // 2] if bg_vals else 255
    seen = bytearray(w * h)
    enclosed = []
    suspects = []
    for y0 in range(h):
        for x0 in range(w):
            i = y0 * w + x0
            if seen[i] or bg[i]:
                continue
            if not (min(px[x0, y0]) >= best_th):
                continue
            stack2 = [(x0, y0)]
            seen[i] = 1
            pts = []
            while stack2:
                x, y = stack2.pop()
                pts.append((x, y))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < w and 0 <= yy < h:
                        j = yy * w + xx
                        if seen[j] or bg[j]:
                            continue
                        if min(px[xx, yy]) >= best_th:
                            seen[j] = 1
                            stack2.append((xx, yy))
            if len(pts) < ENCLOSED_MIN:
                continue
            vals = sorted(min(px[x, y]) for x, y in pts)
            n = len(vals)
            removable = (
                vals[n // 2] >= bg_med - ENCLOSED_TOL
                and vals[int(n * 0.95)] - vals[int(n * 0.05)] <= ENCLOSED_FLAT
            )
            if removable:
                # 像背景：中位亮度贴背景基准 + 无阴影梯度
                enclosed.extend(pts)
                continue

            # ---- 判为「衣物/毛的高光」而保留的封闭块 ----
            # 这条判据在生成的图上有盲区：背景在角色附近被环境光压暗
            # （实测 pose-woman-head-v3 画面角落 249、角色身边 239），贴边白块会
            # 低到 239 被误判成衣物留下，成品里就是一块方形白斑（用户圈出过）。
            # 亮度不可分（白背心 243、白裤 244 与残留块 239 只差 4~5），所以不自动删，
            # 只把「颜色极中性」的块点名：真衣物偏暖（R-B +3.1~+4.7），
            # 残留背景接近中性（R-B ≤1.5）。换素材时看这行警告就知道哪块要人工确认。
            if len(pts) < SUSPECT_MIN:
                continue
            mr = sum(px[x, y][0] for x, y in pts) / n
            mb = sum(px[x, y][2] for x, y in pts) / n
            if abs(mr - mb) <= SUSPECT_CHROMA and vals[n // 2] >= bg_med - SUSPECT_DARK:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                suspects.append((len(pts), round(mr - mb, 2), vals[n // 2],
                                 min(xs), min(ys), max(xs), max(ys)))

    for x, y in enclosed:
        bg[y * w + x] = 1
    print(f"    封闭背景块：{len(enclosed)}px（背景基准亮度 {bg_med}）")
    for n, rb, med, x0, y0, x1, y1 in suspects:
        print(f"    ⚠ 可疑残留（中性白、留着了）：{n}px ({x0},{y0})-({x1},{y1}) "
              f"中位亮度 {med} R-B {rb:+}")

    # 羽化只看「与四边连通的背景」：封闭白块常常夹在细发丝之间，
    # 若把它也算进 near_bg，发丝会被羽化成半透明、看起来变细或消失。
    edge_bg = bytearray(bg)
    for x, y in enclosed:
        edge_bg[y * w + x] = 0

    # 2) 拼 RGBA：背景全透明；与背景相邻的前景像素做半透明羽化
    out = Image.new("RGBA", (w, h))
    opx = out.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            r, g, b = px[x, y]
            if bg[row + x]:
                opx[x, y] = (r, g, b, 0)
                continue
            # 检查 FEATHER 范围内是否有背景像素 → 按比例给 alpha
            a = 255
            if FEATHER > 0:
                near_bg = 0
                total = 0
                for dy in range(-FEATHER, FEATHER + 1):
                    yy = y + dy
                    if yy < 0 or yy >= h:
                        continue
                    for dx in range(-FEATHER, FEATHER + 1):
                        xx = x + dx
                        if xx < 0 or xx >= w:
                            continue
                        total += 1
                        if edge_bg[yy * w + xx]:
                            near_bg += 1
                if total and near_bg:
                    a = int(255 * (1 - near_bg / total * 0.85))
            opx[x, y] = (r, g, b, max(0, min(255, a)))

    # 3) 裁到不透明像素的外接框 + 留白
    bbox = out.getbbox()
    if bbox:
        x0, y0, x1, y1 = bbox
        x0 = max(0, x0 - PAD)
        y0 = max(0, y0 - PAD)
        x1 = min(w, x1 + PAD)
        y1 = min(h, y1 + PAD)
        out = out.crop((x0, y0, x1, y1))

    out.save(dst)
    # 统计：确保不是全透明（全透明说明泛洪把整张吃掉了）
    alpha = out.getchannel("A")
    opaque = sum(1 for v in alpha.getdata() if v > 200)
    print(f"{dst}  {out.size[0]}x{out.size[1]}  不透明像素 {opaque}  ({opaque*100//(out.size[0]*out.size[1])}%)")
    return out.size


def main() -> int:
    for f in FILES:
        # 「源图::目标文件名」形式：成女那两张的源图换了名字，但输出必须保持
        # pet2d-woman-open/closed_v1.png（渲染层、主进程、历史记忆都指向这个路径）。
        if "::" in f:
            src, name = f.split("::", 1)
            dst = "desktop_pet/renderer/assets/" + name
        else:
            src = f
            dst = f.replace("assets/art/character/", "desktop_pet/renderer/assets/")
        cutout(src, dst)
    print("CUTOUT_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
