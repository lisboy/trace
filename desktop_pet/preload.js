// 渲染层 <-> 主进程的最小桥：只暴露桌宠需要的几个动作，不给渲染层 Node 能力
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pet", {
  hover: (on) => ipcRenderer.send("pet:hover", on),
  dragStart: () => ipcRenderer.send("pet:drag-start"),
  dragMove: () => ipcRenderer.send("pet:drag-move"),
  dragEnd: () => ipcRenderer.send("pet:drag-end"),
  menu: () => ipcRenderer.send("pet:menu"),
  quit: () => ipcRenderer.send("pet:quit"),
  getState: () => ipcRenderer.invoke("pet:state"),
  onAction: (cb) => ipcRenderer.on("pet:action", (_e, act) => cb(act)),
  onScale: (cb) => ipcRenderer.on("pet:scale", (_e, s) => cb(s)),
  // 形态切换：主进程菜单点「形态」时下发，渲染层换模型
  onForm: (cb) => ipcRenderer.on("pet:form", (_e, f) => cb(f)),
  getForm: () => ipcRenderer.invoke("pet:form-get"),
  onDragCancel: (cb) => ipcRenderer.on("pet:drag-cancel", () => cb()),
  // 拖动过程中光标真的移动了（由主进程按屏幕坐标判定）：
  // 用来区分「长按不放（揉）」和「拖动（搬走）」，渲染层据此取消长按。
  onDragMoved: (cb) => ipcRenderer.on("pet:drag-moved", () => cb()),
  // 上报立绘矩形 + alpha 掩码：主进程据此按屏幕光标判定命中
  // （窗口全局穿透时渲染层收不到可靠的 mousemove，判定必须在主进程做）。
  reportHitRegion: (payload) => ipcRenderer.send("pet:hit-region", payload),
  // 主进程轮询出的「光标是否在身体上」结果，用于切光标样式与悬停反馈
  onHoverChanged: (cb) => ipcRenderer.on("pet:hover-changed", (_e, on) => cb(on)),
});

