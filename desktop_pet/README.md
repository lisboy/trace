# 循迹桌宠（Trace Pet）· 桌面白狐「霜尾」

一个真的浮在你桌面上的桌宠：透明无边框小窗，能拖、能摸、会眨眼、会自己动、不挡桌面图标。

角色名 **霜尾**。形象是兽耳兽尾的白狐，可在三个形态间切换，**每个形态有独立音色**：

| 形态 | 形象 | 音色（F0 实测） |
| --- | --- | --- |
| 成女 | 银白长发（中分、双眼不被刘海遮）、银灰亮面夹克 + 白短背心 + 白工装裤 + 厚底鞋、右侧大狐尾 | 御姐低音 ≈178Hz |
| 少女 | 同一只狐娘的二十岁前后版本，明显矮一截、脸更嫩 | 成年女中音 ≈205Hz |
| 兽形 | 坐姿纯白幼狐，粉内耳、四爪粉垫、绕身大蓬尾 | 幼崽奶音 ≈300Hz |

## 为什么最终是 2D 立绘而不是 3D

最早用 Three.js 基础几何体程序化建过一版，后来又用 Blender（bpy 脚本 + metaball）真建过
三形态并导出 .glb，但两版出来的形体都偏「拼装感」——方块/球体堆出来的白狐不像毛茸茸的
动物，用户明确反馈「太像乐高积木」。手绘立绘在「毛茸茸 + 极简可爱」这个气质上明显更合适，
而且 2D 渲染几乎零 GPU 负担，不会和你开着的游戏抢显卡。

所以现在的实现是：**每形态五张透明底 PNG（睁眼 / 闭眼 / 捂头 / 捂脸 / 护尾，人形再加一张「蹲下抱膝」）**，
靠交替两帧做真正的眨眼，靠换整张姿势帧做「点她会捂头、戳脸会捂脸、点尾巴她会护住尾巴、踩到脚她会蹲下抱住膝盖缩成一团」。
3D 那版的 .glb 仍留在 `renderer/assets/` 里，随时可以切回去。

## 互动：点不同部位，说不同的话

先在 alpha 掩码上判定「指针真的压在狐身上」，再按归一化坐标分六个部位，每处两句话
（情绪不同、配音不同、动作不同）：

| 部位 | 成女 | 少女 | 兽形 |
| --- | --- | --- | --- |
| 耳 | 娇嗔 → 高冷警告 | 害羞 → 定规矩 | 生气「嗷呜」 |
| 头 | 慵懒认可 → 冷淡嫌头发乱 | 舒服 → 抱怨 | 卖萌求摸 |
| 脸 | 生气反问 → 害羞训人 | 生气 → 不自在 | 惊讶歪头 |
| 身 | 压着脾气 → 轻声嫌弃 | 惊叫 → 强辩 | 舒服呼噜 |
| 尾 | 戏谑 → 冷淡 | 炸毛 → 警告 | 生气 → 得意 |
| 脚 | 无语 → 疲惫 | 惊叫 → 埋怨 | 护爪 |

**整张姿势帧**（不只是晃一下）：点耳/头 → 换成「双手抬起来捂住头顶和狐耳」的姿势帧，戳脸 → 换成
「双手捂脸」的姿势帧，点尾 → 换成「双手把蓬松大尾巴搂到怀里护住」的姿势帧，踩脚 → 换成
「单腿抬起来双手抱住那只脚」的姿势帧（兽形没有手，点尾是「两爪拢住尾巴炸毛」、点脚退回常驻帧 +
动作连招），四类姿势都挂 2.4 秒（与台词气泡等长）后自动回正常驻帧；身用常驻帧 + 动作连招。
姿势帧期间不做眨眼替换（表情帧自带表情），命中掩码始终按 open 帧算，所以护尾的瞬间
「尾巴那块区域」仍然可点，不会点一下就没反应。

> 为什么尾巴是「抱住」而不是「收起来」：img2img 从 denoise 0.58 一路试到 0.90，模型始终把
> 狐尾当成角色固有部件，改出来的只是「小一圈」；而「双手把尾巴抱到怀里」在 0.86 就稳定出，
> 点一下的画面变化更明显，也更像被摸了尾巴立刻护住的反应。

| 操作 | 效果 |
| --- | --- |
| 鼠标移到宠物身上 | 窗口才接收鼠标事件，其余区域**完全点穿**，不挡桌面图标 |
| 悬停在各部位 | 显示部位名提示 + 光环变亮 + 该部位一个小幅即时动作 |
| 单击身体 | 按部位台词 + 情绪表情 + 2~3 段动作连招 + 对应配音；耳/头/脸/尾还会换整张姿势帧 |
| 左键长按（不移动） | 进入「揉」状态：眯眼、蹭蹭、说一句很舒服的话；**不会越变越大** |
| 按住拖动 | 把宠物搬到桌面任意位置，下次启动记住（拖动期间窗口尺寸钉死） |
| 双击 | 转圈 + 跳起 + 飘音符 |
| 空格 | 随机表演一个待机动作（调试动作库用） |
| 1 / 2 / 3 | 切成女 / 少女 / 兽形 |
| 右键 | 菜单：形态 / 摸摸头 / 打个招呼 / 表演一段 / 大小 / 播放配音 / 总在最前 / 回到右下角 / 退出 |

**动画不是只有一个弹跳**：`renderer/pet.js` 里有一个 20 个片段的动作库（跳、连跳、压扁回弹、
摇头、别脸、缩肩、甩尾、耳抖、转圈、伸懒腰、探头、点头、踢腿、颤、鼓身、后仰、重心挪、
打哈欠、蹭蹭、弹性），每次点击叠加 2~3 个，空闲时每 3~7 秒自动来一个待机动作。

## 从项目界面里开关

顶栏右上角有一个 **🦊 霜尾** 按钮（`frontend/src/components/PetToggle.tsx`，在提醒铃铛左边，
任何页面都常驻）：点一下把霜尾叫到桌面、再点一下收起。

它调用后端 `POST /api/pet/start|stop`，由后端 `subprocess.Popen(..., detached)` 拉起/结束
Electron 进程——浏览器自己开不出透明置顶窗，也拿不到启动本地程序的权限，所以必须走后端。
`detached` 的好处是后端重启不会连带把桌宠关掉。

`GET /api/pet/status` 返回 `{running, pid, available, reason}`；`available=false` 且带
`reason` 时（通常是没 `npm install`），前端按钮置灰并把原因显示成 tooltip。
前端每 8 秒对一次状态，所以你从右键菜单/任务管理器直接关掉桌宠，界面也会自己同步。

单实例：`main.js` 里有 `requestSingleInstanceLock`，重复点「打开」只会把已有那只叫到前台。

## 快速开始

```cmd
cd desktop_pet
start.cmd
```

Linux / macOS 用 `./start.sh`；也可以 `npm install`（只第一次需要）后 `npm start`。

## 素材管线（改了立绘必须按顺序跑）

源图在 `assets/art/character/`，输出统一叫 `desktop_pet/renderer/assets/pet2d-*.png`：

```cmd
python desktop_pet/blender/cutout_2d.py      :: 白底泛洪 → woman / teen
python desktop_pet/blender/cutout_green.py   :: 绿幕色键 → beast
python desktop_pet/blender/align_frames.py   :: 睁闭双帧对齐到同一画布，尺寸严格一致
python desktop_pet/blender/build_poses.py    :: 抠「捂头/捂脸/护尾/蹲下抱膝」姿势帧 + 全帧统一画布
```

- **姿势帧（点她会捂头、戳脸会捂脸、点尾巴会护住尾巴、踩脚会蹲下抱住膝盖）也走这条管线**：`build_poses.py`
  会先调 `cutout_2d`/`cutout_green` 抠姿势源图（人形的新源图在 `assets/art/illustration/`，
  较早的 `pose-*` 在 `assets/art/character/`，脚本两边都找），再把
  open/closed/posehead/poseface/posetail(+posefeet) **各帧**一起对齐到同一张画布
  （woman 439×1005 / teen 400×974 / beast 641×755）并断言一致。
  姿势帧和常驻帧不同尺寸会让换姿势的瞬间整个人跳一下——和当初「眨眼跳位」同一个坑。
- **踩脚（posefeet）是蹲姿，为什么不用 img2img**：以站姿立绘为底图让模型「抱起一只脚」，
  它一定会额外画一条抬起的腿、同时保留原来站着的两条——连 denoise 0.85 也改不掉
  （试过 pet13/pet14/pet16 三代，都是三条腿）。最终改成**文生图**重出一张完整的
  「蹲下抱膝」新构图（pet18），腿数天然正确，动作反差也更大。
  代价是它的身长只有站姿的 0.58（`FRAME_H_RATIO`），所以 `unify()` 对这张帧按比例定高、
  再贴底边摆放——否则「全帧高度对齐 open」会把蹲着的人放大到比站着还高。
  气泡/情绪符号也不再固定窗口顶部，而是按当前帧真实 alpha 顶边跟着头顶走
  （站姿仍是原来的 6px，蹲姿自动下移）。
- **封闭背景块必须单独处理**：四边泛洪只能删「连到画外的背景」。抬臂时手臂与头发
  围出的白块、发丝缝、两腿之间、抱脚时腿弯里的空白，四边都不连通，泛洪到不了，
  会原样留成白斑（用户圈出的就是手肘内侧那块）。`cutout_2d` 现在会再扫一遍
  「白但不贴边」的连通块，用**中位亮度贴近背景基准（±3）+ 无阴影梯度（p05~p95 ≤12）**
  两条一起判；白 T 恤/白裤子的高光因为带褶皱阴影（中位比背景低 5 个点以上）不会被误删。
  实测一轮清掉：成女捂头 8633px、成女抱脚 14763px、少女捂头 8603px、成女 open 1131px。
  兽形绿幕同样加了封闭块处理（绿幕无色歧义，判据只用绿主导度），本轮 beast 源图实测 0px。
- **兽形必须走绿幕色键**：白狐本体就是白的，白色泛洪实测会把身体中轴吃掉 22.8%
  （阈值 236）～35.7%（230），表现就是「身体都没一半了」。
- **闭眼帧必须由睁眼帧 img2img 生成**（denoise：成女 0.32、兽形 0.80 才真正闭眼），
  单独文字出图必然换脸换姿势。
- **姿势帧的 img2img denoise 要 ≥0.85**：0.55~0.62 时模型只做微调，姿势几乎不变
  （第一批「捂头」出出来还是双手插兜），但 0.85 起会连衣服一起改，prompt 里必须把
  上衣/外套/裤子/鞋逐件写死。
- **「把尾巴收起来」用 img2img 做不到**（0.58→0.90 全试过，模型把狐尾当固有部件，
  只会缩一圈）；改用「双手把尾巴抱到怀里」这种**增加动作**的描述，0.86 就能稳定出。
  同理兽形没法抱尾（没有手），保留「两爪拢住尾巴护在身前 + 炸毛」那张。
- 换了素材要重算部位分区：`.agent/tmp/row_segments.py`（逐行不透明片段）、
  `.agent/tmp/final_regions.py`（各矩形占比 ≥8% 才算可点）、
  `.agent/tmp/check_regions.js`（分区中心不被更高优先级遮掉）。

## 配音

三套音色用基频（F0）客观标定，不靠形容词：

- `.agent/tmp/f0_pitch.py` — 纯标准库自相关量 F0 中位数
- `.agent/tmp/pitch_normalize.py` — ffmpeg `asetrate=SR*R,aresample=SR,atempo=1/R`
  只变调不变速，把每条拉到目标音域（御姐 178 / 二十岁 205 / 奶音 300 Hz，偏差 ≤8Hz）
- `.agent/tmp/check_pet_assets.js` — 校验台词引用的每个 wav 都存在、动作片段无死代码、
  三形态 × 六部位各 ≥2 句

## 验收快照 / 自测（无需真开窗）

```cmd
:: 只截一张构图（--form=woman|teen|beast）
node_modules\electron\dist\electron.exe . --form=woman --snapshot=..\.trace\shots\pet-woman.png

:: 截「反应姿势」帧（--pose=posehead|poseface|posetail|posefeet），验收捂头 / 捂脸 / 护尾 / 抱脚
node_modules\electron\dist\electron.exe . --form=woman --pose=posehead --snapshot=..\.trace\shots\pose-woman-head.png

:: 逐形态跑「六部位 → 台词 → 情绪 → 动作 → 配音 → 姿势帧」链路，输出 JSON
:: （JSON 里 poseBad 必须为 0：摸头/摸耳→posehead、戳脸→poseface、摸尾→posetail、
::  踩脚→posefeet；兽形没有 posefeet，踩脚期望为空。bad/hitBad 也必须为 0）
node_modules\electron\dist\electron.exe . --selftest=..\.trace\shots\pet-selftest.json

:: 导出窗口与立绘的实际几何（排查「立绘超出窗口 / 快照里只剩腿」这类问题）
node_modules\electron\dist\electron.exe . --form=woman --dump-layout=..\.trace\shots\layout.json
```

> 本机验证时若桌宠已经在跑（单实例锁），自测/快照会静默退出、不写文件。
> 加 `--user-data-dir=<临时目录>` 起一个独立实例即可，例如：
> `node_modules\electron\dist\electron.exe . --user-data-dir=..\.agent\tmp\pet-profile --selftest=..\.trace\shots\st.json`

透明窗截不到合成后的桌面底，所以快照模式会临时铺浅底，仅用于看立绘和构图。

## 技术上是怎么做到「真桌宠」的

这几个点不搞清楚，做出来就是一块挡桌面的方块窗口：

1. **透明**：`transparent: true` + `frame: false` + `backgroundColor: "#00000000"`，
   渲染层背景留空，只剩宠物本体。
2. **点穿，但不是完全不收事件**：默认 `setIgnoreMouseEvents(true, { forward: true })`，
   鼠标全部穿透到桌面；渲染层读立绘的 alpha 生成一张不透明区域掩码，指针真的压在宠物身上
   时才通过 IPC 叫主进程关掉穿透。这样窗口虽然是个矩形，但不会抢桌面点击。
3. **不抢焦点**：`skipTaskbar: true`、`floating` 层级置顶（比 screen-saver 温和，不压全屏
   演示），并 `setVisibleOnAllWorkspaces`，切虚拟桌面也还在。
4. **记住状态**：位置、大小、置顶开关、配音开关、**当前形态**写在 `userData/pet-state.json`；
   拔掉副屏后启动会检测可见性，宠物跑到屏幕外会自动回右下角。
5. **拖动不改尺寸**：拖动只 `setBounds` 换位置、宽高钉死在开始拖动时的那一份，
   否则窗口尺寸会每帧被改一点——表现就是「长按不放，宠物越变越大」。
6. **动作片段用增量语义**：变换合算的累加器初值必须是 0（`1 + o.sy` 才是最终缩放），
   初值给 1 会常驻放大 2 倍、把头送出窗口（快照里只剩腿）。

## 目录

```
desktop_pet/
  main.js            主进程：透明置顶窗、点穿、拖动、右键菜单、状态持久化、快照/自测钩子
  preload.js         最小 IPC 桥（渲染层拿不到 Node 能力）
  renderer/
    index.html       透明页面 + 头顶气泡 + 表情 + 粒子样式
    pet.js           三形态立绘、部位命中、动作库、台词表、配音播放
    assets/          pet2d-*.png（三形态 × 睁眼/闭眼）、voice/*.wav（三套音色）
  blender/           cutout_2d.py 白底泛洪 / cutout_green.py 绿幕色键 / align_frames.py 双帧对齐
  start.cmd / start.sh     一键启动
```

## 后续打包成 exe

现在这个是「开发态可跑」。要发给别人双击就能用，加 electron-builder 即可（依赖体积会到
百 MB 级，所以没放进主流程）：

```cmd
cd desktop_pet
npm i -D electron-builder
npx electron-builder --win portable
```

透明窗在 Windows 上如遇边缘发黑，`main.js` 里已有 `enable-transparent-visuals` 开关，
再不行把 `hasShadow` 保持 false 即可。
