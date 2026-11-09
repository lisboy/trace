"""人物「八方画像」多视角生成辅助。

一个角色要生成所有方向的参考图（正面/左右侧/背后/各 45°/俯视/仰视等）时，
用同一段基础描述 + 每个视角的相机方位，逐张串行出图。工作台手动出图和对话里的
generate_image 工具都复用这里的视角目录与提示词拼接，保证两处一致。

要点（2026-11 复盘）：
- **每张只画一个人、只拍一个视角的全身像**，绝不能出现「多视角参考图组 / 转盘 /
  角色设计页」这类会把模型带偏成「一张图里画好多个角度」的词——那会让每张图
  变成一张拼了多个姿势的联系表（实测就是这样翻车的）。
- 主提示词里常带「三分之四视角 / 半身 / 特写」这类**只适用于正面**的词，逐视角出图时
  必须把它们替换成该视角自己的朝向 + 统一全身构图，否则俯视/仰视/背面都会被这些词
  拽回「三分之四半身」。
- 背面视角（背后 / 右后 / 左后）应只看到背影/后脑，不强调脸。

视角 key 用英文（稳定、易传参），label/camera 用中文（直接进提示词）。
部分底模支持中文提示词；这里统一用「相机/构图」中文指令 + 保留主体原句。
"""
from __future__ import annotations

import re

# 标准转盘 8 方位（水平环绕一圈），顺序 = 从正面开始顺时针
_TURNAROUND = ["front", "front_right_45", "right", "back_right_45",
               "back", "back_left_45", "left", "front_left_45"]

# 每个视角：key / 中文标签 / 相机方位描述（拼进提示词）/ 构图约束 / 所属分组
#
# 相机描述要点：给出**明确的相机高度 + 俯仰角 + 朝向 + 可见面**，用单数、单张、单视角措辞，
# 明确「只画一个人」，避免模型画成多人拼图/转盘。构图统一为「单人全身竖构图」，
# 并在各视角之间保持一致，方便对齐（不同视角唯一变化的是观察方向）。
# 每个视角：key / 中文标签 / 相机方位描述（中文，进提示词）/ 构图约束 / 所属分组 /
# 英文相机指令（camera_en，置顶）——底模对"美女"主体常把中文构图词忽略、一律画成正面半身像，
# 实测把**英文相机指令放在最前面**才能真正扭转朝向（尤其背面/俯视/仰视）。
VIEWS: list[dict[str, str]] = [
    {"key": "front", "label": "正面", "group": "环绕",
     "framing": "单人全身像，头顶到脚完整入镜，竖构图",
     "camera": "正面平视：相机与人物眼睛等高、水平正对人物正前方，只能看到人物正面",
     "camera_en": "Full-body shot, head to toe, front view, camera at eye level facing her front, standing straight, entire body visible, vertical composition"},
    {"key": "front_right_45", "label": "右前45°", "group": "环绕",
     "framing": "单人全身像，头顶到脚完整入镜，竖构图",
     "camera": "右前方45度平视：相机与人物眼睛等高，从人物右前方斜向拍摄，同时看到正面与右侧的过渡",
     "camera_en": "Full-body shot, head to toe, three-quarter front view from her front-right at 45 degrees, camera at eye level, entire body visible, vertical composition"},
    {"key": "right", "label": "右侧面", "group": "环绕",
     "framing": "单人全身像，头顶到脚完整入镜，竖构图",
     "camera": "正右侧面平视：相机与人物眼睛等高、水平对准人物，完整呈现人物右侧的侧面轮廓",
     "camera_en": "Full-body shot, head to toe, straight right-side profile view, camera at eye level, entire body visible, vertical composition"},
    {"key": "back_right_45", "label": "右后45°", "group": "环绕",
     "framing": "单人全身像，头顶到脚完整入镜，竖构图",
     "camera": "右后方45度平视：相机与人物眼睛等高，从人物右后方斜向拍摄，看到人物背影与右侧肩背（不露脸）",
     "camera_en": "Full-body shot, head to toe, three-quarter rear view from her back-right at 45 degrees, camera behind her, seeing her back and right shoulder, face not visible, vertical composition"},
    {"key": "back", "label": "背后", "group": "环绕",
     "framing": "单人全身背影，头顶到脚完整入镜，竖构图",
     "camera": "正后方平视：相机在人物正后方、与眼睛等高、水平拍摄，只看到人物后脑、后背与全身背影，头朝前方不回转，看不到脸",
     "camera_en": "Full-body rear view, seen from directly behind her, camera at eye level behind the person, her head faces away from the camera and does not turn around, only the back of her head, hair, back and entire back-facing body are visible, face completely unseen, no facial features, vertical composition, empty plain background with no other person or creature"},
    {"key": "back_left_45", "label": "左后45°", "group": "环绕",
     "framing": "单人全身像，头顶到脚完整入镜，竖构图",
     "camera": "左后方45度平视：相机与人物眼睛等高，从人物左后方斜向拍摄，看到人物背影与左侧肩背，头朝前方不回转（不露脸）",
     "camera_en": "Full-body three-quarter rear view from her back-left at 45 degrees, camera behind her, her head faces away and does not turn around, seeing her back and left shoulder, face not visible, vertical composition"},
    {"key": "left", "label": "左侧面", "group": "环绕",
     "framing": "单人全身像，头顶到脚完整入镜，竖构图",
     "camera": "正左侧面平视：相机与人物眼睛等高、水平对准人物，完整呈现人物左侧的侧面轮廓",
     "camera_en": "Full-body shot, head to toe, straight left-side profile view, camera at eye level, entire body visible, vertical composition"},
    {"key": "front_left_45", "label": "左前45°", "group": "环绕",
     "framing": "单人全身像，头顶到脚完整入镜，竖构图",
     "camera": "左前方45度平视：相机与人物眼睛等高，从人物左前方斜向拍摄，同时看到正面与左侧的过渡",
     "camera_en": "Full-body shot, head to toe, three-quarter front view from her front-left at 45 degrees, camera at eye level, entire body visible, vertical composition"},
    {"key": "top", "label": "俯视", "group": "附加",
     "framing": "单人全身俯视像，人物直立、头顶在上、脚在下（正立不翻转），头顶到脚完整入镜，竖构图",
     "camera": ("俯视高机位：相机高于人物、位于人物斜前上方约45度向下俯拍，人物直立、完整居中；"
                "能略微看到头顶与肩膀的俯视感、脚下地面可见，因透视头部略大、腿部略小；是明显的俯视，画面不翻转"),
     "camera_en": ("Full-body high-angle hero shot from about 45 degrees above, camera elevated in front-above looking down at her, "
                   "she is standing upright and fully visible head to toe with head at top and feet at bottom of the frame (upright, not flipped), floor visible beneath her, centered, vertical composition")},
    {"key": "bottom", "label": "仰视(脚底往上)", "group": "附加",
     "framing": "单人全身仰视像，人物直立、头顶在上、脚在下（正立不翻转），头顶到脚完整入镜，竖构图",
     "camera": ("仰视低机位：相机低于人物、位于人物斜前方约膝盖到腰部高度向上仰拍约45度，人物正面朝向镜头、完整居中；"
                "有明显由下向上的仰视感，因透视腿脚略大、头部略小；绝不是平视，画面不翻转"),
     "camera_en": ("Full-body low-angle hero shot from about 45 degrees below, camera lowered to knee-to-waist height in front of her looking up, "
                   "she stands upright facing the camera, fully visible head to toe with head at top and feet at bottom of the frame (upright, not flipped), centered, strong upward perspective, vertical composition")},
]

VIEWS_BY_KEY = {v["key"]: v for v in VIEWS}
VIEW_KEYS = [v["key"] for v in VIEWS]
DEFAULT_VIEWS = list(_TURNAROUND)
BACK_KEYS = {"back", "back_right_45", "back_left_45"}

# 多视角一致性：只强调「同一个人、同一套外观」，但**绝不**提「多张/图组/转盘/设计页」——
# 那类词会诱导模型在一张图里画出多个角度（拼成联系表），这是之前翻车的根因。
_LOOK = "画面中只有这一个人物，保持与其它视角完全相同的人物外观、发型发色、服装样式、配色与整体画风"
_COUNT_RULE = "整张图只画这一个人，单人、单个完整身体，画面里不要出现重复或多个人物"
# 强全身构图线索（英文对底模更有效）：远景全身 + 站姿 + 头顶/脚下留白，整个人占画面中段。
# 用「full-length wide shot / 全身占画面中段」这类标准参考图措辞比「连脚都要」更稳（后者会把
# 底模带偏成只拍下半身）。逐视角置顶，减少「半身/胸像/局部」概率。
_FULLBODY_EN = "full-length wide shot, standing pose, entire figure visible from head to toe with headroom above the head and floor below the feet, zoomed out so the whole body fits in frame"

# 主提示词里凡是「镜头朝向/景别/构图」相关的词，逐视角出图时都必须剥离，改由该视角自己的
# 朝向 + 统一全身构图决定；否则这些正面词会与俯视/仰视/背面冲突（把视角拽回半身平视）。
_CONFLICT_PAT = re.compile(
    r"(三分之四|四分之三|3/4|3\/4|正侧面|侧面|侧脸|正脸|半侧脸|回眸|半身|上半身|胸像|全身|特写|头像|立绘|"
    r"正面|背面|背后|右侧|左侧|平视|俯视|仰视|低角度|高角度|机位|视角|构图|镜头|景别|"
    r"中景|近景|远景|对称|居中构图|居中|"
    r"\b(?:shot|shooting|view|angle|composition|camera|framing|close[- ]?up|portrait|"
    r"three[- ]?quarter(?:s)?|full[- ]?body|upper[- ]?body|half[- ]?body|"
    r"centered|center[- ]?framed|front|rear|side|back|from\s+below|from\s+above|"
    r"low[- ]?angle|high[- ]?angle|bird'?s[- ]?eye|worm'?s[- ]?eye|depth[- ]?of[- ]?field|bokeh)\b|"
    r"[a-z]+[- ]+of[- ]+(?:field|view|shot))",
    re.IGNORECASE,
)


_ORPHAN_MODIFIERS = {
    "slightly", "shallow", "soft", "sharp", "subtle", "centered", "cinematic",
    "low", "high", "wide", "close", "full", "half", "upper", "semi", "clean", "crisp", "the",
}

# 背面视角额外要剥离的「脸部/容貌」描写：留着会被底模拉出侧脸/回头。
_FACE_PAT = re.compile(
    r"(五官|面部|面容|容貌|脸蛋|脸部|脸庞|肌肤|妆容|美妆|精致像|颜值|美貌|美人痣|"
    r"facial|face|complexion|cheeks?\b)",
    re.IGNORECASE,
)


def _strip_face_terms(text: str) -> str:
    """删除与脸/容貌相关的短语（背面视角专用，避免模型画出侧脸或回头）。"""
    if not text:
        return ""
    parts = re.split(r"[，,、;；]", text)
    keep: list[str] = []
    for p in parts:
        if _FACE_PAT.search(p):
            continue
        keep.append(p)
    return "，".join(x for x in keep if x.strip()).strip("，, 、;；")


def _strip_conflicts(text: str) -> str:
    """把主体描述里与「镜头朝向/景别/构图」相关的短语整段删掉，只留下人物本身的描述。
    以逗号分段处理：删掉命中冲突词后的空段/孤立修饰词段，避免留下「slightly , shallow ,」这类碎片。"""
    if not text:
        return ""
    parts = re.split(r"[，,、;；]", text)
    keep: list[str] = []
    for p in parts:
        s = _CONFLICT_PAT.sub("", p)
        s = re.sub(r"\s{2,}", " ", s).strip().strip("-./ ")
        if not s:
            continue
        if s.lower() in _ORPHAN_MODIFIERS:
            continue
        keep.append(s)
    return "，".join(keep).strip("，, 、;；")


def view_catalog() -> list[dict[str, str]]:
    """给前端用的视角目录（去掉 camera 长描述，保留 key/label/group）。"""
    return [{"key": v["key"], "label": v["label"], "group": v["group"]} for v in VIEWS]


def normalize_views(views) -> list[str]:
    """把用户/模型给的视角选择规整成合法 key 列表（去重、保持顺序、忽略未知项）。
    - None / 空 → 返回空列表（表示不放多视角）
    - 字符串 "front,left" → 拆分
    - 特殊值 "all" / ["all"] / "all8" → 标准 8 方位
    - 特殊值 "turnaround" → 标准 8 方位
    """
    if not views:
        return []
    if isinstance(views, str):
        items = [s.strip() for s in views.replace("；", ",").replace(" ", ",").split(",") if s.strip()]
    elif isinstance(views, (list, tuple)):
        items = [str(s).strip() for s in views if str(s).strip()]
    else:
        return []
    low = [s.lower() for s in items]
    if any(s in ("all", "all8", "turnaround", "全部", "八方", "环绕") for s in low):
        return list(DEFAULT_VIEWS)
    out: list[str] = []
    for s in items:
        k = s.lower()
        if k in VIEWS_BY_KEY and k not in out:
            out.append(k)
    return out


def build_view_prompt(base: str, view_key: str | None, has_reference: bool = False) -> str:
    """把「总提示词」重构成**该视角专用**的提示词：置顶英文相机+构图指令扭转朝向，
    剥离冲突的镜头词，只输出这一个人、这一个视角、统一全身构图。
    """
    if not view_key:
        return base
    v = VIEWS_BY_KEY.get(view_key)
    if v is None:
        return base
    subject = _strip_conflicts((base or "").strip())
    if view_key in BACK_KEYS:
        subject = _strip_face_terms(subject)  # 背面：去掉五官/脸部描述，避免画出侧脸
    subject_part = f"人物外观：{subject}。" if subject else ""
    ref = "参考图提供的是同一个人物，务必保持人物一致。" if has_reference else ""
    cam_en = v.get("camera_en", "")
    return (
        f"{cam_en}，{_FULLBODY_EN}。单张画面，只有一个角度、只画这一个人。"
        f"{_COUNT_RULE}。"
        f"{v['camera']}。{v['framing']}。"
        f"{subject_part}"
        f"{_LOOK}。"
        f"{ref}"
    )


# 背面视角的负面词：仅靠正向「看不到脸」常被底模忽略（实测仍画正面美女人脸），
# 必须在**负面提示词**里显式排除脸/五官/正面/看镜头，才能真正拍出背影。
_BACK_NEG = ("脸，面部，五官，正脸，侧脸，侧脸轮廓，脸颊，脸部轮廓，耳朵，回头，转头，看镜头，正对镜头，"
             "face, facial features, front view, profile face, side face, side profile, cheek, ear, "
             "looking at camera, facing viewer, head turned to camera")

# 全身构图负面词：底模对「全身」正向词不敏感，常把人物裁成半身/胸像（实测背面常只到臀部）。
# 在负面词里排除「特写/半身/裁切腿脚」能显著提高全身入镜率。
_FULLBODY_NEG = ("特写，半身，胸像，裁切，腿部被裁，脚部被裁，人物出框，"
                 "close-up, bust portrait, half-body, cropped legs, cropped feet, body out of frame, cut off")

# 单人负面词：底模在低角度/俯仰视角常脑补出「摄影师/相机/第二个人/人群」，必须显式排除。
_SOLO_NEG = ("两个人物，多个人，第二个人，摄影师，摄像师，摄影机，相机，摄影设备，他人，路人，人群，"
             "怪物，怪兽，生物，人形，巨物，雕像，剪影人物，"
             "two people, multiple people, second person, photographer, camera, cameraman, crowd, extra person, another person, "
             "creature, monster, giant figure, silhouette figure, statue, other characters")


def build_view_negative(base_negative: str, view_key: str | None) -> str:
    """逐视角负面词：所有视角追加「排除半身/裁切(强制全身) + 排除第二人/相机」；
    背面三个视角再追加「排除脸/正面」。正面/侧面等其它视角不排除脸（本就该露脸）。"""
    if not view_key:
        return base_negative or ""
    b = (base_negative or "").strip().strip(",").strip()
    extra = _FULLBODY_NEG + "，" + _SOLO_NEG
    if view_key in BACK_KEYS:
        extra += "，" + _BACK_NEG
    return (f"{b}, {extra}" if b else extra).strip(", ")


# 兼容旧调用名（工作台 / 对话工具此前都用 augment_prompt）
def augment_prompt(base: str, view_key: str | None, has_reference: bool = False) -> str:
    return build_view_prompt(base, view_key, has_reference=has_reference)


def view_label(view_key: str | None) -> str:
    v = VIEWS_BY_KEY.get(view_key or "")
    return v["label"] if v else ""
