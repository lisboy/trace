// 循迹桌宠 · 2D 渲染层（白狐「霜尾」三形态 + 分部位互动 + 动作库）
//
// 形态素材：真实立绘（透明底 PNG），每个形态一对「睁眼 / 闭眼」帧，
// 靠交替两帧实现真正的眨眼，而不是缩放硬压出来。素材源图在
// assets/art/character/pet4-*（成女/兽形）与 pet3-teen-*（少女），
// 抠图后输出成 renderer/assets/pet2d-*.png，睁闭两帧由 align_frames.py
// 对齐到同一张画布（尺寸严格一致），所以眨眼不会跳位。
//
// 三形态的头身比是分开做的（用户给的三联参考图）：
//   成女 7.5 头身（御姐）、少女 5.5 头身（二十岁上下）、兽形是坐姿幼狐（奶音）。
//
// 角色名：霜尾。
//
// 互动：点身体不同部位 → 不同台词 / 情绪 / 配音 / 动作组合。
//   耳 / 头 / 脸 / 身 / 尾 / 脚，每处 2 句台词，成女与少女共用部位、台词分套。
//
// 动作：flat 立绘只有整体变换可用，所以「丰富」靠片段叠加实现——
//   每次点击播 2~3 个动作片段（跳、压扁回弹、摇头、别脸、缩肩、甩尾…），
//   空闲时每 3~7 秒自动来一个待机小动作（耳朵抖、左右看、伸懒腰、打个哈欠…）。
//
// 交互与旧版一致：点穿桌面、按住拖动、双击转圈、右键菜单、1/2/3 切形态。
const api = window.pet ?? {
  hover: () => {},
  dragStart: () => {},
  dragEnd: () => {},
  menu: () => {},
  getState: async () => ({ form: "teen", voice: true }),
  onAction: () => {},
  onScale: () => {},
  onForm: () => {},
  onDragCancel: () => {},
  onDragMoved: () => {},
  reportHitRegion: () => {},
  onHoverChanged: () => {},
};


/** 角色名：气泡、菜单、窗口标题统一用它 */
const PET_NAME = "霜尾";

const petEl = document.getElementById("pet");
const bubbleEl = document.getElementById("bubble");
const ringEl = document.getElementById("ring");
const emoteEl = document.getElementById("emote");
const fxEl = document.getElementById("fx");
const hintEl = document.getElementById("hint");

/* ---------------------------- 形态定义 ---------------------------- */
const FORMS = {
  woman: {
    label: "成女",
    open: "assets/pet2d-woman-open_v1.png",
    closed: "assets/pet2d-woman-closed_v1.png",
    // 反应帧：同一角色的「捂头」「捂脸」「护尾」「蹲下抱膝（踩脚）」姿势。全部帧由 build_poses.py
    // 对齐到同一张画布（成女 439×1005），所以换帧不会跳大小、不会跳位；
    // 蹲姿帧是按 FRAME_H_RATIO 缩到站姿 0.58 高再贴底边摆的，脚踩在地上、人蹲着，不会顶到画面顶部。
    posehead: "assets/pet2d-woman-posehead_v1.png",
    poseface: "assets/pet2d-woman-poseface_v1.png",
    posetail: "assets/pet2d-woman-posetail_v1.png",
    posefeet: "assets/pet2d-woman-posefeet_v1.png",
    bubbleTop: 6,
    /** 御姐的低音，念同一句更冷 */
    voicePitch: 1,
  },
  teen: {
    label: "少女",
    open: "assets/pet2d-teen-open_v1.png",
    closed: "assets/pet2d-teen-closed_v1.png",
    posehead: "assets/pet2d-teen-posehead_v1.png",
    poseface: "assets/pet2d-teen-poseface_v1.png",
    posetail: "assets/pet2d-teen-posetail_v1.png",
    posefeet: "assets/pet2d-teen-posefeet_v1.png",
    bubbleTop: 6,
    voicePitch: 1,
  },
  beast: {
    label: "兽形",
    open: "assets/pet2d-beast-open_v1.png",
    closed: "assets/pet2d-beast-closed_v1.png",
    posehead: "assets/pet2d-beast-posehead_v1.png",
    poseface: "assets/pet2d-beast-poseface_v1.png",
    posetail: "assets/pet2d-beast-posetail_v1.png",
    bubbleTop: 6,
    voicePitch: 1,
  },
};
const FORM_ORDER = ["woman", "teen", "beast"];

/* ---------------------------- 部位分区 ---------------------------- */
/**
 * 分区用「立绘内的归一化坐标」(u=横向 0~1, v=纵向 0~1, v=0 在图顶)。
 * 只有先通过 alpha 命中（指针真在身体像素上）才会走到这里，
 * 所以分区表只要大致圈出范围就行，不需要精确到轮廓。
 * 边界不是拍的：用 alpha 通道逐行量过，每块都 ≥23% 有像素
 * （.agent/tmp/final_regions.py 可复算），旧表那对 u[0.68,1] 的「右耳」
 * 完全落在透明区、点了没反应，已按实测改掉。
 * 顺序即优先级：先窄后宽。
 */
const REGIONS = {
  // 坐标是量出来的：网格图 .trace/shots/grid-*.png 逐格核对 +
  // alpha 行剖面（.agent/tmp/profile_runs.py，看每行不透明段的起止）+
  // 分区落点自检（.agent/tmp/check_region_map.py 把本表叠到掩码上出 ASCII 图）。
  // 上一版的坑：head 被 ear/face 完全包住，覆盖率 0%（点头顶永远判成耳朵），
  // 现按「耳尖是独立不透明段」的真实行数据重划，head 必须是有面积的一块。
  woman: [
    // 耳尖：v<0.11 处 alpha 是两个独立段 u[0.25,0.32] / u[0.47,0.55]
    { part: "ear", v0: 0, v1: 0.11, u0: 0.2, u1: 0.36 },
    { part: "ear", v0: 0, v1: 0.11, u0: 0.43, u1: 0.6 },
    // 头顶（摸头区）：耳之间的颅顶
    { part: "head", v0: 0, v1: 0.14, u0: 0.24, u1: 0.58 },
    // 脸：眉毛到下巴
    { part: "face", v0: 0.14, v1: 0.3, u0: 0.15, u1: 0.72 },
    // 尾巴：右侧蓬松大尾，v 0.48~0.80 u>0.74（行剖面 v=0.52 起 u1 冲到 0.99）
    { part: "tail", v0: 0.48, v1: 0.82, u0: 0.74, u1: 1 },
    { part: "feet", v0: 0.88, v1: 1, u0: 0, u1: 1 },
    { part: "body", v0: 0.3, v1: 0.92, u0: 0, u1: 1 },
  ],
  teen: [
    // 耳尖：v=0.037 行是两个独立段 u[0.12,0.18] / u[0.44,0.54]
    { part: "ear", v0: 0, v1: 0.1, u0: 0.1, u1: 0.32 },
    { part: "ear", v0: 0, v1: 0.1, u0: 0.4, u1: 0.6 },
    { part: "head", v0: 0, v1: 0.15, u0: 0.16, u1: 0.56 },
    { part: "face", v0: 0.15, v1: 0.3, u0: 0.1, u1: 0.7 },
    { part: "tail", v0: 0.48, v1: 0.86, u0: 0.7, u1: 1 },
    { part: "feet", v0: 0.86, v1: 1, u0: 0, u1: 1 },
    { part: "body", v0: 0.3, v1: 0.88, u0: 0, u1: 1 },
  ],
  // 坐姿幼狐：大耳朵占上方 16%（左 u0.03~0.30 / 右 u0.46~0.72，v=0.163 才连成一片），
  // 颅顶 v0.14~0.28，脸（眼鼻）v0.28~0.52；尾巴是右侧一大团；前爪 v>0.83 中间三块。
  // 注意顺序：尾巴必须在 feet 之前（右下角两者重叠），feet 必须在 face 之前，
  // 否则掌垫会被判成脸——上一版就栽在这（脚垫被划进 face）。
  beast: [
    { part: "ear", v0: 0, v1: 0.16, u0: 0.02, u1: 0.31 },
    { part: "ear", v0: 0, v1: 0.16, u0: 0.44, u1: 0.74 },
    { part: "head", v0: 0.15, v1: 0.28, u0: 0.02, u1: 0.75 },
    { part: "face", v0: 0.28, v1: 0.52, u0: 0.08, u1: 0.68 },
    { part: "tail", v0: 0.5, v1: 1, u0: 0.7, u1: 1 },
    { part: "feet", v0: 0.83, v1: 1, u0: 0.04, u1: 0.7 },
    { part: "body", v0: 0.52, v1: 0.84, u0: 0, u1: 0.7 },
  ],
};


/** 悬停时显示的部位名（比 "ear" 这种内部 key 友好） */
const PART_LABEL = {
  ear: "耳朵",
  head: "头顶",
  face: "脸",
  body: "身体",
  tail: "尾巴",
  feet: "脚",
};


function partAt(form, u, v) {
  const list = REGIONS[form] || REGIONS.teen;
  for (const r of list) {
    if (v < r.v0 || v >= r.v1) continue;
    if (r.u0 !== undefined && (u < r.u0 || u > r.u1)) continue;
    return r.part;
  }
  return "body";
}

/* ---------------------------- 动作片段库 ---------------------------- */
/**
 * flat 立绘只有整体变换可用，所以「动作」= 若干变换片段的叠加。
 * 每个片段 fn(p, o, amp)：p 是 0→1 的进度，o 是累加器：
 *   o.dx/o.dy  位移（dy 负 = 上浮）
 *   o.rot      平面旋转（度）
 *   o.rotY     伪 3D 转身/别脸（度）
 *   o.sx/o.sy  缩放
 * 点击时一次叠 2~3 个片段，就比「只弹一下」丰富得多。
 */
const env = (p, a = 0.18, b = 0.24) => (p < a ? p / a : p > 1 - b ? (1 - p) / b : 1);

const CLIPS = {
  /** 小跳 */
  hop(p, o, a) {
    const s = Math.sin(p * Math.PI);
    o.dy -= s * 16 * a;
    o.sy += s * 0.02 * a;
  },
  /** 连跳两下（被踩到时用） */
  hopDouble(p, o, a) {
    o.dy -= Math.abs(Math.sin(p * Math.PI * 2)) * 17 * a;
  },
  /** 落地压扁再回弹 */
  squash(p, o, a) {
    const s = Math.sin(Math.min(p * 3, 1) * Math.PI);
    o.sy -= s * 0.06 * a;
    o.sx += s * 0.05 * a;
  },
  /** 摇头（否定 / 不情愿） */
  shakeHead(p, o, a) {
    o.rot += Math.sin(p * Math.PI * 5) * (1 - p) * 9 * a;
  },
  /** 高冷：别过脸 hold 一下再回正 */
  tiltCool(p, o, a) {
    const e = env(p, 0.2, 0.28);
    o.rotY -= 24 * a * e;
    o.rot -= 4 * a * e;
  },
  /** 害羞：整个人缩一圈 */
  shyShrink(p, o, a) {
    const e = env(p, 0.22, 0.3);
    o.sy -= 0.07 * a * e;
    o.sx -= 0.02 * a * e;
    o.rotY += 12 * a * e;
    o.dy += 2 * a * e;
  },
  /** 甩尾/抖身：由慢到快再收 */
  wag(p, o, a) {
    o.rot += Math.sin(p * Math.PI * 7) * (1 - p) * 3.4 * a;
  },
  /** 耳朵抖：高频小幅 */
  earFlap(p, o, a) {
    o.rot += Math.sin(p * Math.PI * 12) * (1 - p) * 2.6 * a;
    o.sy += Math.sin(p * Math.PI) * 0.012 * a;
  },
  /** 转圈（双击） */
  spin(p, o, a) {
    o.rotY += p * 360 * a;
  },
  /** 伸懒腰 */
  stretch(p, o, a) {
    const s = Math.sin(p * Math.PI);
    o.sy += s * 0.07 * a;
    o.dy -= s * 6 * a;
  },
  /** 弹性回弹 */
  boing(p, o, a) {
    o.sy += Math.sin(p * Math.PI * 3) * (1 - p) * 0.08 * a;
  },
  /** 探头往一边看 */
  peek(p, o, a) {
    const e = env(p, 0.15, 0.3);
    o.rotY -= 20 * a * e;
    o.dx -= 5 * a * e;
  },
  /** 点头 */
  nod(p, o, a) {
    o.dy += Math.sin(p * Math.PI * 3) * (1 - p) * 4 * a;
  },
  /** 踢腿（用整体侧倾近似） */
  kick(p, o, a) {
    const s = Math.sin(p * Math.PI);
    o.rot -= s * 7 * a;
    o.dy -= s * 4 * a;
  },
  /** 被捏到的颤 */
  flutter(p, o, a) {
    o.rot += Math.sin(p * Math.PI * 18) * (1 - p) * 1.8 * a;
  },
  /** 鼓起（生气） */
  puff(p, o, a) {
    const s = Math.sin(p * Math.PI);
    o.sx += s * 0.05 * a;
    o.sy += s * 0.03 * a;
  },
  /** 后仰（被吓到） */
  leanBack(p, o, a) {
    const s = Math.sin(p * Math.PI);
    o.rotY += s * 14 * a;
    o.rot += s * 3 * a;
  },
  /** 重心左右挪 */
  sway(p, o, a) {
    o.rot += Math.sin(p * Math.PI * 2) * 5 * a;
  },
  /** 打哈欠：拉长 + 微微后仰 */
  yawn(p, o, a) {
    const s = Math.sin(p * Math.PI);
    o.sy += s * 0.045 * a;
    o.rotY += s * 8 * a;
    o.dy -= s * 3 * a;
  },
  /** 蹭蹭（贴过来） */
  nuzzle(p, o, a) {
    const s = Math.sin(p * Math.PI * 2) * (1 - p);
    o.rotY += s * 16 * a;
    o.dx += s * 4 * a;
  },
};

/**
 * 待机小动作：每 3~7 秒随机来一个，让桌宠「活着」。
 * 不带台词、不带配音，纯动作。
 */
const IDLE_CLIPS = [
  { clips: ["earFlap"], dur: 0.6 },
  { clips: ["peek"], dur: 1.2 },
  { clips: ["sway"], dur: 1.6 },
  { clips: ["hop"], dur: 0.5 },
  { clips: ["stretch"], dur: 1.1 },
  { clips: ["yawn"], dur: 1.3 },
  { clips: ["wag"], dur: 0.9 },
  { clips: ["nuzzle"], dur: 1.0 },
  { clips: ["boing"], dur: 0.6 },
  { clips: ["nod"], dur: 0.7 },
];

/* ---------------------------- 台词表 ---------------------------- */
/**
 * 每句 = { mood, text, voice, emote, anim }。
 * mood 决定气泡配色 + 头顶表情 + 飘出的粒子：
 *   cute 卖萌 / cool 高冷 / angry 生气 / shy 害羞 / surprise 惊讶
 * voice 是 renderer/assets/voice/*.wav 的文件名。三套音色：
 *   wom-* 御姐低音（F0≈178Hz） / teen-* 成年女中音（F0≈205Hz） / bst-* 幼崽奶音（F0≈300Hz）
 * anim 是点击后播的动作片段序列（见 CLIPS）。
 */
const LINES = {
  woman: {
    ear: [
      { mood: "shy", text: "耳朵可不能随便碰。……算了，这次就当没看见。", voice: "wom-ear1", emote: "❗", anim: ["earFlap", "shyShrink"] },
      { mood: "cool", text: "再摸一次试试？我可不保证还这么客气。", voice: "wom-ear2", emote: "💢", anim: ["earFlap", "tiltCool"] },
    ],
    head: [
      { mood: "cute", text: "摸头。嗯……手法勉强及格。", voice: "wom-head1", emote: "❤", anim: ["hop", "squash"] },
      { mood: "cool", text: "头发乱了。你赔得起吗？", voice: "wom-head2", emote: "…", anim: ["shakeHead", "leanBack"] },
    ],
    face: [
      { mood: "angry", text: "戳脸？你当我是小孩吗。", voice: "wom-face1", emote: "💢", anim: ["shakeHead", "kick"] },
      { mood: "shy", text: "看脸不如看屏幕。你的活还没干完。", voice: "wom-face2", emote: "😳", anim: ["shyShrink", "peek"] },
    ],
    body: [
      { mood: "angry", text: "腰是禁区。手，拿开。", voice: "wom-body1", emote: "💢", anim: ["puff", "shakeHead"] },
      { mood: "shy", text: "别捏。我没那么软。", voice: "wom-body2", emote: "😖", anim: ["shyShrink", "flutter"] },
    ],
    tail: [
      { mood: "surprise", text: "尾巴是我的。碰一下，收一次费。", voice: "wom-tail1", emote: "❗", anim: ["wag", "hop"] },
      { mood: "cool", text: "轻点抓。它比你手头那个项目值钱。", voice: "wom-tail2", emote: "…", anim: ["tiltCool", "wag"] },
    ],
    feet: [
      { mood: "cute", text: "脚也点？你是真的闲。", voice: "wom-feet1", emote: "😣", anim: ["hopDouble", "kick"] },
      { mood: "cool", text: "站了一天。累。别碰。", voice: "wom-feet2", emote: "…", anim: ["tiltCool", "boing"] },
    ],
  },
  teen: {
    ear: [
      { mood: "shy", text: "呀——别碰耳朵啦，会痒的。", voice: "teen-ear1", emote: "❗", anim: ["earFlap", "shyShrink"] },
      { mood: "cool", text: "耳朵不许碰。这是规矩。", voice: "teen-ear2", emote: "💢", anim: ["earFlap", "tiltCool"] },
    ],
    head: [
      { mood: "cute", text: "嗯……被摸到耳朵根了。还挺舒服。", voice: "teen-head1", emote: "❤", anim: ["hop", "squash"] },
      { mood: "cool", text: "轻一点。头发乱了我不负责。", voice: "teen-head2", emote: "…", anim: ["shakeHead", "leanBack"] },
    ],
    face: [
      { mood: "angry", text: "戳脸要收钱的，你知道吗。", voice: "teen-face1", emote: "💢", anim: ["shakeHead", "kick"] },
      { mood: "shy", text: "脸上没字。别一直盯着看。", voice: "teen-face2", emote: "😳", anim: ["shyShrink", "peek"] },
    ],
    body: [
      { mood: "angry", text: "喂——不许挠肚子。我要生气了。", voice: "teen-body1", emote: "💢", anim: ["puff", "shakeHead"] },
      { mood: "shy", text: "身上没肉。不许捏，真没肉。", voice: "teen-body2", emote: "😖", anim: ["shyShrink", "flutter"] },
    ],
    tail: [
      { mood: "surprise", text: "尾巴不能抓，会炸毛的。", voice: "teen-tail1", emote: "❗", anim: ["wag", "hop"] },
      { mood: "cool", text: "别动我的尾巴。它比你还怕生。", voice: "teen-tail2", emote: "…", anim: ["tiltCool", "wag"] },
    ],
    feet: [
      { mood: "cute", text: "踩到我的脚了……走不动了。", voice: "teen-feet1", emote: "😣", anim: ["hopDouble", "kick"] },
      { mood: "cool", text: "连脚都点。你是真够闲的。", voice: "teen-feet2", emote: "…", anim: ["tiltCool", "boing"] },
    ],
  },
  beast: {
    ear: [
      { mood: "angry", text: "嗷呜——耳朵不能摸啦，会痒。", voice: "bst-ear1", emote: "❗", anim: ["earFlap", "shakeHead"] },
      { mood: "angry", text: "哼……耳朵不给摸，再摸就不理你了。", voice: "bst-ear2", emote: "💢", anim: ["earFlap", "puff"] },
    ],
    head: [
      { mood: "cute", text: "唔……摸摸头，好舒服。", voice: "bst-head1", emote: "❤", anim: ["hop", "squash"] },
      { mood: "cute", text: "唔嗯……好暖。再摸一下下。", voice: "bst-head2", emote: "❤", anim: ["nuzzle", "boing"] },
    ],
    face: [
      { mood: "surprise", text: "脸上……有什么东西吗？", voice: "bst-face1", emote: "😳", anim: ["peek", "boing"] },
      { mood: "surprise", text: "呜？为什么一直看我呀……", voice: "bst-face2", emote: "❗", anim: ["peek", "shyShrink"] },
    ],
    body: [
      { mood: "cute", text: "咕噜噜……肚皮被摸到了。", voice: "bst-body1", emote: "❤", anim: ["puff", "wag"] },
      { mood: "cute", text: "呼噜呼噜……肚皮只给你摸。", voice: "bst-body2", emote: "❤", anim: ["nuzzle", "wag"] },
    ],
    tail: [
      { mood: "angry", text: "嗷呜！尾巴生气了，不理你了！", voice: "bst-tail1", emote: "💢", anim: ["wag", "shakeHead"] },
      { mood: "cute", text: "我的尾巴，超级蓬松吧！", voice: "bst-tail2", emote: "✨", anim: ["wag", "hop"] },
    ],
    feet: [
      { mood: "angry", text: "呜——脚垫不许碰啦！", voice: "bst-feet1", emote: "💢", anim: ["kick", "shakeHead"] },
      { mood: "cute", text: "呀！踩到我了……疼疼疼。", voice: "bst-feet2", emote: "😣", anim: ["hopDouble", "kick"] },
    ],
  },
};

/** 三形态各自的打招呼台词与配音 */
const GREETINGS = {
  woman: [
    { text: "我在。要做什么，说吧。", voice: "wom-greet1" },
    { text: "又卡住了？拿来看看。", voice: "wom-greet2" },
  ],
  teen: [
    { text: "我在呢，随时可以开工。", voice: "teen-greet1" },
    { text: "任务跑完我会提醒你。", voice: "teen-greet2" },
  ],
  beast: [
    { text: "我在呢。你回来啦。", voice: "bst-greet1" },
    { text: "唔……好困。你也要休息呀。", voice: "bst-greet2" },
  ],
};


const MOOD_FX = {
  cute: ["💗", "✨", "💕"],
  cool: ["❄", "…", "🫧"],
  angry: ["💢", "🔥", "💥"],
  shy: ["😳", "🌸", "💗"],
  surprise: ["❗", "💥", "✨"],
};

const imgCache = {}; // form -> { open: Image, closed: Image }
let currentForm = "teen";
let soundOn = true;

/* ---------------------------- 状态 ---------------------------- */
const state = {
  t: 0,
  blinkAt: 2.2,
  blinkT: 0,
  happyT: 0,
  squash: 0,
  pointerX: 0,
  pointerY: 0,
  hovering: false,
  dragging: false,
  /** 当前鼠标所在的部位（ear/head/face/body/tail/feet），用来做悬停反馈 */
  hoverPart: null,
  downAt: null,
  pressT: 0, // 长按计时（长按 = 揉，不缩放）
  rubbing: false,
  moved: false, // 本次按下期间光标是否真移动过（主进程告知）
  lastClickAt: 0,
  /** 正在播放的动作片段 */
  clips: [],
  /** 下次待机动作的时间 */
  idleAt: 3.5,
  /** 立绘不透明区域掩码：用于判断「指针是否压在宠物身上」 */
  mask: null,
  maskW: 0,
  maskH: 0,
  /** 当前展示的是哪一帧（open/closed/posehead/poseface/posetail），掩码始终按 open 算 */
  frame: "open",
  /** 反应姿势（''=无），poseT 倒数到 0 自动回正 */
  pose: "",
  poseT: 0,
};

/* ---------------------------- 素材加载 ---------------------------- */
/**
 * 素材加载。
 *
 * 为什么要带 cache-busting 查询串：素材是 file:// 下的 PNG，Chromium 会按 URL 做磁盘缓存。
 * 重跑 `blender/build_poses.py` 覆盖同名 PNG 之后，已经开着的桌宠窗口仍会拿着旧图 ——
 * 表现就是「磁盘上的帧已经修好了（白斑已删），界面里还留着一块旧白块」。这个坑在
 * 「抠图还有问题」那一轮实际踩到过：用户在界面上圈出的白斑，在现役 PNG 里早已是透明。
 * 现在每次加载都追加 `?v=<启动时间戳>`，同一个进程内 URL 稳定（不重复解码），
 * 但跨进程一定拿到新文件。配合 `im.src = ""` 释放旧解码位图。
 */
const ASSET_VERSION = Date.now().toString(36);

/** 素材地址统一带上启动时间戳（renderer 里给 <img> 赋值也要用，见 @loadImage 的说明） */
function assetUrl(src) {
  return src + (src.includes("?") ? "&" : "?") + "v=" + ASSET_VERSION;
}

function loadImage(src) {
  const url = assetUrl(src);
  return new Promise((resolve, reject) => {
    const im = new Image();
    im.onload = () => resolve(im);
    im.onerror = () => {
      // 缓存串理论上不影响加载；万一某些环境不认查询串，退回裸地址再试一次，
      // 宁可拿到可能被缓存的旧图，也不要整只宠物加载失败。
      const im2 = new Image();
      im2.onload = () => resolve(im2);
      im2.onerror = () => reject(new Error("加载失败: " + src));
      im2.src = src;
    };
    im.src = url;
  });
}

async function loadAll() {
  await Promise.all(
    FORM_ORDER.map(async (k) => {
      const f = FORMS[k];
      // 五帧一起加载：open/closed 管眨眼，posehead/poseface/posetail 管分部位点击反应。
      // 缺任何一帧都让它直接 reject，宁可报错也不要跑到一半换帧失败。
      const [open, closed, posehead, poseface, posetail] = await Promise.all([
        loadImage(f.open),
        loadImage(f.closed),
        loadImage(f.posehead),
        loadImage(f.poseface),
        loadImage(f.posetail),
      ]);
      // posefeet 是可选帧：兽形没有手，抱不住脚（模型在 denoise 0.88 也改不动坐姿），
      // 所以兽形没有这张图，缺了不影响其它帧，点脚时走动作近似。
      const posefeet = f.posefeet ? await loadImage(f.posefeet).catch(() => null) : null;
      imgCache[k] = { open, closed, posehead, poseface, posetail, posefeet };
    })
  );
}

/* ---------------------------- 立绘布局 ---------------------------- */
/**
 * 按素材实际宽高比设置显示尺寸：高度占窗口 88%，宽度按比例，不裁切不变形。
 * 兽形是横宽的、成女是竖长的，靠这一条统一在同一个窗口里都完整可见。
 */
function layoutPet() {
  const cache = imgCache[currentForm];
  if (!cache) return;
  const im = cache.open;
  const stageH = window.innerHeight;
  const stageW = window.innerWidth;

  let h = stageH * 0.88;
  let w = (im.naturalWidth / im.naturalHeight) * h;
  if (w > stageW * 0.94) {
    w = stageW * 0.94;
    h = (im.naturalHeight / im.naturalWidth) * w;
  }
  petEl.style.height = h + "px";
  petEl.style.width = w + "px";
  petEl.style.maxHeight = "none";

  const ringW = Math.min(stageW * 0.72, w * 0.78);
  ringEl.style.width = ringW + "px";
  ringEl.style.height = Math.max(12, ringW * 0.16) + "px";
  // 立绘矩形变了，命中区域得跟着重报，否则主进程还按旧矩形判命中
  reportHitRegion();
  placeOverlays(state.frame || "open");
}

/**
 * 当前这一帧「头顶」在窗口里的 y 像素。
 *
 * 为什么需要：气泡/情绪符号在 index.html 里是固定 top:8px/2px 的，
 * 站姿帧的头顶刚好贴着画布顶部，所以一直没问题；但踩脚的蹲姿帧只占画布下方
 * 约 58%（见 build_poses.py 的 FRAME_H_RATIO），头顶落在画布中部，气泡和表情
 * 就会浮在头顶上方一大段空白里，看着像「话说在空气里」。这里按当前帧素材的
 * 真实 alpha 顶边算出头顶位置，气泡/表情挂在它上方。
 */
function headTopY(frameName) {
  const cache = imgCache[currentForm];
  if (!cache) return null;
  const im = cache[frameName] || cache.open;
  if (!im || !im.naturalHeight) return null;
  const ratio = (im._topRatio ?? (im._topRatio = measureTopRatio(im)));
  if (ratio == null) return null;
  const stageH = window.innerHeight;
  const dispH = stageH * 0.88; // 与 layoutPet 的立绘高度一致
  const top = stageH - stageH * 0.03 - dispH * (1 - ratio); // 立绘底边在 97% 处
  return Math.max(2, Math.round(top));
}

/** 量一帧素材「第一行不透明像素」在整图中的相对高度（0=顶边，1=底边） */
function measureTopRatio(im) {
  try {
    const c = document.createElement("canvas");
    c.width = im.naturalWidth;
    c.height = im.naturalHeight;
    const ctx = c.getContext("2d");
    ctx.drawImage(im, 0, 0);
    const { data } = ctx.getImageData(0, 0, c.width, c.height);
    for (let y = 0; y < c.height; y++) {
      const row = y * c.width * 4;
      for (let x = 0; x < c.width; x++) {
        if (data[row + x * 4 + 3] > 40) return y / c.height;
      }
    }
  } catch (e) {
    return null;
  }
  return null;
}

/** 按当前帧把气泡/情绪符号挂到头顶上方（站姿/蹲姿都贴着真实头顶，不悬空也不压脸） */
function placeOverlays(frameName) {
  const y = headTopY(frameName);
  if (y == null) return;
  const key = currentForm;
  const base = FORMS[key] ? FORMS[key].bubbleTop : 6;
  // 气泡是「底边贴着头顶上沿」摆放：用气泡自身高度反推 top。
  // 站姿帧头顶本来就在窗口顶部附近（画布头顶留白很小），算出来会小于 base，于是落回 base ——
  // 和改动前的位置一致；蹲姿帧头顶在画布中部，气泡才整体跟着下移。
  const bubbleH = bubbleEl.offsetHeight || 40;
  bubbleEl.style.top = Math.max(base, y - bubbleH - 8) + "px";
  // 情绪符号（30px emoji）挂在头顶正上方一点，别和气泡打架
  emoteEl.style.top = Math.max(2, y - 38) + "px";
}


/** 建立 alpha 掩码（每 4px 采一次，够判定命中且不卡） */
function buildMask() {
  const cache = imgCache[currentForm];
  if (!cache) return;
  const im = cache.open;
  const step = 4;
  const mw = Math.ceil(im.naturalWidth / step);
  const mh = Math.ceil(im.naturalHeight / step);
  const c = document.createElement("canvas");
  c.width = im.naturalWidth;
  c.height = im.naturalHeight;
  const ctx = c.getContext("2d");
  ctx.drawImage(im, 0, 0);
  let data;
  try {
    data = ctx.getImageData(0, 0, c.width, c.height).data;
  } catch (e) {
    // file:// 下 canvas 污染会抛错：退化成整块矩形命中，不影响使用
    state.mask = null;
    return;
  }
  const mask = new Uint8Array(mw * mh);
  for (let y = 0; y < mh; y++) {
    for (let x = 0; x < mw; x++) {
      const px = (y * step * c.width + x * step) * 4 + 3;
      mask[y * mw + x] = data[px] > 40 ? 1 : 0;
    }
  }
  state.mask = mask;
  state.maskW = mw;
  state.maskH = mh;
  reportHitRegion();
}

/**
 * 立绘的「布局矩形」（窗口内 CSS 坐标）。
 * 必须用 offset* 拼，不能用 getBoundingClientRect：后者含 transform
 * （呼吸浮动、侧倾 ±2.6°、动作缩放），会把矩形放大/旋转，
 * 和 alpha 掩码（原图的未变换坐标）对不上，命中就会偏。
 */
function petLayoutRect() {
  const w = petEl.offsetWidth;
  const h = petEl.offsetHeight;
  return { left: petEl.offsetLeft - w / 2, top: petEl.offsetTop, width: w, height: h };
}

/**
 * 把「立绘在窗口内的矩形 + alpha 掩码」上报给主进程。
 * 主进程用屏幕光标 + 这张掩码判断「是不是压在宠物身上」，从而决定窗口穿透与否。
 * 布局变化（换形态、改大小、窗口 resize）后都要重报一次。
 */
function reportHitRegion() {
  const r = petLayoutRect();
  const payload = { rect: r, mask: null };
  if (state.mask) {
    payload.mask = { w: state.maskW, h: state.maskH, data: Array.from(state.mask) };
  }
  api.reportHitRegion(payload);
}



/** 指针命中的归一化坐标；不在身体像素上返回 null */
function hitUV(ev) {
  const r = petLayoutRect();
  if (ev.clientX < r.left || ev.clientX > r.left + r.width || ev.clientY < r.top || ev.clientY > r.top + r.height) {
    return null;
  }
  const u = (ev.clientX - r.left) / r.width;
  const v = (ev.clientY - r.top) / r.height;
  if (!state.mask) return { u, v }; // 掩码不可用时退化为包围盒
  const mx = Math.floor(u * state.maskW);
  const my = Math.floor(v * state.maskH);
  if (mx < 0 || my < 0 || mx >= state.maskW || my >= state.maskH) return null;
  return state.mask[my * state.maskW + mx] === 1 ? { u, v } : null;
}


function pointerOnPet(ev) {
  return hitUV(ev) !== null;
}

/* ---------------------------- 形态切换 ---------------------------- */
let pendingForm = null;

function setForm(key, { silent = false } = {}) {
  if (!FORMS[key]) key = "teen";
  if (!imgCache[key]) {
    pendingForm = key;
    return;
  }
  currentForm = key;
  // 换形态必须强制换图：state.frame 可能本来就是 "open"，只按名字判断会
  // 早退，结果停在上一形态的立绘上。
  state.frame = "";
  clearPose();
  setFrame("open");
  layoutPet();
  buildMask();
  bubbleEl.style.top = FORMS[key].bubbleTop + "px";
  if (!silent) {
    addClip("hop");
    addClip("squash", { delay: 0.12 });
    say(`${FORMS[key].label}形态~`, 1300, "cute", "✨");
  }
}

/* ---------------------------- 反应姿势帧 ---------------------------- */
/**
 * 点不同部位换「整张姿势」——这是用户要的「点她会捂着头、点尾巴她会有明显形态变化」。
 * 姿势帧和常驻帧是同一个角色、同一张画布（build_poses.py 统一对齐），
 * 所以换帧只是换 <img src>，人不会跳位也不会缩放。
 *
 * 命中掩码始终按 open 帧算（buildMask 只看 open）：姿势帧里尾巴被抱起来了，
 * 但「尾巴那块区域」在 2.4 秒内仍然是可点的。这样护尾的瞬间
 * 鼠标不会因为身体像素变了而突然变成穿透，点起来才稳。
 */
function setFrame(name) {
  const f = FORMS[currentForm];
  if (!f || !f[name]) return;
  if (state.frame === name) return;
  state.frame = name;
  petEl.src = assetUrl(f[name]);
  // 换帧可能是蹲姿（头顶比站姿低半截），气泡/表情要跟着头顶走
  placeOverlays(name);
}

/** 播一个反应姿势，ms 后自动回正；该形态没有这张帧（如兽形没 posefeet）就退回常驻帧 */
function showPose(kind, ms = 1500) {
  const f = FORMS[currentForm];
  if (!f || !f[kind]) {
    clearPose();
    return;
  }
  state.pose = kind;
  state.poseT = ms / 1000;
  setFrame(kind);
}

function clearPose() {
  state.pose = "";
  state.poseT = 0;
  // 同时把图换回常驻睁眼帧：否则上一次捂头/捂脸/护尾的姿势帧会一直挂着，
  // 接着点身/脚时画面还是「捂着头」的样子。
  if (state.frame !== "open") setFrame("open");
}


/* ---------------------------- 动作调度 ---------------------------- */
/**
 * 播一个动作片段。delay 让片段接在前一个后面（做成「跳→落地压扁」这类连招）。
 */
function addClip(name, { dur = 0.6, amp = 1, delay = 0 } = {}) {
  if (!CLIPS[name]) return;
  state.clips.push({ name, dur, amp, t: -delay });
}

/** 一次播一整套动作（台词里配的 anim 数组） */
function playAnim(names) {
  let at = 0;
  for (const n of names) {
    addClip(n, { delay: at });
    at += 0.16;
  }
}

/** 推进所有动作片段，把结果累加进 o */
function stepClips(dt, o) {
  for (let i = state.clips.length - 1; i >= 0; i--) {
    const c = state.clips[i];
    c.t += dt;
    if (c.t < 0) continue;
    const p = c.t / c.dur;
    if (p >= 1) {
      state.clips.splice(i, 1);
      continue;
    }
    CLIPS[c.name](p, o, c.amp);
  }
}

/* ---------------------------- 气泡 / 表情 / 粒子 ---------------------------- */
let bubbleTimer = 0;
function say(text, ms = 2200, mood = "cute", emote = "") {
  bubbleEl.textContent = text;
  bubbleEl.dataset.mood = mood;
  bubbleEl.classList.add("show");
  window.clearTimeout(bubbleTimer);
  bubbleTimer = window.setTimeout(() => bubbleEl.classList.remove("show"), ms);
  if (emote) showEmote(emote);
  spawnFx(mood);
}

let emoteTimer = 0;
function showEmote(ch) {
  emoteEl.textContent = ch;
  emoteEl.classList.add("show");
  window.clearTimeout(emoteTimer);
  emoteTimer = window.setTimeout(() => emoteEl.classList.remove("show"), 900);
}

/** 从宠物上半身飘出 3~5 个情绪粒子（纯 emoji，透明窗里不铺底色） */
function spawnFx(mood) {
  const list = MOOD_FX[mood] || MOOD_FX.cute;
  const n = 3 + Math.floor(Math.random() * 3);
  const r = petEl.getBoundingClientRect();
  const baseX = r.left + r.width * (0.32 + Math.random() * 0.36);
  const baseY = r.top + r.height * (0.22 + Math.random() * 0.2);
  for (let i = 0; i < n; i++) {
    const s = document.createElement("span");
    s.className = "p";
    s.textContent = list[Math.floor(Math.random() * list.length)];
    s.style.left = baseX + (Math.random() - 0.5) * r.width * 0.36 + "px";
    s.style.top = baseY + (Math.random() - 0.5) * r.height * 0.1 + "px";
    s.style.setProperty("--dx", (Math.random() - 0.5) * 44 + "px");
    s.style.setProperty("--dy", -(40 + Math.random() * 50) + "px");
    s.style.setProperty("--rot", (Math.random() - 0.5) * 60 + "deg");
    s.style.animationDelay = i * 60 + "ms";
    fxEl.appendChild(s);
    window.setTimeout(() => s.remove(), 1100 + i * 60);
  }
}

/* ---------------------------- 配音 ---------------------------- */
/**
 * 三形态三套音色，已用基频（F0）逐条归一：
 *   成女 wom-*  ≈178Hz（御姐低音）
 *   少女 teen-* ≈205Hz（成年女中音，二十岁上下）
 *   兽形 bst-*  ≈300Hz（幼崽奶音）
 * 归一化脚本：.agent/tmp/pitch_normalize.py（ffmpeg asetrate+atempo，只变调不变速）。
 */
let audio = null;
function playVoice(name) {
  if (!soundOn || !name) return;
  try {
    if (audio) {
      audio.pause();
      audio.currentTime = 0;
    }
    audio = new Audio(`assets/voice/${name}.wav`);
    audio.volume = 0.95;
    const p = audio.play();
    if (p && p.catch) p.catch(() => {});
  } catch (e) {
    /* 播放失败不影响互动 */
  }
}

/* ---------------------------- 反应 ---------------------------- */
const lineCursor = {}; // form:part -> 上次用到第几句，保证连点不会连续重复同一句
let greetingCursor = 0;

function react(part) {
  const table = LINES[currentForm] || LINES.teen;
  const pool = table[part] || table.body;
  const key = `${currentForm}:${part}`;
  const i =
    lineCursor[key] === undefined
      ? Math.floor(Math.random() * pool.length)
      : (lineCursor[key] + 1) % pool.length;
  lineCursor[key] = i;
  const line = pool[i];

  say(line.text, 2400, line.mood, line.emote);
  playVoice(line.voice);

  // 整张姿势帧：摸头/摸耳 → 抬手捂头，戳脸 → 双手捂脸，碰尾巴 → 抱住尾巴护起来，
  // 踩到脚 → 抱脚缩起来。这几下是用户点名要的「点她会捂着头 / 捂着脸 / 收起尾巴」，
  // 换的是整张立绘，比「只晃一下」直观得多；时长与台词气泡对齐（2.4s）。
  // 兽形没有 posefeet（没有手抱不住脚），showPose 找不到帧会自动退回常驻帧 + 动作连招。
  if (part === "ear" || part === "head") showPose("posehead", 2400);
  else if (part === "face") showPose("poseface", 2400);
  else if (part === "tail") showPose("posetail", 2400);
  else if (part === "feet") showPose("posefeet", 2400);
  else clearPose(); // 身没有专属姿势帧，用常驻帧 + 动作连招，免得语义乱套

  // 台词自带的动作连招 + 按情绪补一层气质动作（高冷别脸 / 害羞缩肩 / 生气鼓身）
  playAnim(line.anim || ["hop"]);
  switch (line.mood) {
    case "cool":
      addClip("tiltCool", { dur: 1.2, delay: 0.1 });
      break;
    case "shy":
      addClip("shyShrink", { dur: 0.9, delay: 0.1 });
      break;
    case "angry":
      addClip("puff", { dur: 0.7, delay: 0.1 });
      break;
    case "surprise":
      addClip("leanBack", { dur: 0.6, delay: 0.05 });
      break;
    default:
      addClip("boing", { dur: 0.7, delay: 0.2 });
  }
  // 卖萌/害羞时眯眼笑一下
  if (line.mood === "cute" || line.mood === "shy") state.happyT = Math.max(state.happyT, 0.9);
}

function doAction(act) {
  if (act === "pet") {
    react("head");
  } else if (act === "greet") {
    const pool = GREETINGS[currentForm] || GREETINGS.teen;
    const g = pool[greetingCursor % pool.length];
    greetingCursor++;
    addClip("hop");
    addClip("squash", { delay: 0.1 });
    addClip("wag", { delay: 0.3 });
    say(g.text, 2200, "cute", "♪");
    playVoice(g.voice);
    state.happyT = Math.max(state.happyT, 1.0);
  } else if (act === "perform") {
    // 菜单「表演一段」：随机抽 3 个待机动作串成一串，纯动作不出声，
    // 用来一眼验证动作库是不是足够丰富。
    const picks = [];
    for (let i = 0; i < 3; i++) {
      picks.push(IDLE_CLIPS[Math.floor(Math.random() * IDLE_CLIPS.length)].clips[0]);
    }
    playAnim(picks);
    state.happyT = Math.max(state.happyT, 0.9);
  } else if (act === "voice-on") {
    soundOn = true;
  } else if (act === "voice-off") {
    soundOn = false;
  } else if (act && act.startsWith("form:")) {
    setForm(act.slice(5));
  } else if (act === "next-form") {
    const i = FORM_ORDER.indexOf(currentForm);
    setForm(FORM_ORDER[(i + 1) % FORM_ORDER.length]);
  }
}

/* ---------------------------- 鼠标事件 ---------------------------- */
/** 悬停在不同部位 → 姿态立刻有反应（这就是用户说的「鼠标放到各地方都没变化」） */
function setHoverPart(part) {
  if (state.hoverPart === part) return;
  state.hoverPart = part;
  if (!part) {
    hintEl.classList.remove("show");
    document.body.style.cursor = "default";
    ringEl.classList.remove("hover");
    return;
  }
  document.body.style.cursor = "pointer";
  const label = PART_LABEL[part] || part;
  hintEl.textContent = label;
  hintEl.dataset.part = part;
  hintEl.classList.add("show");
  ringEl.classList.add("hover");
  // 悬停本身给一个小幅即时动作，让人一眼看出「这里可点、点的是这里」
  if (part === "ear") addClip("earFlap", { dur: 0.5, amp: 0.7 });
  else if (part === "tail") addClip("wag", { dur: 0.7, amp: 0.6 });
  else if (part === "feet") addClip("boing", { dur: 0.5, amp: 0.5 });
  else addClip("boing", { dur: 0.5, amp: 0.4 });
}

/**
 * 悬停时的持续反馈：让光环在指针所在部位对应的高度亮起来，
 * 并让宠物朝指针方向微微歪头（幅度很小，桌宠应该稳住不抽）。
 * 只在 hoverPart 存在时生效，离开立刻恢复。
 */
function applyHoverFeedback() {
  const part = state.hoverPart;
  ringEl.classList.toggle("hover", !!part);
}


document.addEventListener("mousemove", (ev) => {
  const w = window.innerWidth || 1;
  state.pointerX = (ev.clientX / w) * 2 - 1;
  state.pointerY = (ev.clientY / (window.innerHeight || 1)) * 2 - 1;
  if (state.dragging) return;

  const hit = hitUV(ev);
  if (hit) {
    setHoverPart(partAt(currentForm, hit.u, hit.v));
  } else {
    setHoverPart(null);
  }
  const on = !!hit;
  if (on !== state.hovering) {
    state.hovering = on;
    api.hover(on);
  }
});

// 主进程轮询出的命中态（这是权威来源）：光标离开身体时必须立刻清掉悬停反馈。
// 主进程还带回归一化坐标 u/v，渲染层据此算部位——比等 mousemove 可靠得多。
api.onHoverChanged((info) => {
  if (!info || typeof info.on !== "boolean") {
    // 兼容旧签名（布尔）
    const on = !!info;
    state.hovering = on;
    if (!on) setHoverPart(null);
    return;
  }
  state.hovering = info.on;
  if (!info.on) {
    setHoverPart(null);
  } else if (!state.dragging) {
    setHoverPart(partAt(currentForm, info.u, info.v));
  }
});



document.addEventListener("mousedown", (ev) => {
  if (ev.button !== 0 || !pointerOnPet(ev)) return;
  state.downAt = { x: ev.screenX, y: ev.screenY, t: Date.now() };
  state.pressT = 0;
  state.rubbing = false;
  state.moved = false;
  api.dragStart();
  state.dragging = true;
  document.body.style.cursor = "grabbing";
});

window.addEventListener("mouseup", (ev) => {
  if (!state.dragging && !state.rubbing) return;
  const wasRubbing = state.rubbing;
  state.dragging = false;
  state.rubbing = false;
  api.dragEnd();
  document.body.style.cursor = state.hovering ? "grab" : "default";

  const d = state.downAt;
  state.downAt = null;
  if (!d || wasRubbing) return; // 揉过了，松手不再当成一次点击

  const moved = Math.hypot(ev.screenX - d.x, ev.screenY - d.y);
  if (!state.moved && moved < 6 && Date.now() - d.t < 450 && Date.now() - state.lastClickAt > 260) {
    const hit = hitUV(ev);
    const part = hit ? partAt(currentForm, hit.u, hit.v) : "body";
    react(part);
    state.lastClickAt = Date.now();
  }
});

document.addEventListener("dblclick", (ev) => {
  if (!pointerOnPet(ev)) return;
  state.clips.length = 0;
  addClip("spin", { dur: 0.62 });
  addClip("hop", { dur: 0.62 });
  state.happyT = Math.max(state.happyT, 0.6);
  say("转圈圈！", 1200, "cute", "🎵");
});

document.addEventListener("contextmenu", (ev) => {
  ev.preventDefault();
  api.menu();
});

window.addEventListener("keydown", (ev) => {
  if (ev.key === "1") setForm("woman");
  else if (ev.key === "2") setForm("teen");
  else if (ev.key === "3") setForm("beast");
  else if (ev.key === "f" || ev.key === "F") doAction("next-form");
  // 空格：让它自己表演一个随机动作（调试用，方便一眼看动作库）
  else if (ev.key === " ") {
    ev.preventDefault();
    const idle = IDLE_CLIPS[Math.floor(Math.random() * IDLE_CLIPS.length)];
    playAnim(idle.clips);
    state.happyT = Math.max(state.happyT, 0.4);
  }
});

// 指针离开窗口必须复位穿透状态，否则窗口会一直吃鼠标
window.addEventListener("mouseleave", () => {
  if (state.hovering) {
    state.hovering = false;
    api.hover(false);
  }
});

api.onDragCancel(() => {
  state.dragging = false;
  state.downAt = null;
  state.rubbing = false;
  state.hovering = false;
  document.body.style.cursor = "default";
});

// 主进程判定光标真移动了 → 这是「搬走」，不是「揉」，取消长按计时。
api.onDragMoved(() => {
  state.moved = true;
  state.pressT = 0;
});

/* ---------------------------- 尺寸 ---------------------------- */
window.addEventListener("resize", () => {
  layoutPet();
});

/* ---------------------------- 主循环 ---------------------------- */
const clock = { last: performance.now() };

function frame(now) {
  const dt = Math.min((now - clock.last) / 1000, 0.05);
  clock.last = now;
  state.t += dt;
  const t = state.t;

  // 长按 700ms 且光标没真移动 → 揉（持续眯眼），并停止拖动，避免「按住就跟着跑」。
  // 光标是否真移动由主进程按屏幕坐标判定（拖动是主进程轮询光标的，
  // 渲染层看不到移动，不能自己比 clientX/screenX）。
  if (state.dragging && state.downAt && !state.rubbing && !state.moved) {
    state.pressT += dt;
    if (state.pressT > 0.7) {
      state.rubbing = true;
      state.happyT = 2.4;
      state.dragging = false;
      api.dragEnd();
      document.body.style.cursor = "grab";
      addClip("nuzzle", { dur: 1.2 });
      say("唔……这样揉很舒服。", 2000, "cute", "❤");
      playVoice(currentForm === "beast" ? "bst-head2" : currentForm === "woman" ? "wom-head1" : "teen-head1");
    }
  }

  // 待机小动作：不点它也会自己动，桌宠才像活的
  state.idleAt -= dt;
  if (state.idleAt <= 0 && !state.dragging && !state.rubbing) {
    state.idleAt = 3 + Math.random() * 4;
    const idle = IDLE_CLIPS[Math.floor(Math.random() * IDLE_CLIPS.length)];
    playAnim(idle.clips);
  }

  if (state.squash > 0) state.squash = Math.max(0, state.squash - dt * 4.5);

  // 反应姿势（捂头/捂脸/护尾）是有时限的：到时自动回正到常驻睁眼帧
  if (state.poseT > 0) {
    state.poseT -= dt;
    if (state.poseT <= 0) {
      clearPose();
      setFrame("open");
    }
  }

  // 眨眼：睁眼/闭眼两张图切换，比缩放压扁自然
  let closed = false;
  if (state.blinkT > 0) {
    state.blinkT -= dt;
    closed = true;
  } else {
    state.blinkAt -= dt;
    if (state.blinkAt <= 0) {
      state.blinkT = 0.11;
      state.blinkAt = 2.6 + Math.random() * 4.4;
      closed = true;
    }
  }
  // 摸头/揉/卖萌的时候眯眼笑（持续闭眼）
  const happy = state.happyT > 0;
  if (happy) state.happyT -= dt;

  // 姿势帧期间不做眨眼替换：表情帧已经自带表情（惊讶瞪眼/不爽眯眼），
  // 再拿闭眼帧去盖会变成「捂着头却闭着眼」的怪画面。
  // 只有姿势播完、回到常驻帧之后，眨眼才接管 <img src>。
  const cache = imgCache[currentForm];
  if (cache && !state.pose) {
    const wantClosed = closed || happy;
    setFrame(wantClosed ? "closed" : "open");
  }

  // ---------------- 变换合成 ----------------
  // 1) 动作片段累加（o 里 sx/sy 是「相对 1 的增量」，所以初值是 0）
  const o = { dx: 0, dy: 0, rot: 0, sx: 0, sy: 0, rotY: 0 };
  stepClips(dt, o);

  // 2) 呼吸：整体轻微上下浮 + 极轻微纵向缩放
  const breath = Math.sin(t * 1.8);
  const floatY = breath * 2.2;

  // 3) 跟随鼠标：轻轻侧倾（左右 ±2.6°）+ 随鼠标上下略微拉伸
  const lean = state.pointerX * 2.6;
  const stretchM = state.pointerY * 0.8;

  // 4) 常驻的尾巴/身体微摆（幅度很小，但让它一直「活」着）
  const idleSway = Math.sin(t * 1.35) * 0.7;

  const sy = 1 + breath * 0.006 + o.sy;
  const sx = 1 - breath * 0.004 + o.sx;

  petEl.style.transform =
    `translate(calc(-50% + ${o.dx.toFixed(2)}px), ${(-floatY - o.dy).toFixed(2)}px) ` +
    `rotate(${(lean + idleSway + o.rot).toFixed(2)}deg) ` +
    `scale(${sx.toFixed(4)}, ${(sy + stretchM * 0.004).toFixed(4)}) ` +
    `rotateY(${o.rotY.toFixed(2)}deg)`;

  // 光环自转 + 光点跑圈
  const dot = ringEl.querySelector(".dot");
  if (dot) {
    const a = t * 1.3;
    const rx = ringEl.clientWidth / 2;
    const ry = ringEl.clientHeight / 2;
    dot.style.left = 50 + (Math.cos(a) * rx * 100) / ringEl.clientWidth + "%";
    dot.style.top = 50 + (Math.sin(a) * ry * 100) / ringEl.clientHeight + "%";
  }

  requestAnimationFrame(frame);
}

/* ---------------------------- 启动 ---------------------------- */
api.onAction(doAction);
api.onForm((f) => setForm(f, { silent: true }));

// 自测钩子：供 `electron . --selftest=<json>` 逐部位跑一遍命中 → 台词 → 动作链路。
// 只在没有真实点击的环境里用，不参与正常交互。
window.__petDebug = {
  setForm,
  react,
  partAt,
        /** 自测/快照用：直接挂上反应姿势（posehead|poseface|posetail|posefeet），ms 后自动回正 */
  showPose,
  /** 清空正在播的动作队列，自测时用来只看本次点击触发了哪些片段 */
  clearClips: () => {
    state.clips.length = 0;
  },
  /** 自测用：模拟「光标悬停在某部位」，走与主进程轮询完全相同的入口 */
  simulateHover: (part) => {
    setHoverPart(part || null);
  },
  /**
   * 命中自检：为每个部位找「该分区内一个真实不透明像素」，
   * 用 partAt(u,v) 反推它被判成什么部位。
   * 这样能抓出「分区落在透明区」或「被更高优先级分区吃掉」的死区——
   * 上一版 head 覆盖 0% 就是这么漏掉的。
   */
  hitProbe: () => {
    const res = {};
    if (!state.mask) return res;
    const parts = ["ear", "head", "face", "body", "tail", "feet"];
    for (const want of parts) {
      let found = null;
      // 1% 步长扫全图找第一个「属于 want 且不透明」的点
      for (let v = 0.01; v < 1 && !found; v += 0.01) {
        for (let u = 0.01; u < 1; u += 0.01) {
          const mx = Math.floor(u * state.maskW);
          const my = Math.floor(v * state.maskH);
          if (mx < 0 || my < 0 || mx >= state.maskW || my >= state.maskH) continue;
          if (state.mask[my * state.maskW + mx] !== 1) continue;
          if (partAt(currentForm, u, v) !== want) continue;
          found = {
            u: +u.toFixed(3),
            v: +v.toFixed(3),
            // 走真实判定链：坐标 → 部位
            hit: partAt(currentForm, u, v),
          };
          break;
        }
      }
      res[want] = found;
    }
    return res;
  },

  getState: () => ({
    form: currentForm,
    bubble: bubbleEl.textContent,
    bubbleMood: bubbleEl.dataset.mood,
    emote: emoteEl.textContent,
    clips: state.clips.map((c) => c.name),
    voice: audio ? audio.src.split("/").pop() : null,
    soundOn,
    /** 当前实际显示的帧（open/closed/posehead/poseface/posetail）——自测用来确认姿势帧真的换了 */
    frame: state.frame,
    pose: state.pose,
  }),
};

loadAll()
  .then(async () => {
    const s = await api.getState().catch(() => null);
    if (s && typeof s.voice === "boolean") soundOn = s.voice;
    const form = s?.form && FORMS[s.form] ? s.form : "teen";
    setForm(form, { silent: true });
    if (pendingForm) setForm(pendingForm, { silent: true });
    say(`我是${PET_NAME}。点点耳朵、脸、尾巴，我有不同反应哦`, 3400, "cute", "✨");
  })
  .catch((e) => {
    console.error("[pet] 素材加载失败", e);
    say("素材加载失败，检查 renderer/assets 下的图片", 5000, "angry", "❗");
  });

requestAnimationFrame(frame);
