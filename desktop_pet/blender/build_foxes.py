"""循迹白狐桌宠 · Blender 程序化建模脚本（Blender 4.2 LTS）。

用法（headless）：
    blender.exe -b -P desktop_pet/blender/build_foxes.py

产出：
    desktop_pet/renderer/assets/fox_woman.glb   成女（白狐姐姐）
    desktop_pet/renderer/assets/fox_teen.glb    少女（3 头身小女孩）
    desktop_pet/renderer/assets/fox_beast.glb   兽形（圆滚小白狐，极简可爱风）
    desktop_pet/blender/preview/*.png           三形态预览渲染

=========================== 两条硬经验（改之前务必读） ===========================
1) **有机形体一律用 metaball（元球）**，不要用一堆独立球去拼。
   独立球之间曲面不连续，渲染出来是「串珠腿 / 糖葫芦尾巴」，
   整只像拼装积木——这是「乐高感」的根源。
   元球同族的多个 element 会融成**单一光滑等值面**，天然是圆润的。

2) **耳朵/眼睛不能用尖锥和薄片**。
   之前用 ConeGeometry 做耳朵，缩小到桌宠尺寸就变成两片尖锐三角贴片；
   眼睛用「白眼球+虹膜+瞳孔」三层薄贴片则碎成一堆小点。
   现在：耳朵＝2~3 个元球融成的厚圆耳 + 独立族的粉色内衬；
        眼睛＝一整颗深色亮球 + 两颗突出表面的高光。
   这就是「极简可爱风」的做法：形体大而圆，五官少而大。
=============================================================================
"""
from __future__ import annotations

import math
import os
import sys

import bpy
from mathutils import Euler, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
PET_ROOT = os.path.dirname(HERE)                       # desktop_pet/
OUT_ASSETS = os.path.join(PET_ROOT, "renderer", "assets")
OUT_PREVIEW = os.path.join(HERE, "preview")

# 配色口径：白毛最亮、夹克中灰、内耳/腮红粉、眼睛深墨蓝。
COL = {
    "fur": (0.985, 0.988, 0.995, 1.0),        # 主体白（最亮）
    "fur_shade": (0.880, 0.900, 0.935, 1.0),  # 肚皮/暗部毛
    "tail_tip": (0.815, 0.855, 0.915, 1.0),
    "inner_ear": (0.975, 0.700, 0.770, 1.0),  # 内耳粉（用户明确要的）
    "hair": (0.965, 0.975, 0.995, 1.0),
    "skin": (1.0, 0.930, 0.915, 1.0),
    "jacket": (0.520, 0.575, 0.665, 1.0),
    "cloth": (0.985, 0.990, 1.0, 1.0),
    "pants": (0.700, 0.745, 0.800, 1.0),
    "boot": (0.585, 0.630, 0.700, 1.0),
    "eye_white": (1.0, 1.0, 1.0, 1.0),
    "iris": (0.330, 0.560, 0.775, 1.0),
    "pupil": (0.105, 0.135, 0.185, 1.0),      # 深墨蓝的眼睛本体
    "nose": (0.560, 0.470, 0.500, 1.0),       # 小粉鼻
    "cheek": (1.0, 0.520, 0.600, 1.0),
}


# --------------------------------------------------------------------------- #
# 场景与材质
# --------------------------------------------------------------------------- #
def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.film_transparent = True
    scene.render.resolution_x = 640
    scene.render.resolution_y = 780
    scene.render.resolution_percentage = 100


_MATS: dict[str, bpy.types.Material] = {}


def mat(name: str) -> bpy.types.Material:
    if name in _MATS:
        return _MATS[name]
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = COL[name]
    # 毛/布粗糙、眼睛和鼻子光滑；全部 0 金属度（白毛不能像塑料）
    rough = {"eye_white": 0.1, "iris": 0.12, "pupil": 0.08, "nose": 0.2,
             "jacket": 0.38, "boot": 0.45, "hair": 0.5, "cheek": 0.9,
             "skin": 0.6}.get(name, 0.82)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = 0.0
    if name == "cheek":
        # 腮红要看得出来但不抢眼睛：0.75 不透明度 + 略深的粉
        bsdf.inputs["Alpha"].default_value = 0.78
        m.blend_method = "BLEND"
    m.use_backface_culling = False
    _MATS[name] = m
    return m


# --------------------------------------------------------------------------- #
# 元球：有机形体的唯一正确做法
# --------------------------------------------------------------------------- #
class Blobs:
    """按「族」组织的 metaball 集合。

    族名（key）= 融合范围。同族 element 融成一张曲面；不同族各自独立。
    group 参数用来把同材质的部件拆成独立曲面（内耳不能融进白色耳朵里）。
    """

    def __init__(self, resolution: float = 0.012) -> None:
        self.res = resolution
        self.groups: dict[str, bpy.types.Object] = {}

    def _mb(self, mat_name: str, group: str | None = None) -> bpy.types.Object:
        key = group or mat_name
        if key not in self.groups:
            mb = bpy.data.metaballs.new(f"mb_{key}")
            mb.resolution = self.res
            mb.render_resolution = self.res
            # threshold 越低，等值面越外扩、相邻球越早融合。
            # 0.4 能把间距 < 半径的球融成连续曲面；0.62 会一鼓一鼓的。
            mb.threshold = 0.4
            obj = bpy.data.objects.new(f"mb_{key}", mb)
            bpy.context.collection.objects.link(obj)
            mb.materials.append(mat(mat_name))
            self.groups[key] = obj
        return self.groups[key]

    def ball(self, mat_name, loc, r, scale=(1, 1, 1), rot=(0, 0, 0),
             group=None) -> None:
        mb = self._mb(mat_name, group).data
        if scale == (1, 1, 1) and rot == (0, 0, 0):
            el = mb.elements.new(type="BALL")
            el.co = loc
            el.radius = r
        else:
            el = mb.elements.new(type="ELLIPSOID")
            el.co = loc
            el.size_x = r * scale[0]
            el.size_y = r * scale[1]
            el.size_z = r * scale[2]
            if rot != (0, 0, 0):
                el.rotation = Euler([math.radians(a) for a in rot], "XYZ").to_quaternion()

    def limb(self, mat_name, p0, p1, r0, r1, density: float = 0.3) -> None:
        """两点之间铺一条渐细的元球链，融合后是光滑的肢体。

        间距必须远小于半径（这里取 0.3×最小半径）。0.5 倍时等值面在球心处
        仍会轻微起伏，看起来就是一串珠子——这是腿像糖葫芦的根因。
        """
        dist = math.dist(p0, p1)
        rmin = max(1e-4, min(r0, r1))
        steps = max(10, int(dist / (rmin * density)) + 1)
        for i in range(steps):
            t = i / (steps - 1)
            loc = tuple(p0[k] + (p1[k] - p0[k]) * t for k in range(3))
            self.ball(mat_name, loc, r0 + (r1 - r0) * t)

    def convert(self) -> list[bpy.types.Object]:
        out = []
        for key in list(self.groups):
            obj = self.groups[key]
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.convert(target="MESH")
            conv = bpy.context.view_layer.objects.active
            conv.name = f"body_{key}"
            for p in conv.data.polygons:
                p.use_smooth = True
            out.append(conv)
        self.groups.clear()
        return out


# --------------------------------------------------------------------------- #
# 锐利部件：眼睛、腮红、小鼻头
# --------------------------------------------------------------------------- #
class Sharp:
    def __init__(self) -> None:
        self.objects: list[bpy.types.Object] = []

    def _finish(self, obj, name, mat_name, smooth=True):
        obj.name = name
        obj.data.materials.clear()
        obj.data.materials.append(mat(mat_name))
        if smooth:
            for p in obj.data.polygons:
                p.use_smooth = True
        self.objects.append(obj)
        return obj

    def sphere(self, name, mat_name, loc, r, scale=(1, 1, 1), seg=24, ring=16):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=seg, ring_count=ring,
                                             radius=r, location=loc)
        o = bpy.context.object
        o.scale = scale
        return self._finish(o, name, mat_name)

    def disc(self, name, mat_name, loc, radius, rot=(0, 0, 0)):
        bpy.ops.mesh.primitive_circle_add(vertices=24, radius=radius,
                                          fill_type="NGON", location=loc)
        o = bpy.context.object
        o.rotation_euler = Euler([math.radians(a) for a in rot], "XYZ")
        return self._finish(o, name, mat_name, smooth=False)


# --------------------------------------------------------------------------- #
# 共用零件
# --------------------------------------------------------------------------- #
def make_eye(S: Sharp, x: float, y: float, z: float, r: float,
             iris: bool = False, side: float = 1.0) -> None:
    """极简可爱风的眼睛：一整颗深色亮球 + 两颗突出表面的高光。

    为什么不画虹膜：桌宠只有 300px 宽，眼睛实际直径不到 20px，
    分层贴片会碎成一堆点。一颗大黑眼珠 + 高光才是这个尺寸下的正确画法。
    需要一点「狐狸的冷蓝」时，iris=True 会在眼球前压一片很淡的蓝。
    """
    S.sphere("eye", "pupil", (x, y, z), r, (1.0, 0.88, 1.05))
    if iris:
        S.sphere("iris", "iris", (x, y - r * 0.42, z), r * 0.62, (1.0, 0.4, 1.0))
    # 高光必须推出眼球表面（-Y 方向再外推），否则会陷进去看不见
    S.sphere("eye_hi", "eye_white",
             (x + side * r * 0.3, y - r * 0.92, z + r * 0.42), r * 0.36)
    S.sphere("eye_hi2", "eye_white",
             (x - side * r * 0.3, y - r * 0.86, z - r * 0.34), r * 0.17)


def make_face(S: Sharp, *, eye_r: float, eye_x: float, eye_y: float, eye_z: float,
              cheek_x: float, cheek_z: float, cheek_r: float,
              mouth_z: float, mouth_y: float, iris: bool = False) -> None:
    for s in (-1, 1):
        make_eye(S, s * eye_x, eye_y, eye_z, eye_r, iris=iris, side=s)
        # 腮红：面朝镜头(-Y)的圆片，压在脸颊表面稍外侧一点点，
        # 位置比眼睛更低更外。ang 让它略微朝外转，贴着脸的弧度。
        S.disc("cheek", "cheek", (s * cheek_x, eye_y + eye_r * 0.28, cheek_z),
               cheek_r, (90, 0, s * 20))
    # 小嘴：极扁的小球，远看成一条微笑弧
    S.sphere("mouth", "inner_ear", (0, mouth_y, mouth_z), 0.016, (1.7, 0.5, 0.55))


def make_ear(B: Blobs, x: float, y: float, z: float, w: float, h: float,
             tilt: float = 0.22, separate: bool = False) -> None:
    """圆润狐耳：底部一颗**宽**球 + 上端一颗小球，融成「下宽上尖」的圆润狐耳。

    ⚠️ 别做成两根细长竖条——那是兔耳。狐耳的口径是：
      底宽 ≈ 0.9~1.0 × w、高 ≈ 1.6~1.8 × w、上端快速收窄并略往外撇。
    内耳是独立族（group="inner_ear"），贴在耳朵朝前(-Y)的表面，占耳面一半。
    separate=True 时外耳也另起一族，轮廓上是两只独立耳朵，不与头融成猫耳帽。
    """
    for s in (-1, 1):
        ex = s * x
        grp = "ear" if separate else None
        # 底：宽而扁（狐耳根宽）
        B.ball("fur", (ex + s * w * 0.10, y, z + h * 0.14), w * 1.05,
               (1.0, 0.9, 0.95), rot=(0, s * 12, 0), group=grp)
        # 中段：略窄
        B.ball("fur", (ex + s * w * 0.26, y, z + h * 0.48), w * 0.74,
               (1.0, 0.88, 1.15), rot=(0, s * 16, 0), group=grp)
        # 上端：小圆尖，往外撇（狐耳的尖）
        B.ball("fur", (ex + s * w * 0.46, y + 0.01, z + h * 0.74), w * 0.42,
               (1.0, 0.85, 1.2), rot=(0, s * 20, 0), group=grp)
        # 粉色内衬：独立族，薄片贴在耳前
        B.ball("inner_ear", (ex + s * w * 0.20, y - w * 0.70, z + h * 0.42),
               w * 0.50, (1.0, 0.38, 1.35), group="inner_ear")


def make_tail(B: Blobs, base, length: float, thickness: float,
              droop: float, yaw: float, curl: float = 0.0) -> None:
    """狐尾：沿身后(+Y)铺一串渐细元球，融合成一条蓬松整尾。

    curl>0 时尾巴先往后上方翘再垂下，做出狐尾的弧度。
    元球融合后不会出现「一节一节」，这是换 metaball 的最大收益。
    """
    x, y, z = base
    n = 26
    cos_y, sin_y = math.cos(yaw), math.sin(yaw)
    for i in range(n):
        t = i / (n - 1)
        back = t * length * math.cos(droop)
        vert = -t * length * math.sin(droop) + math.sin(t * math.pi) * curl
        loc = (x + back * sin_y, y + back * cos_y, z + vert)
        rad = (0.06 + math.sin(t * math.pi) * 0.075) * thickness
        B.ball("tail_tip" if t > 0.74 else "fur", loc, rad, (1.0, 1.0, 0.95))


# --------------------------------------------------------------------------- #
# 形态三（重点）：兽形 —— 圆滚小白狐，极简可爱风
# --------------------------------------------------------------------------- #
def build_beast(B: Blobs, S: Sharp) -> float:
    """一只圆滚滚的坐姿小白狐。

    造型口径（用户明确要求：圆润度、粉色内耳、极简可爱）：
      * 身体和头**融成一整团**（chibi 轮廓），不做出明显脖子；
      * 只有 4 个小圆爪露在身下，不画腿的结构；
      * 两只大圆耳立在头顶两侧，粉色内衬占耳面一半；
      * 两颗大黑眼珠 + 高光，一个粉鼻头，两片腮红；
      * 大尾巴从身后卷起来，尾尖浅灰。

    ⚠️ 关键：五官必须放在**融合后的等值面外面**。
    身体三颗球融成一整团后，正面最外壳大约在 y ≈ -0.30；上一版把眼睛放在
    y=-0.268，看似在外面，实际落在球体之间加厚出来的曲面上，被整只吞掉了
    （渲染出来只有两个小白点——那是高光，眼珠本体埋在身体里）。
    现在统一按「最外壳 y≈-0.30」再往外推，眼珠本体可完整看到。
    """
    # ---- 身体 + 头：融成一整团蛋形 ----
    B.ball("fur", (0, 0.02, 0.36), 0.29, (1.06, 1.02, 1.0))     # 下身/坐垫
    B.ball("fur", (0, 0.0, 0.58), 0.275, (1.0, 1.0, 0.98))      # 上身
    B.ball("fur", (0, -0.01, 0.78), 0.245, (1.0, 0.98, 0.96))   # 头
    # 肚皮浅色块（很淡的软亮区）
    B.ball("fur_shade", (0, -0.22, 0.42), 0.145, (1.2, 0.5, 1.25))

    # ---- 4 个小圆爪（融合连在身体上，只露一点点）----
    for s in (-1, 1):
        B.ball("fur", (s * 0.135, -0.20, 0.052), 0.083, (1.0, 1.25, 0.7))
        B.ball("fur", (s * 0.225, -0.02, 0.058), 0.086, (0.95, 1.2, 0.68))

    # ---- 耳朵：大而圆 + 粉内衬，独立成族不融进头（否则像顶猫耳帽）----
    make_ear(B, x=0.135, y=-0.02, z=0.98, w=0.115, h=0.30, separate=True)

    # ---- 微凸的吻部 ----
    B.ball("fur", (0, -0.235, 0.70), 0.10, (1.25, 0.85, 0.8))

    # ---- 五官：全部推到最外壳之外 ----
    make_face(S, eye_r=0.082, eye_x=0.12, eye_y=-0.318, eye_z=0.80,
              cheek_x=0.196, cheek_z=0.715, cheek_r=0.055,
              mouth_z=0.672, mouth_y=-0.335)
    S.sphere("nose", "nose", (0, -0.352, 0.735), 0.036, (1.3, 0.95, 1.0))

    # ---- 大尾巴：从身体右侧探出来 ----
    # 正后方会被整只身体挡住（正交正面视角完全看不到），
    # 所以从右后侧往外、往上卷，露出一大截蓬松尾巴。
    make_tail(B, (0.26, 0.12, 0.24), 0.58, 1.75, droop=-0.85, yaw=-1.50, curl=0.30)
    return 1.36


# --------------------------------------------------------------------------- #
# 形态一：成女（白狐姐姐）
# --------------------------------------------------------------------------- #
def build_woman(B: Blobs, S: Sharp) -> float:
    """成女（白狐姐姐）：沿用少女版**已验证**的结构，只改比例——更高、腿更长、加腰线。

    这次不另起结构。前几版把躯干、腿、手臂都拆成独立部件去「堆」，结果
    躯干成了空心管子、前襟成了悬空方框、腿从髋侧伸出来 —— 越拼越像机器人。
    少女版的「一整团 + 连续肢体」已经被预览图证明是对的，这里照抄它的组织方式：
      1) 头 = 一颗大球 + 下颌小球的融合体；
      2) 身体 = 一颗躯干球（上宽下窄）；
      3) 腿 = 从髋下沿连续铺到脚；
      4) 手臂 = 从肩侧连续铺到手腕，起点压在肩上；
      5) 耳朵/眼睛/腮红照旧（这版五官已经很好）。
    """
    # ---- 腿：矮胖两截球（同少女版结构），不做长细棍 ----
    for s in (-1, 1):
        B.ball("pants", (s * 0.1, 0, 0.6), 0.082, (1.0, 1.0, 1.5))
        B.ball("boot", (s * 0.105, -0.03, 0.44), 0.078, (1.05, 1.25, 0.9))
        B.ball("boot", (s * 0.105, -0.07, 0.38), 0.068, (1.0, 1.35, 0.7))

    # ---- 身体：一整团（跟少女版一致的球叠，只把「腰」做出来）----
    B.ball("pants", (0, 0, 0.72), 0.135, (1.25, 1.05, 0.9))    # 髋
    B.ball("cloth", (0, 0, 0.95), 0.128, (1.15, 1.0, 1.25))    # 上身/白上衣
    B.ball("cloth", (0, 0, 1.13), 0.105, (1.35, 1.05, 0.7))    # 肩
    # 夹克：肩上一圈（短款搭肩）—— 用扁球压出一整圈，不是两片前襟
    B.ball("jacket", (0, 0.01, 1.06), 0.118, (1.22, 1.02, 0.62))
    B.ball("jacket", (0, 0.02, 0.84), 0.128, (1.18, 1.02, 0.3))   # 下摆
    for s in (-1, 1):
        # 短手臂：3 颗球垂下（粗短有肉感，与肩重叠）
        B.ball("jacket", (s * 0.155, 0, 1.02), 0.058, (1.0, 1.0, 1.1))
        B.ball("jacket", (s * 0.172, -0.005, 0.9), 0.052, (1.0, 1.0, 1.05))
        B.ball("skin", (s * 0.178, -0.012, 0.815), 0.05, (1.0, 0.95, 1.0))

    # ---- 颈 + 大头 ----
    B.ball("skin", (0, 0, 1.22), 0.062, (1.0, 1.0, 1.2))
    B.ball("skin", (0, 0, 1.4), 0.168, (1.02, 0.97, 1.04))
    B.ball("skin", (0, -0.032, 1.285), 0.1, (1.2, 1.0, 0.9))

    # ---- 银白长发 ----
    B.ball("hair", (0, 0.065, 1.435), 0.172, (1.02, 0.95, 1.0))
    B.ball("hair", (0, 0.005, 1.51), 0.13, (1.1, 0.95, 0.4))
    for s in (-1, 1):
        B.ball("hair", (s * 0.155, 0.02, 1.38), 0.075, (1.0, 0.9, 1.3))
        B.ball("hair", (s * 0.148, 0.0, 1.24), 0.062, (1.0, 0.9, 1.25))
    B.ball("hair", (0, 0.11, 1.26), 0.11, (1.05, 0.8, 1.5))

    # ---- 五官 / 耳 ----
    make_face(S, eye_r=0.062, eye_x=0.084, eye_y=-0.245, eye_z=1.405,
              cheek_x=0.12, cheek_z=1.335, cheek_r=0.04,
              mouth_z=1.312, mouth_y=-0.262, iris=True)
    make_ear(B, x=0.118, y=0.02, z=1.525, w=0.08, h=0.21)

    # ---- 大狐尾：从右后侧探出（正面可见）----
    make_tail(B, (0.15, 0.1, 0.7), 0.55, 1.35, droop=0.65, yaw=-1.25, curl=0.18)
    return 1.74


# --------------------------------------------------------------------------- #
# 形态二：少女（3 头身小女孩）
# --------------------------------------------------------------------------- #
def build_teen(B: Blobs, S: Sharp) -> float:
    # ---- 腿 / 过膝袜 / 小白鞋 ----
    for s in (-1, 1):
        B.ball("boot", (s * 0.115, -0.06, 0.05), 0.076, (1.05, 1.5, 0.7))
        B.ball("boot", (s * 0.115, -0.02, 0.12), 0.066, (1.0, 1.05, 0.9))
        B.limb("skin", (s * 0.115, 0, 0.15), (s * 0.1, 0, 0.38), 0.066, 0.08)
        B.ball("cloth", (s * 0.113, 0, 0.19), 0.072, (1.04, 1.04, 0.95))

    # ---- 裙子：圆台（下宽上窄），不旋转 ----
    bpy.ops.mesh.primitive_cone_add(vertices=44, radius1=0.30, radius2=0.13,
                                    depth=0.32, location=(0, 0, 0.57))
    skirt = bpy.context.object
    skirt.name = "skirt"
    skirt.scale = (1.0, 0.94, 1.0)
    for p in skirt.data.polygons:
        p.use_smooth = True
    skirt.data.materials.clear()
    skirt.data.materials.append(mat("jacket"))
    S.objects.append(skirt)

    # ---- 上身 + 搭肩短夹克 ----
    B.ball("cloth", (0, 0, 0.76), 0.105, (1.25, 1.05, 1.1))
    B.ball("jacket", (0, 0.015, 0.83), 0.095, (1.55, 1.25, 0.7))
    for s in (-1, 1):
        B.limb("jacket", (s * 0.16, 0, 0.81), (s * 0.212, 0, 0.64), 0.055, 0.045)
        B.ball("skin", (s * 0.216, 0, 0.60), 0.048)

    # ---- 大头 ----
    B.ball("skin", (0, 0, 1.16), 0.215, (1.0, 0.97, 1.02))
    B.ball("skin", (0, -0.04, 1.03), 0.13, (1.2, 1.0, 1.0))
    # 头发：包后脑 + 头顶薄发盖
    B.ball("hair", (0, 0.08, 1.19), 0.222, (1.02, 0.95, 1.0))
    B.ball("hair", (0, 0.01, 1.31), 0.17, (1.15, 0.95, 0.4))
    for s in (-1, 1):
        B.limb("hair", (s * 0.2, 0.02, 1.17), (s * 0.195, -0.01, 0.86), 0.072, 0.046)
    B.ball("hair", (0, 0.19, 1.04), 0.09, (1.0, 0.8, 1.5))

    make_face(S, eye_r=0.078, eye_x=0.098, eye_y=-0.278, eye_z=1.165,
              cheek_x=0.15, cheek_z=1.07, cheek_r=0.042,
              mouth_z=1.048, mouth_y=-0.292, iris=True)
    make_ear(B, x=0.138, y=0.02, z=1.30, w=0.098, h=0.30)

    # ---- 短蓬尾巴 ----
    make_tail(B, (0.07, 0.13, 0.62), 0.58, 1.3, droop=0.85, yaw=-0.7, curl=0.06)
    return 1.60


# --------------------------------------------------------------------------- #
# 合并 / 导出 / 渲染
# --------------------------------------------------------------------------- #
def merge_all(objs: list[bpy.types.Object], name: str) -> bpy.types.Object:
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    if len(objs) > 1:
        bpy.ops.object.join()
    root = bpy.context.view_layer.objects.active
    root.name = name
    return root


def add_lighting_and_cam(top_z: float) -> None:
    def light(name, loc, energy, color=(1, 1, 1)):
        d = bpy.data.lights.new(name, type="AREA")
        d.energy = energy
        d.size = 4.5
        d.color = color
        o = bpy.data.objects.new(name, d)
        o.location = loc
        bpy.context.collection.objects.link(o)
        direction = Vector((0, 0, top_z * 0.55)) - Vector(loc)
        o.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

    # 白毛最怕过曝：整体压暗，靠轮廓光和腮红/内耳提供层次
    light("key", (2.2, -3.2, 3.0), 150, (1.0, 0.97, 0.94))
    light("fill", (-2.8, -2.4, 1.4), 45, (0.82, 0.90, 1.0))
    light("rim", (-0.4, 3.0, 2.6), 105, (0.74, 0.86, 1.0))

    world = bpy.data.worlds.new("W")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.72, 0.79, 0.90, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 0.16
    bpy.context.scene.world = world
    # 二次元平涂口径：不做胶片曲线，靠材质明度差分层
    bpy.context.scene.view_settings.view_transform = "Standard"
    bpy.context.scene.view_settings.look = "None"

    cam_data = bpy.data.cameras.new("Cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = max(1.6, top_z * 1.18)
    cam = bpy.data.objects.new("Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    cam.location = (0, -6.0, top_z * 0.52)
    cam.rotation_euler = Euler((math.radians(90), 0, 0), "XYZ")


def export_and_render(name: str, objs: list[bpy.types.Object], top_z: float) -> None:
    root = merge_all(objs, "FoxBody")
    bpy.context.view_layer.objects.active = root
    bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
    root.location = (0, 0, 0)

    os.makedirs(OUT_ASSETS, exist_ok=True)
    glb = os.path.join(OUT_ASSETS, f"{name}.glb")
    bpy.ops.object.select_all(action="DESELECT")
    root.select_set(True)
    bpy.ops.export_scene.gltf(
        filepath=glb,
        export_format="GLB",
        use_selection=True,
        export_apply=True,
        export_yup=True,
        export_normals=True,
        export_texcoords=False,
        export_materials="EXPORT",
    )
    print(f"[build] 已导出 {glb}  ({os.path.getsize(glb)} 字节)  顶点 {len(root.data.vertices)}")

    add_lighting_and_cam(top_z)
    os.makedirs(OUT_PREVIEW, exist_ok=True)
    png = os.path.join(OUT_PREVIEW, f"{name}.png")
    bpy.context.scene.render.filepath = png
    bpy.context.scene.eevee.taa_render_samples = 48
    bpy.ops.render.render(write_still=True)
    print(f"[build] 已渲染 {png}")


def make(name: str, builder) -> None:
    reset_scene()
    _MATS.clear()
    B = Blobs()
    S = Sharp()
    top = builder(B, S)
    objs = B.convert() + S.objects
    export_and_render(name, objs, top)


def main() -> int:
    make("fox_beast", build_beast)     # 用户重点：先出兽形看效果
    make("fox_teen", build_teen)
    make("fox_woman", build_woman)
    print("[build] ALL_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
