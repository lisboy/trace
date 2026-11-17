// 循迹桌面宠物「霜尾」—— Electron 主进程
// 一个透明、无边框、置顶、点穿背景的小窗，里面跑 2D 手绘立绘（白狐三形态）。
// 实现要点：
//  1) transparent + frame:false + alwaysOnTop：桌宠浮在桌面上，没有窗口边框；
//  2) 默认 setIgnoreMouseEvents(true, {forward:true})，鼠标事件穿透到桌面，
//     只有指针真的压在宠物身体上时（渲染层用像素判定后 IPC 通知）才恢复接收，
//     这样窗口虽然占一块矩形区域，但不会挡住桌面图标的点击；
//  3) 右键菜单提供「形态 / 摸摸头 / 打个招呼 / 表演一段 / 置顶 / 大小 / 配音 / 退出」。
const { app, BrowserWindow, ipcMain, Menu, screen, shell } = require("electron");
const path = require("path");
const fs = require("fs");

/** 角色名：窗口标题、菜单标题统一用它（渲染层里的 PET_NAME 必须与之一致） */
const PET_NAME = "霜尾";

// 透明窗在 Windows 上必须在 app ready 之前打开这个开关，
// 放在 whenReady 里再加就晚了（会导致窗口底色不透明）。
app.commandLine.appendSwitch("enable-transparent-visuals");

const STATE_FILE = path.join(app.getPath("userData"), "pet-state.json");
// form：当前形态（woman 成女 / teen 少女 / beast 兽形），跟位置、大小一起持久化，
// 下次启动还是你上次选的那只。
const DEFAULT_STATE = {
  x: null, y: null, scale: 1, alwaysOnTop: true, bubble: true, form: "teen",
  // 配音开关：默认开（点不同部位会说不同的话）
  voice: true,
};
const FORMS = [
  { key: "woman", label: "成女形态（御姐）" },
  { key: "teen", label: "少女形态（二十岁）" },
  { key: "beast", label: "兽形（小奶狐）" },
];

let win = null;
let petState = loadState();
let hovering = false; // 指针是否在宠物身体像素上
let dragging = null; // 拖动中的偏移量

function loadState() {
  try {
    const raw = fs.readFileSync(STATE_FILE, "utf8");
    return { ...DEFAULT_STATE, ...JSON.parse(raw) };
  } catch {
    return { ...DEFAULT_STATE };
  }
}

function saveState() {
  try {
    fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
    fs.writeFileSync(STATE_FILE, JSON.stringify(petState, null, 2), "utf8");
  } catch {
    /* 保存失败不影响运行 */
  }
}

/** 窗口基准尺寸：宠物本体 + 头顶气泡留白。
 *  2D 立绘的宽高比各不相同，所以按素材实际比例给窗口（窗口比立绘略宽一点留气泡位置），
 *  让立绘尽量铺满窗口、同时不被裁切。
 *  抠图 + 各姿势帧（睁眼/闭眼/捂头/捂脸/护尾，人形还有蹲下抱膝的踩脚帧）统一对齐后的真实像素比：
 *  woman 439×1005 / teen 400×974 / beast 641×755（build_poses.py 保证各帧同画布）。 */
const FORM_SIZE = {
  woman: { w: 271, h: 620 }, // 七头身高挑，最窄最高（439:1005）
  teen: { w: 246, h: 600 }, // 矮一截、略宽（400:974）
  beast: { w: 386, h: 455 }, // 坐姿横宽（641:755）
};


function baseSize(form = petState.form || "teen") {
  return FORM_SIZE[form] || FORM_SIZE.teen;
}

function currentBounds() {
  const { scale } = petState;
  const { w, h } = baseSize();
  return {
    width: Math.round(w * scale),
    height: Math.round(h * scale),
  };
}

/** 默认落在主屏右下角（任务栏上方一点） */
function defaultPosition(width, height) {
  const { workArea } = screen.getPrimaryDisplay();
  return {
    x: Math.round(workArea.x + workArea.width - width - 24),
    y: Math.round(workArea.y + workArea.height - height - 12),
  };
}

/** 保证窗口至少有一部分留在某个显示器里，避免拔掉副屏后桌宠丢失 */
function clampToDisplays(x, y, width, height) {
  const displays = screen.getAllDisplays();
  const visible = displays.some((d) => {
    const a = d.workArea;
    return x + width > a.x + 40 && x < a.x + a.width - 40 && y + height > a.y + 20 && y < a.y + a.height - 20;
  });
  return visible ? { x, y } : defaultPosition(width, height);
}

function createWindow() {
  // 调试用：--form=woman|teen|beast 强制指定形态（验收快照时逐个形态各拍一张）
  const formArg = process.argv.find((a) => a.startsWith("--form="));
  if (formArg) {
    const f = formArg.slice("--form=".length);
    if (FORM_SIZE[f]) petState.form = f;
  }
  const { width, height } = currentBounds();
  const pos =
    petState.x === null || petState.y === null
      ? defaultPosition(width, height)
      : clampToDisplays(petState.x, petState.y, width, height);

  win = new BrowserWindow({
    width,
    height,
    x: pos.x,
    y: pos.y,
    title: `循迹桌宠 · ${PET_NAME}`,
    transparent: true,
    frame: false,
    resizable: false,
    maximizable: false,
    minimizable: false,
    skipTaskbar: true,
    hasShadow: false,
    alwaysOnTop: petState.alwaysOnTop,
    backgroundColor: "#00000000",
    // 桌宠不需要音频/麦克风权限，直接不给
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });

  // floating 层级：压在普通窗口之上，但不抢全屏/演示窗口（比 screen-saver 温和）
  if (petState.alwaysOnTop) win.setAlwaysOnTop(true, "floating");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: false });
  win.loadFile(path.join(__dirname, "renderer", "index.html"));

  // 调试用：--snapshot=<路径> 时，等渲染稳定后把窗口画面存成 PNG 并退出。
  // 透明窗用 capturePage 取不到合成后的桌面底，所以铺一层浅灰底再截，只用于验收模型与构图。
  const snapArg = process.argv.find((a) => a.startsWith("--snapshot="));
  if (snapArg) {
    const outPath = snapArg.slice("--snapshot=".length);
    // --pose=posehead|poseface|posetail：快照时先把反应姿势挂上，用来目视验收
    // 「点头会捂头 / 戳脸会捂脸 / 点尾会护尾」这三张姿势帧真的画出来了。
    const poseArg = (process.argv.find((a) => a.startsWith("--pose=")) || "").split("=")[1] || "";
    win.webContents.once("did-finish-load", () => {
      setTimeout(async () => {
        try {
          win.setBackgroundColor("#eef3fa");
          // 先切到要验收的姿势帧，再量覆盖层位置（量在切帧之前只会读到站姿的位置）
          if (poseArg) {
            await win.webContents.executeJavaScript(
              `window.__petDebug.showPose(${JSON.stringify(poseArg)}, 60000); true`
            );
            await new Promise((r) => setTimeout(r, 120));
          }
          // 头顶气泡会正好挡住头部，快照时先藏起来，只拍模型；
          // 藏之前把气泡/表情的实际 top 打出来——蹲姿帧的头顶比站姿低半截，
          // 靠这个数字确认 placeOverlays 把气泡挪到了头顶上方（而不是固定 8px 悬在半空）。
          const overlay = await win.webContents.executeJavaScript(
            '(() => { const b=document.getElementById("bubble"); const e=document.getElementById("emote");'
            + ' const t = (el) => el ? Math.round(el.getBoundingClientRect().top) : null;'
            + ' return { bubbleTop: t(b), emoteTop: t(e), stageH: window.innerHeight,'
            + ' frame: window.__petDebug.getState().frame, pose: window.__petDebug.getState().pose }; })()'
          );
          console.log("[pet] 覆盖层位置：" + JSON.stringify(overlay));
          await win.webContents.executeJavaScript(
            'const b=document.getElementById("bubble"); if(b) b.style.display="none"; true'
          );
          // 隐藏气泡后要等一帧重绘，否则截到的还是带气泡的旧画面
          await new Promise((r) => setTimeout(r, 260));
          const img = await win.webContents.capturePage();
          fs.writeFileSync(outPath, img.toPNG());
          console.log("[pet] 快照已保存：" + outPath);
        } catch (e) {
          console.error("[pet] 快照失败：", e);
        } finally {
          app.quit();
        }
      }, 2600);
    });
  }

  // 自测用：--selftest=<路径> 逐形态跑「六部位命中 → 台词 → 动作 → 配音」链路，
  // 把每步结果写成 JSON 并退出。用来在没有真实鼠标点击的环境里验证互动不是死代码。
  const testArg = process.argv.find((a) => a.startsWith("--selftest="));
  if (testArg) {
    const out = testArg.slice("--selftest=".length);
    win.webContents.once("did-finish-load", () => {
      setTimeout(async () => {
        try {
          const res = await win.webContents.executeJavaScript(`(async () => {
            const forms = ["woman", "teen", "beast"];
            const parts = ["ear", "head", "face", "body", "tail", "feet"];
            const out = [];
            for (const f of forms) {
              window.__petDebug.setForm(f, { silent: true });
              await new Promise((r) => setTimeout(r, 260));
              // A) 台词→动作→配音链路
              for (const p of parts) {
                window.__petDebug.clearClips();
                window.__petDebug.react(p);
                await new Promise((r) => setTimeout(r, 60));
                const s = window.__petDebug.getState();
                out.push({
                  form: f, part: p,
                  bubble: s.bubble, mood: s.bubbleMood, emote: s.emote,
                  clips: s.clips.slice(0, 6), voice: s.voice, soundOn: s.soundOn,
                  // 姿势帧：摸头/摸耳应换成 posehead，戳脸 poseface，摸尾 posetail，踩脚 posefeet（兽形无此帧）
                  frame: s.frame, pose: s.pose,
                });
              }
              // B) 命中判定：给每个部位一个「该部位内部的真实不透明点」，
              //    走 partAt(u,v) 反推，验证「点这里会不会判成这个部位」。
              //    上一版 selftest 直接 react(part) 绕过了这一步，
              //    所以分区表里的死区（head 被 ear/face 包住 0% 覆盖）一直没被测出来。
              out.push({ form: f, part: "__hit__", hitMap: window.__petDebug.hitProbe() });
            }
            return out;
          })()`);
          const hitRows = res.filter((r) => r.part === "__hit__");
          const inter = res.filter((r) => r.part !== "__hit__");
          const bad = inter.filter((r) => !r.bubble || !r.voice || !r.clips.length);
          // 姿势帧异常：摸头/摸耳没切到 posehead、摸尾没切到 posetail
          // （或反过来把姿势帧用在了脸/身/脚上），说明姿势帧链路断了。
          // 姿势帧异常：摸头/摸耳没切到 posehead、摸尾没切到 posetail，
          // 或者脸/身/脚反而挂上了姿势帧（说明姿势没被清掉）——都算链路断了。
          // 注意非姿势部位允许 frame=closed（卖萌/害羞时眯眼笑是正常的），
          // 所以这里判的是 pose 字段，不是 frame 字段。
          // 姿势帧异常：摸头/摸耳没切到 posehead、戳脸没切到 poseface、摸尾没切到 posetail、
          // 踩脚没切到 posefeet，或者身/反而挂上了姿势帧（说明姿势没被清掉）——都算链路断了。
          // 注意：① 非姿势部位允许 frame=closed（卖萌/害羞时眯眼笑是正常的），所以判的是 pose 字段不是 frame；
          //       ② 兽形没有 posefeet（没有手抱不住脚），踩脚期望值是空。
          const wantPose = (form, part) => {
            if (part === "ear" || part === "head") return "posehead";
            if (part === "face") return "poseface";
            if (part === "tail") return "posetail";
            if (part === "feet") return form === "beast" ? "" : "posefeet";
            return "";
          };
          const poseBad = inter.filter((r) => (r.pose || "") !== wantPose(r.form, r.part));
          // 命中异常：某形态某部位在自身区域内找不到任何可判中的点
          const hitBad = [];
          for (const h of hitRows) {
            for (const [part, info] of Object.entries(h.hitMap)) {
              if (!info || info.hit !== part) {
                hitBad.push({ form: h.form, part, got: info });
              }
            }
          }
          fs.writeFileSync(
            out,
            JSON.stringify({ total: inter.length, bad, hitBad, poseBad, hitRows, all: inter }, null, 2),
            "utf8",
          );
          console.log(
            `[pet] 自测完成：${inter.length} 条互动，异常 ${bad.length} 条；` +
            `命中分区异常 ${hitBad.length} 条；姿势帧异常 ${poseBad.length} 条 → ${out}`,
          );
        } catch (e) {
          console.error("[pet] 自测失败：", e);
        } finally {
          app.quit();
        }
      }, 2400);
    });
  }

  // 调试用：--hover-shot=<路径> 模拟「鼠标悬停在某部位」，截一张图确认真有反馈。
  // 没有这一项就没法证明「鼠标放到各地方没反应」已经修好——
  // 主进程轮询的是真实光标，无头环境里没法真的把鼠标挪过去。
  const hoverArg = process.argv.find((a) => a.startsWith("--hover-shot="));
  if (hoverArg) {
    const out = hoverArg.slice("--hover-shot=".length);
    const part = (process.argv.find((a) => a.startsWith("--hover-part=")) || "").split("=")[1] || "tail";
    win.webContents.once("did-finish-load", () => {
      setTimeout(async () => {
        try {
          // 直接打渲染层的悬停入口，等价于主进程轮询算出「在尾巴上」
          await win.webContents.executeJavaScript(
            `window.__petDebug.simulateHover(${JSON.stringify(part)}); true`,
          );
          await new Promise((r) => setTimeout(r, 700));
          const img = await win.webContents.capturePage();
          fs.writeFileSync(out, img.toPNG());
          console.log("[pet] 悬停反馈截图已保存：" + out + " (部位 " + part + ")");
        } catch (e) {
          console.error("[pet] 悬停截图失败：", e);
        } finally {
          app.quit();
        }
      }, 2600);
    });
  }

  // 调试用：--hittest=<路径> 在真实 Electron 里验证「点击穿透判定」——
  // 这是用户报「点击会穿透点到后面」的复现点：必须确认
  //   ① 压在身体像素上时 on=true（窗口会接收鼠标）
  //   ② 压在透明角落时 on=false（鼠标穿透到桌面，不挡后面窗口）
  // 用注入坐标做，不需要移动真实鼠标。
  const hitArg = process.argv.find((a) => a.startsWith("--hittest="));
  if (hitArg) {
    const out = hitArg.slice("--hittest=".length);
    win.webContents.once("did-finish-load", () => {
      setTimeout(async () => {
        const rows = [];
        const forms = ["woman", "teen", "beast"];
        try {
          for (const f of forms) {
            await win.webContents.executeJavaScript(
              `window.__petDebug.setForm(${JSON.stringify(f)}, { silent: true }); true`,
            );
            await new Promise((r) => setTimeout(r, 350));
            const b = win.getBounds();
            // 立绘矩形 + 掩码由渲染层上报，主进程这边直接用
            if (!hitRect || !hitMask) {
              rows.push({ form: f, error: "hitRect/hitMask 未上报" });
              continue;
            }
            const r = hitRect;
            const toScreen = (u, v) => ({
              x: Math.round(b.x + r.left + u * r.width),
              y: Math.round(b.y + r.top + v * r.height),
            });
            // 用掩码本身找一个「身体中心点」和一个「透明角落点」
            const probes = await win.webContents.executeJavaScript(
              `window.__petDebug.hitProbe()`,
            );
            for (const [part, info] of Object.entries(probes)) {
              if (!info) {
                rows.push({ form: f, part, error: "找不到该部位的可命中点" });
                continue;
              }
              const p = toScreen(info.u, info.v);
              const hit = hitTestAt(p);
              rows.push({
                form: f, part, u: info.u, v: info.v, screen: p,
                on: hit.on, expect: true, ok: hit.on === true,
              });
            }
            // 透明角落：四角必有一处在透明区（alpha=0）
            const corners = [[0.02, 0.02], [0.98, 0.02], [0.02, 0.98], [0.98, 0.98]];
            for (const [u, v] of corners) {
              const p = toScreen(u, v);
              const hit = hitTestAt(p);
              const mx = Math.floor(u * hitMask.w);
              const my = Math.floor(v * hitMask.h);
              const px = hitMask.data[my * hitMask.w + mx];
              rows.push({
                form: f, part: `corner(${u},${v})`, u, v, screen: p,
                on: hit.on, maskPixel: px, expect: px === 1 ? "any" : false,
                ok: px === 1 ? true : hit.on === false,
              });
            }
          }
          const bad = rows.filter((r) => r.ok === false);
          fs.writeFileSync(out, JSON.stringify({ bad, rows }, null, 2), "utf8");
          console.log(`[pet] 命中判定自检：${rows.length} 点，异常 ${bad.length} 点 → ${out}`);
        } catch (e) {
          console.error("[pet] 命中判定自检失败：", e);
        } finally {
          app.quit();
        }
      }, 2600);
    });
  }

  // 调试用：--dump-layout=<路径> 打印窗口与立绘的实际几何，排查「立绘超出窗口」类问题
  const dumpArg = process.argv.find((a) => a.startsWith("--dump-layout="));
  if (dumpArg) {
    const out = dumpArg.slice("--dump-layout=".length);
    win.webContents.once("did-finish-load", () => {
      setTimeout(async () => {
        try {
          const info = await win.webContents.executeJavaScript(`(() => {
            const p = document.getElementById("pet");
            const r = p.getBoundingClientRect();
            return {
              innerW: window.innerWidth, innerH: window.innerHeight,
              dpr: window.devicePixelRatio,
              src: p.getAttribute("src"),
              natural: [p.naturalWidth, p.naturalHeight],
              styleH: p.style.height, styleW: p.style.width,
              styleMaxH: p.style.maxHeight,
              offsetH: p.offsetHeight, offsetW: p.offsetWidth,
              computed: (() => {
                const cs = getComputedStyle(p);
                return {
                  transform: cs.transform, height: cs.height, width: cs.width,
                  maxHeight: cs.maxHeight, maxWidth: cs.maxWidth, bottom: cs.bottom,
                  zoom: cs.zoom, objectFit: cs.objectFit,
                };
              })(),
              rect: [r.left, r.top, r.width, r.height],
              winBounds: null,
            };
          })()`);
          info.winBounds = win.getBounds();
          fs.writeFileSync(out, JSON.stringify(info, null, 2), "utf8");
          console.log("[pet] 布局信息：" + JSON.stringify(info));
        } catch (e) {
          console.error("[pet] 布局导出失败：", e);
        } finally {
          app.quit();
        }
      }, 2200);
    });
  }

  win.on("closed", () => {
    win = null;
    stopDragLoop();
    if (hoverTimer) clearInterval(hoverTimer);
    hoverTimer = null;
  });

  // 拖到窗口外松手时渲染层收不到 mouseup，这里兜底收尾，
  // 否则窗口会一直保持「接收鼠标」状态，挡住桌面点击。
  win.on("blur", () => {
    if (dragging) {
      stopDragLoop();
      dragging = null;
      hovering = false;
      applyMouseMode();
      if (win) win.webContents.send("pet:drag-cancel");
    }
  });

  // 位置变化就记下来（拖完自动记住下次在哪）
  win.on("moved", () => {
    if (!win) return;
    const [x, y] = win.getPosition();
    petState.x = x;
    petState.y = y;
    saveState();
  });

  win.webContents.on("render-process-gone", (_e, details) => {
    console.error("[pet] 渲染进程退出：", details.reason);
  });
}

/** 只有鼠标真的在宠物身上才让窗口接收事件，否则整体穿透 */
function applyMouseMode() {
  if (!win) return;
  if (dragging || hovering) {
    win.setIgnoreMouseEvents(false);
  } else {
    win.setIgnoreMouseEvents(true, { forward: true });
  }
}

/* ---------------------- 命中判定（主进程侧） ---------------------- */
// 立绘在窗口内的位置（CSS 像素），由渲染层上报；窗口 DIP 与 CSS 像素在
// 本应用里一致（未设置 zoomFactor），所以可以直接和 screen 坐标相减。
let hitRect = null; // { left, top, width, height }
let hitMask = null; // { w, h, data: Uint8Array }
let lastHoverKey = ""; // 上一次上报给渲染层的部位坐标，用来去重


/**
 * 用屏幕光标判断是否压在宠物身体像素上。
 * 为什么放在主进程：窗口全局穿透时，渲染层的 mousemove 只有 forward 过来的移动事件，
 * 首次进入、点击前那一刻经常收不到，导致「命中」状态一直为 false、窗口始终穿透——
 * 用户点了就穿到后面的窗口去。主进程轮询 screen.getCursorScreenPoint() 没有这个问题。
 * 返回值同时带上归一化坐标 u/v，渲染层据此算出「光标在哪个部位」做悬停反馈。
 * pt 可注入（自检用），默认取真实光标。
 */
function hitTestAt(pt) {
  if (!win || !hitRect) return { on: false, u: 0, v: 0 };
  const b = win.getBounds();
  // 光标 → 窗口内 CSS 坐标
  const cx = pt.x - b.x;
  const cy = pt.y - b.y;
  const r = hitRect;
  if (cx < r.left || cx > r.left + r.width || cy < r.top || cy > r.top + r.height) {
    return { on: false, u: 0, v: 0 };
  }
  const u = (cx - r.left) / r.width;
  const v = (cy - r.top) / r.height;
  if (!hitMask) return { on: true, u, v }; // 掩码缺失时退化成包围盒
  const mx = Math.floor(u * hitMask.w);
  const my = Math.floor(v * hitMask.h);
  if (mx < 0 || my < 0 || mx >= hitMask.w || my >= hitMask.h) return { on: false, u, v };
  // 掩码向外膨胀 2 格（约 8px 屏幕像素）：让窗口在指针「快要」贴到毛上时就恢复接收鼠标。
  // 不膨胀的话，用户从远处快速点过来，60ms 轮询还没来得及把窗口切回可接收状态，
  // 那一下点击就漏到后面的窗口去了——这正是用户报的「点击会穿透」。
  const RAD = 2;
  for (let dy = -RAD; dy <= RAD; dy++) {
    const yy = my + dy;
    if (yy < 0 || yy >= hitMask.h) continue;
    for (let dx = -RAD; dx <= RAD; dx++) {
      const xx = mx + dx;
      if (xx < 0 || xx >= hitMask.w) continue;
      if (hitMask.data[yy * hitMask.w + xx] === 1) return { on: true, u, v };
    }
  }
  return { on: false, u, v };
}

function pointerOnPetNow() {
  return hitTestAt(screen.getCursorScreenPoint());
}

/** 轮询光标：命中态变了才切窗口的穿透模式（避免每帧都调 setIgnoreMouseEvents） */
let hoverTimer = null;
function startHoverLoop() {
  if (hoverTimer) return;
  // 30ms（≈33Hz）：比 60ms 更不容易在「快速点过来」时漏掉第一下点击
  hoverTimer = setInterval(() => {
    if (!win) return;
    // 拖动中不做命中判定：窗口此时已经接收鼠标，再判会把拖动打断
    if (dragging) return;
    const hit = pointerOnPetNow();
    if (hit.on !== hovering) {
      hovering = hit.on;
      applyMouseMode();
    }
    // 每次都告诉渲染层当前所在部位（u/v 变了才发，省 IPC）
    const key = hit.on ? `${hit.u.toFixed(3)},${hit.v.toFixed(3)}` : "";
    if (key !== lastHoverKey) {
      lastHoverKey = key;
      if (win) win.webContents.send("pet:hover-changed", { on: hit.on, u: hit.u, v: hit.v });
    }
  }, 30);
}



function resizeTo(scale) {
  if (!win) return;
  const { width, height } = currentBounds();
  const old = win.getBounds();
  // 缩放时以「脚底中心」为锚点，宠物不会跳位置
  const footX = old.x + old.width / 2;
  const footY = old.y + old.height;
  const w = Math.round(baseSize().w * scale);
  const h = Math.round(baseSize().h * scale);
  win.setBounds({
    x: Math.round(footX - w / 2),
    y: Math.round(footY - h),
    width: w,
    height: h,
  });
  petState.scale = scale;
  saveState();
  win.webContents.send("pet:scale", scale);
}

/**
 * 切换形态：记状态 → 通知渲染层换模型 → 按新形态的取景重算窗口尺寸。
 * 以「脚底中心」为锚点缩窗，宠物不会因为换形态跳位置。
 */
function setForm(form) {
  if (!FORM_SIZE[form]) return;
  petState.form = form;
  saveState();
  if (!win) return;
  win.webContents.send("pet:form", form);

  const old = win.getBounds();
  const footX = old.x + old.width / 2;
  const footY = old.y + old.height;
  const { w, h } = baseSize(form);
  const scale = petState.scale || 1;
  const width = Math.round(w * scale);
  const height = Math.round(h * scale);
  win.setBounds({
    x: Math.round(footX - width / 2),
    y: Math.round(footY - height),
    width,
    height,
  });
}

function buildMenu() {
  const sizeItems = [
    { label: "小", scale: 0.8 },
    { label: "中", scale: 1 },
    { label: "大", scale: 1.35 },
  ].map((it) => ({
    label: it.label,
    type: "radio",
    checked: Math.abs((petState.scale || 1) - it.scale) < 0.01,
    click: () => resizeTo(it.scale),
  }));

  // 形态切换：三选一。切换后窗口尺寸按新形态的取景重新算，
  // 不然高挑的成女和矮胖的兽形共用 320×380 会一个被裁、一个留白过多。
  const formItems = FORMS.map((f) => ({
    label: f.label,
    type: "radio",
    checked: (petState.form || "teen") === f.key,
    click: () => setForm(f.key),
  }));

  return Menu.buildFromTemplate([
    { label: `${PET_NAME} · 形态`, submenu: formItems },
    { type: "separator" },
    {
      label: "摸摸头",
      click: () => win && win.webContents.send("pet:action", "pet"),
    },
    {
      label: "打个招呼",
      click: () => win && win.webContents.send("pet:action", "greet"),
    },
    {
      // 随机表演一段待机动作（动作库里的一个随机组合），方便一眼看动作丰不丰富
      label: "表演一段",
      click: () => win && win.webContents.send("pet:action", "perform"),
    },
    { type: "separator" },
    { label: "大小", submenu: sizeItems },
    {
      label: "播放配音",
      type: "checkbox",
      checked: petState.voice !== false,
      click: (item) => {
        petState.voice = item.checked;
        saveState();
        if (win) win.webContents.send("pet:action", item.checked ? "voice-on" : "voice-off");
      },
    },
    {
      label: "总在最前",
      type: "checkbox",
      checked: petState.alwaysOnTop,
      click: (item) => {
        petState.alwaysOnTop = item.checked;
        saveState();
        if (win) win.setAlwaysOnTop(item.checked, "floating");
      },
    },
    { type: "separator" },
    {
      label: "回到右下角",
      click: () => {
        if (!win) return;
        const { width, height } = win.getBounds();
        const p = defaultPosition(width, height);
        win.setPosition(p.x, p.y);
      },
    },
    {
      label: "退出桌宠",
      click: () => app.quit(),
    },
  ]);
}

function registerIpc() {
  // 渲染层在「布局完成 / 换形态」后上报：立绘在窗口内的 CSS 矩形 + alpha 掩码。
  // 命中判定改由主进程按屏幕光标算，因为窗口处于 setIgnoreMouseEvents(true,{forward:true})
  // 时渲染层的 mousemove 并不保证送进来（forward 只转移动、且首击有竞态）——
  // 这就是用户报的「点击会穿透点到后面的窗口」。
  ipcMain.on("pet:hit-region", (_e, payload) => {
    if (!payload || !payload.rect) return;
    hitRect = payload.rect;
    if (payload.mask && payload.mask.w && payload.mask.h) {
      hitMask = {
        w: payload.mask.w,
        h: payload.mask.h,
        data: Uint8Array.from(payload.mask.data),
      };
    }
  });

  // 渲染层只用它改光标样式（手型/默认），不再决定窗口是否穿透——那是主进程的事。
  ipcMain.on("pet:hover", () => {});

  // 拖动：渲染层只负责「按下/松开」，位置由主进程按光标屏幕坐标换算。
  // 不用 mousemove 传位置——光标拖出窗口后渲染层收不到事件，宠物会卡住不动。
  ipcMain.on("pet:drag-start", () => {
    if (!win) return;
    const cursor = screen.getCursorScreenPoint();
    const [wx, wy] = win.getPosition();
    dragging = { dx: cursor.x - wx, dy: cursor.y - wy };
    applyMouseMode();
    startDragLoop();
  });

  ipcMain.on("pet:drag-end", () => {
    stopDragLoop();
    dragging = null;
    hovering = false;
    applyMouseMode();
  });

  ipcMain.on("pet:menu", () => {
    if (!win) return;
    buildMenu().popup({ window: win });
  });

  ipcMain.on("pet:quit", () => app.quit());

  // 渲染层启动时问一次当前形态（持久化在 pet-state.json）
  ipcMain.handle("pet:form-get", () => petState.form || "teen");
  ipcMain.on("pet:form-set", (_e, form) => setForm(form));

  ipcMain.handle("pet:state", () => ({
    scale: petState.scale,
    form: petState.form || "teen",
    voice: petState.voice !== false,
  }));
}

/* ---------------------------- 拖动跟随 ---------------------------- */
let dragTimer = null;
// 拖动期间钉住的窗口尺寸：只改位置、不改尺寸（见下面 setBounds 的注释）
let dragSize = null;

function startDragLoop() {
  stopDragLoop();
  if (!win) return;
  const b = win.getBounds();
  dragSize = { width: b.width, height: b.height };
  const origin = screen.getCursorScreenPoint();
  let reported = false;
  dragTimer = setInterval(() => {
    if (!win || !dragging) return stopDragLoop();
    const cursor = screen.getCursorScreenPoint();
    // 光标真的移动过 6px 才算「拖动」；只按住不动不算。
    // 渲染层拿这个信号取消长按揉摸，避免「想揉变成搬走」。
    if (!reported && Math.hypot(cursor.x - origin.x, cursor.y - origin.y) > 6) {
      reported = true;
      dragging.moved = true;
      if (win) win.webContents.send("pet:drag-moved");
    }
    // 这里必须显式带 width/height，不能只 setPosition：
    // 高 DPI（125%/150% 缩放）下 setPosition 的 DIP↔物理像素换算会四舍五入，
    // 结果是窗口尺寸每帧被悄悄改一点——表现就是「左键长按不放，宠物越变越大」。
    win.setBounds({
      x: Math.round(cursor.x - dragging.dx),
      y: Math.round(cursor.y - dragging.dy),
      width: dragSize.width,
      height: dragSize.height,
    });
  }, 16);
}

function stopDragLoop() {
  if (dragTimer) clearInterval(dragTimer);
  dragTimer = null;
  // 收尾时按「当前形态 + 当前缩放」把尺寸强行校正一次，
  // 万一中途已经被撑大/压扁过，松手立刻回正，不会留在那里。
  if (win && dragSize) {
    const want = currentBounds();
    const b = win.getBounds();
    if (b.width !== want.width || b.height !== want.height) {
      win.setBounds({ x: b.x, y: b.y, width: want.width, height: want.height });
    }
  }
  dragSize = null;
}

// 单实例：重复启动只是把已有桌宠叫到前面，不会开第二只
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      win.show();
      win.focus();
    }
  });

  app.whenReady().then(() => {
    // Windows 上透明窗需要禁用 GPU 合成缓存才会真正透明
    registerIpc();
    createWindow();
    applyMouseMode();
    // 命中判定改由主进程轮询光标（见 pointerOnPetNow 注释）：
    // 窗口全局穿透时渲染层的 mousemove 不可靠，会漏掉「第一次点上去」。
    startHoverLoop();

    screen.on("display-metrics-changed", () => {
      if (!win) return;
      const old = win.getBounds();
      const p = clampToDisplays(old.x, old.y, old.width, old.height);
      win.setPosition(p.x, p.y);
    });
  });

  app.on("window-all-closed", () => app.quit());
}
