"""服务与视觉验收工具（项目组长自测专用）。

为什么单独一层：`run_command` 是**同步阻塞**的（等命令跑完才返回、超时还会被杀），
起不了常驻服务；而「启服务 → 截图看真实效果 → 改到对」是组长自测的核心动作。

四个工具：
- `start_service`   后台起服务（不阻塞），自动等端口就绪，返回 pid / url / 日志路径
- `service_logs`    看后台服务输出（起不来时靠它定位）
- `stop_service`    按 pid 或本轮全部收掉
- `screenshot_page` 无头浏览器截图，并把图**作为视觉输入**回灌给模型（能真看见效果）

run 结束时编排器会调用 `kill_run_services` 兜底收掉所有残留进程。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from game_agent.agents.tools import ToolContext, ToolError, ToolResult
from game_agent.agents.tools.native import (
    _BROWSER_KILL_MSG, _browser_global_kill, _dangerous, _effective_whitelist,
    _whitelist_error, cmd_head)
from game_agent.assets import gpu_arbiter
from game_agent.config import SHELL_POLICY
from game_agent.logging_setup import get_logger
from game_agent.orchestration import envfacts
from game_agent.orchestration.tempfiles import register as register_temp

log = get_logger("agents.service")

# run_id -> {pid: 记录}
_PROCS: dict[str, dict[int, dict]] = {}

_LOG_DIR = Path(".trace") / "tmp" / "services"
_SHOT_DIR = Path(".trace") / "screenshots"
# view_images 合成的联系表：只作为「模型当轮看到的预览」落盘给前端出缩略图，
# run 收尾随暂存目录一起清掉（见 orchestration/tempfiles.SCRATCH_DIRS）。
_SHEET_DIR = Path(".trace") / "previews"

# 常见 Windows 浏览器位置（Chrome 优先，其次 Edge）
_BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _browser_exe() -> str:
    """找无头浏览器：env GA_BROWSER_PATH > PATH 里的 chrome/msedge > 常见安装路径。"""
    env = os.environ.get("GA_BROWSER_PATH", "").strip()
    if env and Path(env).is_file():
        return env
    for name in ("chrome", "msedge", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    for p in _BROWSER_CANDIDATES:
        if Path(p).is_file():
            return p
    raise ToolError(
        "找不到无头浏览器（Chrome/Edge）。可设置环境变量 GA_BROWSER_PATH 指向 "
        "chrome.exe，或改用 run_command 做非视觉验证。")


def _run_id(t: ToolContext) -> str:
    return str(getattr(t.run_ctx, "run_id", "") or "default")


def _fix_python(cmd: str, python_bin: str | None) -> str:
    if not python_bin:
        return cmd
    m = re.match(r'^("?)python\1(?=\s|$)', cmd, flags=re.I)
    if m:
        return f'"{python_bin}"' + cmd[m.end():]
    return cmd


def _log_path(workspace: Path, tag: str) -> Path:
    d = workspace / _LOG_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{tag}-{int(time.time() * 1000)}.log"


def _tail(path: Path, lines: int = 40) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return "(日志读取失败)"
    rows = text.splitlines()
    return "\n".join(rows[-lines:]) if rows else "(空)"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def _cmd_head(cmd: str) -> str:
    """兼容旧调用点的薄封装；解析口径统一在 native.cmd_head（覆盖引号路径）。"""
    return cmd_head(cmd)


def record_start_entry(workspace, cmd: str, url: str) -> dict:
    """起服务成功后落一条入口事实（service_start / service_url），返回真正新记的键。

    同键不覆盖：第一次撞通的那条最可信，后来换端口是任务特有的选择。
    记不上也不能影响「服务已经起来了」这个结果，所以整段吞异常。
    """
    if workspace is None:
        return {}
    entry = {"service_start": cmd}
    if url:
        entry["service_url"] = url
    try:
        existing = envfacts.load(workspace)
        fresh = {k: v for k, v in entry.items() if k not in existing}
        return fresh if fresh and envfacts.record(workspace, entry) else {}
    except Exception:  # noqa: BLE001
        return {}


async def start_service(args: dict, t: ToolContext) -> str:
    """后台启动服务并等它就绪（用于改完代码后的截图验收）。"""
    if t.run_ctx.fencing:
        await t.run_ctx.fencing.assert_valid()
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        raise ToolError("缺少 command（例：python -m uvicorn game_agent.main:app --port 8000）")
    # 配音路由硬规则：语音服务由 generate_audio 内部拉起，不许自己起。
    # 实测 2026-10-28（run 865be9fd）：用户只说「配音重新生成」，模型 start_service 起了 8860。
    from game_agent.agents.tools.audio_guard import (
        AUDIO_ROUTE_DENIED, audio_python_allowed, is_audio_server_command)
    if not audio_python_allowed(t.run_ctx.user_request) and is_audio_server_command(
            cmd, int(args.get("port") or 0)):
        raise ToolError(AUDIO_ROUTE_DENIED)
    # ① 危险动作：与 run_command 同一口径（删盘/格式化/关机/远程执行/强推），与是不是服务无关
    danger = _dangerous(cmd)
    if danger:
        raise ToolError(
            f"这条命令被拦：{danger}。这类操作不可逆，不在自动执行范围内。"
            "如果确实需要，请让人来执行，或改用只影响本项目的写法（限定路径、先备份）。")
    # ①·二 按进程名全杀浏览器：会连用户自己开着的窗口一起关掉（实测踩过）。
    if _browser_global_kill(cmd):
        raise ToolError(_BROWSER_KILL_MSG)
    # ② 白名单只作 strict 模式的兜底；默认 open 策略与 run_command 一致——
    #    否则 `"C:\Program Files\...\chrome.exe" ...`、`node tools/dev.mjs` 这类
    #    正常启动命令会在这里被无谓拦掉（用户实测截图：命令不在白名单中：program）。
    head = _cmd_head(cmd)
    if SHELL_POLICY == "strict":
        allowed = _effective_whitelist(t.workspace)
        allowed.add(_cmd_head(t.python_bin or ""))
        if head and head not in allowed:
            raise ToolError(_whitelist_error(head, t.workspace))
    port = int(args.get("port") or 0)
    wait_s = float(args.get("wait_s") or 25)
    ready_path = str(args.get("ready_path") or "/")
    if not ready_path.startswith("/"):
        ready_path = "/" + ready_path

    cmd = _fix_python(cmd, t.python_bin)
    # 组长直启 ComfyUI/IndexTTS2 也要先腾卡：两个本地服务同时驻留 = 显存 OOM，
    # 而且这时报错只会变成「服务起来了但一调就崩」，排查成本更高。
    gpu_kind = gpu_arbiter.kind_for_command(cmd)
    gpu_note = ""
    if gpu_kind:
        room = await gpu_arbiter.make_room(gpu_kind)
        gpu_note = gpu_arbiter.note(room)
    log = _log_path(t.workspace, "svc")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "PYTHONUNBUFFERED": "1"}
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    log_f = open(log, "ab")
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(t.workspace), stdout=log_f,
            stderr=subprocess.STDOUT, env=env, creationflags=flags, close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        log_f.close()
        raise ToolError(f"启动失败：{type(e).__name__}: {e}")

    rec = {"pid": proc.pid, "command": cmd, "port": port, "log": str(log),
           "started": time.time(), "_fh": log_f, "url": ""}
    if port:
        rec["url"] = f"http://127.0.0.1:{port}{ready_path}"
    _PROCS.setdefault(_run_id(t), {})[proc.pid] = rec
    register_temp(_run_id(t), log)  # 日志只为当轮排错服务，run 收尾删掉

    # 等端口就绪（没给 port 就只确认进程没立刻退出）
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _tail(log, 40)
            _PROCS.get(_run_id(t), {}).pop(proc.pid, None)
            try:
                log_f.close()
            except Exception:  # noqa: BLE001
                pass
            raise ToolError(
                f"服务启动后立即退出（exit={proc.returncode}）。命令：{cmd}\n"
                f"日志 {log} 尾部：\n{tail}")
        if port:
            if _port_open(port):
                break
        else:
            await asyncio.sleep(1.5)
            break
        await asyncio.sleep(0.4)
    else:
        raise ToolError(
            f"服务在 {int(wait_s)}s 内没有监听端口 {port}（进程仍存活，已保留）。"
            f"看 service_logs 确认是否换端口启动或卡在启动日志。")

    # 「这条命令在这个项目里真的能起起来」是起服务最贵的一手证据（实测换个 run 就得
    # 重新翻 README / package.json 猜一遍起法）。就绪了就顺手记一条入口事实，
    # 下个 run 的上下文里直接给出 service_start / service_url。
    entry_fresh = record_start_entry(t.workspace, cmd, rec["url"])
    entry_note = (" 这条起法已记入 " + envfacts.ENV_FILE + "（" + "、".join(entry_fresh) +
                  "），下个任务会直接给它，不用再猜启动命令。") if entry_fresh else ""

    return json.dumps({
        "state": "running", "pid": proc.pid, "command": cmd,
        "url": rec["url"], "port": port or None, "log": str(log),
        "hint": "下一步可用 screenshot_page 截图看效果；验收完 stop_service 收掉。"
                + entry_note + (f"（{gpu_note}）" if gpu_note else ""),
        "log_tail": _tail(log, 20),
    }, ensure_ascii=False)


async def service_logs(args: dict, t: ToolContext) -> str:
    """看本轮后台服务的最新日志（定位启动失败/运行报错）。"""
    pid = int(args.get("pid") or 0)
    lines = int(args.get("lines") or 60)
    procs = _PROCS.get(_run_id(t), {})
    if not procs:
        raise ToolError("本次任务没有后台服务在跑（先用 start_service 启动）")
    chosen = [procs[pid]] if pid in procs else list(procs.values())
    out = []
    for rec in chosen[-3:]:
        out.append(f"=== pid {rec['pid']} | {rec['command']} ===\n"
                   f"{_tail(Path(rec['log']), lines)}")
    return "\n\n".join(out)


async def stop_service(args: dict, t: ToolContext) -> str:
    """收掉后台服务（不传 pid = 收掉本轮全部）。"""
    pid = int(args.get("pid") or 0)
    rid = _run_id(t)
    procs = _PROCS.get(rid, {})
    if not procs:
        return "本次任务没有需要停止的服务。"
    targets = [pid] if pid in procs else list(procs.keys())
    stopped = []
    for p in targets:
        rec = procs.pop(p)
        _kill(p)
        try:
            rec["_fh"].close()
        except Exception:  # noqa: BLE001
            pass
        stopped.append(p)
    return f"已停止服务：{stopped}"


def _kill(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        else:
            os.kill(pid, 9)
    except Exception:  # noqa: BLE001
        pass


def kill_run_services(run_id: str) -> None:
    """run 结束时兜底清理：所有后台服务进程都要收掉，避免端口泄漏。"""
    for pid, rec in list(_PROCS.get(str(run_id), {}).items()):
        _kill(pid)
        try:
            rec["_fh"].close()
        except Exception:  # noqa: BLE001
            pass
    _PROCS.pop(str(run_id), None)


def _to_data_url(png: Path, max_width: int = 1024) -> str:
    """把截图压成 vision 能吃的 jpeg data-url（原图仍留在磁盘上）。

    1024 宽 / q78 是量出来的：判断「布局有没有溢出、元素对不对齐」用不到更细的像素，
    而回灌的 base64 每一张都要占满后续所有轮次的上下文——单图从 ~300KB 级降到 ~180KB 级。
    """
    try:
        from PIL import Image
        img = Image.open(png).convert("RGB")
        if img.width > max_width:
            h = int(img.height * max_width / img.width)
            img = img.resize((max_width, h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=78, optimize=True)
        raw = buf.getvalue()
    except Exception:  # noqa: BLE001 - 没装 Pillow 就直传 png 原字节
        raw = png.read_bytes()
        return "data:image/png;base64," + base64.b64encode(raw).decode()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode()


_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_MAX_IMG_BYTES = 12 * 1024 * 1024


def _persist_shot(p: Path, t: ToolContext) -> str:
    """把工具产出图另存到稳定目录（DATA_DIR/run_shots），返回 ``固定预览：<ref>`` 那一行。

    这些图原本只落在工作区 .trace/screenshots|previews，run 收尾会整目录清空，用户回看
    轨迹时缩略图全变「图片已清理」。稳定目录与工作区生命周期解耦，前端据标记改走
    /api/run-shots 取图。复制失败就返回空串（前端仍按原工作区路径兜底，不报错）。"""
    try:
        from game_agent.core import run_shots
        ref = run_shots.save_file(
            getattr(t.run_ctx, "team_id", None), _run_id(t), p)
    except Exception:  # noqa: BLE001 - 存不下只影响历史回看，不影响本轮
        return ""
    return f" 固定预览：{ref}" if ref else ""

# 同一张图在这个间隔内被重复回灌 → 判定它只是在盯着截图猜数字。
# 一次验证循环通常 3-6 次工具调用，4 次以内重看基本就是原地打转。
_REPEAT_VIEW_GAP = 4


def _view_note(t: ToolContext, key: str) -> str:
    """记下「这张图在第几次工具调用看过」，重复查看时改成一句节流提示。

    每张回灌图都会留在后续所有轮次的上下文里，反复看同一张既不省钱也不出结论；
    布局类判断本来有数值口径（溢出量是几个像素，不是"看着好像有一点点滚动条"）。
    """
    prev = t.last_view.get(key)
    t.last_view[key] = t.call_seq
    if prev is None or t.call_seq - prev >= _REPEAT_VIEW_GAP:
        return ""
    return (f"\n[这张图 {t.call_seq - prev} 次工具调用前刚看过。像素不会留到下一轮："
            "结论只有写进文件才会留下——美术判断写 .trace/art-review.md（一行一张图），"
            "页面问题用数值判定（run_command 跑一次 scrollHeight/clientHeight 对比），"
            "别重新打开同一张图。]")


def _resolve_local_image(url: str, ws: Path) -> Path | None:
    """把 file:// URL 或磁盘路径解析成图片文件；http(s) 或非图片一律返回 None。

    以前 screenshot_page 只认真 http(s)，组长想看「我自己刚截的那张 target/shots/x.png」
    时，URL 会被拼成 `http://file:///D:/...`，浏览器直接 ERR_NAME_NOT_RESOLVED。
    """
    s = str(url or "").strip().strip('"').strip("'")
    if not s or s.lower().startswith(("http://", "https://", "data:")):
        return None
    # 附图标记（chat-uploads/<team>/<file> 或老数据 .trace/uploads/<file>）：
    # 原图已不保证在工作区，统一交给 chat_images.resolve_ref 解析（含 DATA_DIR 回退）
    if s.replace("\\", "/").startswith(("chat-uploads/", ".trace/uploads/")):
        from game_agent.core import chat_images
        real = chat_images.resolve_ref(s, ws, None)
        if real is not None and real.is_file():
            return real
    if s.lower().startswith("file:"):
        from urllib.parse import unquote, urlparse
        p = unquote(urlparse(s).path or "")
        if re.match(r"^/[A-Za-z]:", p):      # /D:/GameSoftWare/... → D:/GameSoftWare/...
            p = p[1:]
        cand = Path(p)
    else:
        cand = Path(s)
        if not cand.is_absolute():
            cand = Path(ws) / cand
    if cand.suffix.lower() not in _IMG_EXT:
        return None
    try:
        return cand.resolve()
    except OSError:
        return None


async def view_image(args: dict, t: ToolContext) -> "ToolResult":
    """看磁盘上已有的图片（自己截的图、cdp 脚本出的图、美术出的图、用户放的参考图）。

    以前模型只能靠 screenshot_page 打开 http 页面，本地图片完全没有「看」的入口，
    于是它去读 base64、去 grep 源码猜画面——既慢又容易猜错。
    """
    raw = str(args.get("path") or args.get("url") or args.get("file") or "").strip()
    if not raw:
        raise ToolError("缺少 path（例：target/shots/manor-check.png 或 .trace/screenshots/shot-1.png）")
    p = _resolve_local_image(raw, t.workspace)
    if p is None:
        raise ToolError(f"不是图片文件：{raw}（支持 {'/'.join(x.lstrip('.') for x in _IMG_EXT)}）")
    if not p.is_file():
        raise ToolError(f"文件不存在：{raw}")
    size = p.stat().st_size
    if size == 0:
        raise ToolError(f"图片是空文件（0 字节）：{raw}")
    if size > _MAX_IMG_BYTES:
        raise ToolError(f"图片太大（{size // 1024 // 1024}MB > 12MB）：先缩图再看")
    try:
        rel = str(p.relative_to(t.workspace.resolve())).replace("\\", "/")
    except ValueError:
        rel = str(p)
    return ToolResult(
        text=(f"图片已附在本条消息中：{rel}（{size // 1024}KB）。"
              "直接目视核对，不要靠读源码或猜。" + _view_note(t, str(p))
              + _persist_shot(p, t)),
        images=[_to_data_url(p)],
    )


_MAX_SHEET = 9  # 一次合并的图片张数上限（再多每格就小到看不清了）


def _sheet_data_url(items: list[tuple[str, "object"]], cell: int = 512,
                    cols: int = 3, max_edge: int = 1400) -> tuple[str, int, "object"]:
    """把若干 PIL 图拼成一张联系表 → (jpeg data-url, 拼了几张, PIL 合图)。

    第三个返回值留了 PIL 合图本身，调用方要落盘给前端出缩略图时直接用，
    不必再解码一遍 data-url。

    格子等比内切、白底铺平（透明图摊白，不然 JPEG 编码会失败）。
    整张长边再压到 1400：并排比对构图/配色够用，比 9 张原图省一个数量级。
    """
    from PIL import Image, ImageDraw
    from game_agent.llm.context_window import encode_vision_image

    n = len(items)
    cols = min(cols, n)
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    for k, (_, img) in enumerate(items):
        im = img
        if im.mode != "RGB":
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            im = flat
        im = im.copy()
        im.thumbnail((cell - 16, cell - 40), Image.LANCZOS)
        left, top = (k % cols) * cell, (k // cols) * cell
        x = left + (cell - im.width) // 2
        y = top + 32 + (cell - 40 - im.height) // 2
        sheet.paste(im, (x, y))
        draw.text((left + 12, top + 6), f"#{k + 1}", fill="white", font_size=22)
    return encode_vision_image(sheet, max_edge=max_edge), n, sheet


async def view_images(args: dict, t: ToolContext) -> "ToolResult":
    """把最多9张选中的美术图合并压缩，通过 images 真正送入模型视野。"""
    raws = args.get("paths")
    if not isinstance(raws, list) or not raws or any(
            not isinstance(p, str) or not p.strip() for p in raws):
        raise ToolError("paths 必须是非空图片路径数组，一次最多9张")
    raws = [p.strip() for p in raws]
    if len(raws) > _MAX_SHEET:
        raise ToolError(f"一次最多合并 {_MAX_SHEET} 张（现在给了 {len(raws)} 张），"
                        "先挑出真正要比的，或分两次调用")
    from PIL import Image
    loaded: list[tuple[str, Image.Image]] = []
    skipped: list[str] = []
    for raw in raws:
        p = _resolve_local_image(raw, t.workspace)
        if p is None or not p.is_file():
            skipped.append(f"{raw}（不是图片或文件不存在）")
            continue
        if p.stat().st_size > _MAX_IMG_BYTES:
            skipped.append(f"{raw}（超过12MB，请先缩图）")
            continue
        try:
            with Image.open(p) as source:
                source.thumbnail((512, 512), Image.Resampling.LANCZOS)
                img = source.copy()
        except (OSError, ValueError, Image.DecompressionBombError) as e:
            skipped.append(f"{raw}（读不出：{type(e).__name__}）")
            continue
        try:
            rel = str(p.relative_to(t.workspace.resolve())).replace("\\", "/")
        except ValueError:
            rel = str(p)
        loaded.append((rel, img))
    if not loaded:
        raise ToolError("没有一张图能读出来：" + "；".join(skipped)[:400])
    try:
        url, n, sheet = _sheet_data_url(loaded)
    finally:
        for _, image in loaded:
            image.close()
    # 合图落盘：模型那一侧只吃 data-url，但前端轨迹要能像截图一样出缩略图。
    # 落到 .trace/previews/（run 收尾随暂存目录清掉），把工作区相对路径写进结果文本，
    # 前端 ShotThumbs 认「合图预览：<path>」这个标记就能渲染。
    sheet_rel = ""
    shot_mark = ""
    try:
        d = t.workspace / _SHEET_DIR
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"sheet-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}.jpg"
        sheet.save(out, format="JPEG", quality=82, optimize=True)
        register_temp(_run_id(t), out)
        sheet_rel = str(out.relative_to(t.workspace.resolve())).replace("\\", "/")
        shot_mark = _persist_shot(out, t)
    except (OSError, ValueError) as e:  # noqa: BLE001 - 存不下来只影响缩略图，不影响模型看图
        log.warning("联系表落盘失败（不影响模型看图）：%s", e)
    finally:
        try:
            sheet.close()
        except Exception:  # noqa: BLE001
            pass
    grid = "、".join(f"第{i + 1}格={rel}" for i, (rel, _) in enumerate(loaded))
    kb = (len(url) - 23) * 3 // 4 // 1024
    return ToolResult(
        text=(f"已把 {n} 张图拼成一张联系表（左到右、上到下排列）附在本条消息中：{grid}"
              f"。合图约 {kb}KB，只发送这张压缩合图，原图文件不变。"
              + (f"（合图预览：{sheet_rel}）" if sheet_rel else "")
              + "要检查选中图片的细节请再用 view_image。"
              + (f" 跳过：{'；'.join(skipped)[:200]}" if skipped else "")
              + shot_mark),
        images=[url])


async def screenshot_page(args: dict, t: ToolContext) -> "ToolResult":
    """用无头浏览器截图并回灌给模型（组长据此判断「页面上真实长什么样」）。"""
    url = str(args.get("url") or "").strip()
    if not url:
        raise ToolError("缺少 url（例：http://127.0.0.1:8000/runs）")
    # 本地图片（file:// 或磁盘路径）不走浏览器：直接回灌视觉，别再拼 http://
    local = _resolve_local_image(url, t.workspace)
    if local is not None and local.is_file():
        return await view_image({"path": str(local)}, t)
    if re.fullmatch(r"\d{2,5}", url):
        url = f"http://127.0.0.1:{url}/"
    if not url.lower().startswith(("http://", "https://")):
        if url.startswith("/"):
            url = "http://127.0.0.1:8000" + url
        else:
            url = "http://" + url
    width = int(args.get("width") or 1440)
    height = int(args.get("height") or 900)
    wait_ms = int(args.get("wait_ms") or 1200)
    rel_out = str(args.get("out") or "").strip()
    if rel_out:
        out = t.workspace / rel_out
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        d = t.workspace / _SHOT_DIR
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"shot-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}.png"
        # 自动命名的截图是给模型当轮判断用的，run 收尾删掉；
        # 显式传了 out 的是用户要的交付物，不碰。
        register_temp(_run_id(t), out)

    exe = _browser_exe()
    profile = tempfile.mkdtemp(prefix="ga-shot-")
    cmd = [
        exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        f"--user-data-dir={profile}", f"--window-size={width},{height}",
        f"--screenshot={out}", f"--virtual-time-budget={wait_ms}",
        url,
    ]
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, timeout=90)
        note = (proc.stderr or b"").decode("utf-8", errors="replace")[-600:]
    except subprocess.TimeoutExpired:
        raise ToolError("截图超时（>90s）：页面可能一直在加载，先确认服务正常响应")
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    if not out.is_file() or out.stat().st_size == 0:
        raise ToolError(f"截图未生成（url={url}）。浏览器输出：{note or '(无)'}")

    rel = str(out.relative_to(t.workspace.resolve())).replace("\\", "/")
    size = out.stat().st_size
    return ToolResult(
        text=(f"截图已保存：{rel}（{width}x{height}，{size // 1024}KB）；"
              f"图片已附在本条消息中，请直接目视核对页面效果。"
              + _view_note(t, str(out)) + _persist_shot(out, t)),
        images=[_to_data_url(out)],
    )
