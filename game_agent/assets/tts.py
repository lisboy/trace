"""IndexTTS2 本地语音合成客户端（配音 / 语音工作台共用）。

为什么单独一个模块而不是塞进 specialized.py：
- agent 的 tts 工具、界面的语音工作台、对话里的「朗读」按钮都要用它，
  三处共用一套超时/SSE 解析/错误翻译，避免各写一份再各自踩坑；
- 它跟 ComfyUI 的 assets/comfyui.py 是同一层级的东西（本机重模型服务客户端）。

几条实测出来的硬约束，写在这里防止以后被改回去：
1. `/api/v1/tts` 返回的是 SSE 流，不是普通 JSON —— 必须流式读，逐行取 `data:`。
2. `done` 事件里的 `wav` 只是相对路径，成品还得再 GET 一次才拿到字节。
3. 单卡 8G 服务端串行推理：一句 20~60 秒，首次还要 30~60 秒加载模型，
   所以超时下限 300 秒，别按普通 HTTP 接口的 10 秒设。
4. 服务不可达要能快速失败（连接不上时别等满超时），健康检查用 3 秒短超时。
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from game_agent.assets import gpu_arbiter
from game_agent.assets import media_queue
from game_agent.config import (
    TTS_AUTOSTART, TTS_BASE_URL, TTS_EMO_AUDIO, TTS_EMO_MODE, TTS_EMO_TEXT,
    TTS_EMO_VECTOR, TTS_EMO_WEIGHT, TTS_HOME, TTS_LAUNCH_CMD, TTS_TIMEOUT_S)
from game_agent.logging_setup import get_logger

log = get_logger("assets.tts")

# IndexTTS2 的情感控制有三条独立通路（服务端 /api/v1/tts 的 emo_mode）：
#   0 = 沿用音色参考音频自带的情绪（默认，最容易听成"平"）
#   1 = 情感参考音频 emo_audio（另给一段"带目标情绪"的音频，用它注入情绪）
#   2 = 八维情感向量 emo_vector（高兴/愤怒/悲伤/害怕/厌恶/忧郁/惊讶/平静 的权重）
#   3 = 文本描述 emo_text（走 Qwen 情感分析，把"愤怒地质问"变成情感向量）
# 只给音色参考音频时模型没有情绪线索，就会用平静语气——这也是"三句一个调"的根因。
EMO_VEC_DIM = 8
EMO_VEC_NAMES = ("高兴", "愤怒", "悲伤", "害怕", "厌恶", "忧郁", "惊讶", "平静")


class TTSError(RuntimeError):
    """语音服务调用失败（不可达 / 返回 error / 没拿到音频）。"""


def parse_emo_vector(value: Any) -> list[float] | None:
    """把「8 维情感权重」解析成定长浮点列表（接受 list 或 "0.8,0,0,..." 字符串）。

    服务端要求严格 8 个分量、顺序固定（高兴/愤怒/悲伤/害怕/厌恶/忧郁/惊讶/平静）。
    缺位补 0、多位截断；全 0 视同没给（返回 None），免得把"没情绪"当成 mode=2 送出去。
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        vals = list(value)
    else:
        s = str(value).strip()
        if not s:
            return None
        vals = [x for x in re.split(r"[,\s]+", s) if x]
    out: list[float] = []
    for x in vals[:EMO_VEC_DIM]:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            out.append(0.0)
    if not any(out):
        return None
    out += [0.0] * (EMO_VEC_DIM - len(out))
    return out


async def health(base_url: str = "", timeout_s: float = 3.0) -> dict[str, Any]:
    """探测语音服务。返回 {enabled, reachable, model_loaded, design_ready, error, base_url}。

    永不抛异常：状态栏/下拉框要能一直渲染，服务挂了只是显示"未连接"。
    传了显式 base_url（如档位里那个旧端口）时走 _base()：一旦系统把配置端口改绑，
    这里要跟着映射到实际端口，否则状态栏永远显示「未连接」。
    """
    eff = (_base(base_url or actual_base() or "") if (base_url or actual_base())
           else "").strip().rstrip("/")
    info: dict[str, Any] = {
        "enabled": bool(eff),
        "reachable": False, "model_loaded": False, "design_ready": False,
        "error": None, "base_url": eff,
    }
    if not info["enabled"]:
        info["error"] = "未配置 TTS_BASE_URL"
        return info
    try:
        async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as c:
            r = await c.get(f"{info['base_url']}/api/v1/model")
            if r.status_code != 200:
                info["error"] = f"/api/v1/model HTTP {r.status_code}"
                return info
            info["model_loaded"] = bool(r.json().get("loaded"))
            info["reachable"] = True
            try:
                d = await c.get(f"{info['base_url']}/api/v1/voice-design/status")
                info["design_ready"] = bool(d.json().get("ready")) if d.status_code == 200 else False
            except Exception:  # noqa: BLE001 - 老版本服务没有这个接口
                info["design_ready"] = False
    except Exception as e:  # noqa: BLE001
        info["error"] = f"连不上语音服务：{type(e).__name__}: {e}"
    return info


_AUTOSTART_LOCK = asyncio.Lock()

# 最近一次由本进程发起的语音服务启动记录（跟 comfyui._LAUNCH_RECORD 同一套思路）：
# 同轮对话里语音服务可能已在启动中（端口未就绪），探活失败不能直接再拉一个——
# 双实例抢同一块卡就是"生图/配音全失败"的经典来源。
_LAUNCH_RECORD: dict | None = None

# 启动失败熔断：base -> (monotonic, error)，60s 内重复 ensure 直接快速失败
_ENSURE_FAIL_CACHE: dict[str, tuple[float, str]] = {}
_ENSURE_FAIL_TTL_S = 60.0

# 实际在用的语音服务地址覆盖：配置里的 TTS_BASE_URL 端口落在 Windows「保留/排除端口段」时，
# 服务端 bind 会立刻失败（WinError 10013 "以一种访问权限不允许的方式做了一个访问套接字的
# 尝试"，一个包都不发），启动器拿到的探活一直是「未就绪」，日志只有 ^C^C^C^C。
# 2026-11-05 实测：Hyper-V/WinNAT 动态保留把 8851-8950 整段排除，原设的 8860 正好落在里面，
# 语音服务连续 5 次起不来。这里记住「实际可用端口」，让之后连它的地方（合成/健康/列表）
# 都改走它，不必改库里的档位配置。_ACTUAL_BASE 非空时覆盖 TTS_BASE_URL。
# 2026-11-10 用户要求：端口统一用 10000 以上（避开 Windows 保留段，也避开低位常用端口），
# 被占用/被系统保留时自动改绑到 10000 以上的空闲端口（与 ComfyUI 同一套规则）。
_ACTUAL_BASE: str | None = None
# 端口下限：改绑候选一律 >10000（用户要求）。
_PORT_FLOOR = 10000
# 端口被系统保留时的自动改绑候选：先从配置端口往上跳几个，再试本机高位空闲区。
# 7860 是 Windows 保留段 7781-7880 的常客；18888 是 game_agent 后端自身端口——都排除。
_PORT_BLOCKED_SKIP = {7860, 8860, 18888}


def actual_base() -> str:
    """实际应连接的语音服务地址（优先用自动改绑后的端口，否则用配置值）。"""
    return (_ACTUAL_BASE or TTS_BASE_URL or "").strip().rstrip("/")


def _cfg_base() -> str:
    return (TTS_BASE_URL or "").strip().rstrip("/")


def _cfg_port() -> int:
    try:
        return urlparse(_cfg_base()).port or 0
    except Exception:  # noqa: BLE001
        return 0


def _candidate_ports() -> list[int]:
    """配置端口 + 已改绑端口（去重、保序）——停止/改绑/发现外部实例时用。"""
    ports: list[int] = []
    for raw in (TTS_BASE_URL, _ACTUAL_BASE):
        try:
            p = urlparse((raw or "").strip()).port
        except Exception:  # noqa: BLE001
            p = None
        if p and p not in ports:
            ports.append(p)
    return ports


def _all_bases() -> list[str]:
    """配置地址 + 已改绑地址（去重、保序；空忽略）。停止服务时两处都要看一眼。"""
    out: list[str] = []
    for raw in (TTS_BASE_URL, _ACTUAL_BASE):
        b = (raw or "").strip().rstrip("/")
        if b and b not in out:
            out.append(b)
    return out


def discover_external(probe: bool = True) -> str | None:
    """同步找一个「外部已拉起」的语音服务实际地址（端口可能 ≠ 配置端口）。

    背景：用户手动起服务 / 旧实例还在跑时，实际端口可能与配置不同。配置端口探不通时，
    API 层（/status、/start）调它把 _ACTUAL_BASE 记下来，让后续探活/合成/状态栏跟随。
    probe=True 时逐个候选做真实 HTTP 探活（同步 httpx，供在线程里跑）。
    """
    global _ACTUAL_BASE
    ext = _find_starting_tts()
    cands: list[int] = []
    if ext and ext.get("port"):
        cands.append(int(ext["port"]))
    for p in _candidate_ports():
        if p not in cands:
            cands.append(p)
    _u = urlparse((TTS_BASE_URL or "").strip())
    host = _u.hostname or "127.0.0.1"
    scheme = _u.scheme or "http"
    for p in cands:
        if not p or p < 1024:
            continue
        base = f"{scheme}://{host}:{p}"
        if probe:
            try:
                with httpx.Client(timeout=1.5, trust_env=False) as c:
                    if c.get(f"{base}/api/v1/model").status_code != 200:
                        continue
            except Exception:  # noqa: BLE001
                continue
        _ACTUAL_BASE = base
        log.info("检测到外部语音服务实际端口 %s（配置 %s），探活跟随", base, TTS_BASE_URL)
        return base
    return None


async def _discover_external(probe: bool = True) -> str | None:
    return await asyncio.to_thread(discover_external, probe)


def _maybe_rebind(base: str) -> str:
    """配置端口 bind 不了（系统保留 WinError 10013 / 被占用 10048）→ 改绑到 >10000 的可用
    端口并记下 _ACTUAL_BASE。只在「本机 + 尚未改绑 + 该端口确实 bind 不了」时动；远端地址
    / 本来就能用的端口原样返回。改绑只动 base，库里的档位配置不用改。

    关键排除：端口若正被「外部 IndexTTS 实例」占用（它还在加载、暂时探不通但进程在），
    那是同一台服务的启动中，不是「系统保留」——不能改绑，否则会另起一个实例（双开抢卡）。
    """
    global _ACTUAL_BASE
    if _ACTUAL_BASE is not None:
        return base
    u = urlparse(base)
    if u.hostname not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return base
    if not u.port or _port_bindable(u.port):
        return base
    ext = _find_starting_tts()
    if ext is not None and (not ext.get("port") or ext.get("port") == u.port):
        # 外部实例正占用该端口（加载中 / 探测暂不可达）→ 视为「在启动」，不改绑
        return base
    alt = _pick_alt_base(base)
    if alt:
        log.warning("语音服务端口 %s 被系统保留/占用（WinError 10013/10048 一类），"
                    "自动改绑到 %s", base, alt)
        _ACTUAL_BASE = alt
        return alt
    return base


def _base(base_url: str = "") -> str:
    req = (base_url or "").strip().rstrip("/")
    # 显式传入的地址若正是「配置里那个被系统拒绝、已自动改绑」的端口 → 用改绑后的，
    # 否则 specialized._indextts_generate 一路把档位里的旧端口传下来，HTTP 层照样打死。
    if req and _ACTUAL_BASE and req == _cfg_base():
        return _ACTUAL_BASE
    base = (req or actual_base() or "").strip().rstrip("/")
    if not base:
        raise TTSError("未配置语音服务地址（.env TTS_BASE_URL，例：http://127.0.0.1:10189）")
    return base


def _port_bindable(port: int) -> bool:
    """本机 127.0.0.1:port 能否 bind。WinError 10013（系统保留段）会被判为不可用。"""
    if not port:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pick_alt_base(base: str) -> str | None:
    """配置端口 bind 不了时挑一个可用端口，返回改绑后的 base URL（保持 host/协议）。
    只在「本机且配置端口确实被系统拒绝/占用」时用；远端地址一律不动。
    候选全部 >10000（用户要求）：先从配置端口往上 +100 步进探测，再试几个高位空闲区。"""
    u = urlparse(base)
    host = (u.hostname or "")
    if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return None
    cur = u.port
    if not cur:
        return None
    if _port_bindable(cur):
        return None  # 配置端口本来就能用，不改
    # 近邻上跳：从 max(配置端口+100, 10000+100) 起步，确保候选全部 >10000
    start = max(cur + 100, _PORT_FLOOR + 100)
    candidates: list[int] = [start + 100 * i for i in range(0, 6)]
    candidates += [10188, 10189, 10288, 10888, 11088, 12088, 13088, 15088, 18088, 21088]
    for p in candidates:
        if p in _PORT_BLOCKED_SKIP or p <= _PORT_FLOOR or p > 65535:
            continue
        if _port_bindable(p):
            return f"{u.scheme or 'http'}://{host}:{p}"
    return None

# 启动日志：IndexTTS2 起不来时（依赖缺失/端口被占/显存不足）有据可查
BOOT_LOG_PATH = None  # 启动时按 TTS_HOME 赋值（模块导入时 config 可能还没就绪）


def _boot_log_path() -> Path:
    global BOOT_LOG_PATH
    if BOOT_LOG_PATH is None:
        try:
            from game_agent.config import LOG_DIR
            BOOT_LOG_PATH = LOG_DIR / "tts_boot.log"
        except Exception:  # noqa: BLE001
            BOOT_LOG_PATH = Path.home() / ".trace" / "logs" / "tts_boot.log"
    return BOOT_LOG_PATH


def _read_boot_log_tail(n: int = 25) -> str:
    try:
        lines = _boot_log_path().read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])[-2000:]
    except OSError:
        return ""


def _find_starting_tts() -> dict | None:
    """找「外部已拉起、正在启动中」的 IndexTTS2 进程（cmdline 带 TTS_HOME / indextts / server.py）。

    返回里带 port：优先从 cmdline 解析 --port；解析不到就从该进程（含子进程）的监听端口
    反查——覆盖「用户手动起、端口 ≠ 配置端口」与「shell 秒退、真身是子进程」的场景，
    让状态栏/探活跟随实际端口，避免永远显示「未连接」。
    """
    try:
        import psutil
    except ImportError:
        return None
    home = str(TTS_HOME or "").lower().replace("\\", "/").rstrip("/")
    me = psutil.Process().pid
    cfg_port = _cfg_port()
    for p in psutil.process_iter(["pid", "cmdline"]):
        if p.info["pid"] == me:
            continue
        cl = " ".join(p.info.get("cmdline") or [])
        if not cl:
            continue
        low = cl.lower().replace("\\", "/")
        if "game_agent" in low or "pytest" in low:
            continue
        hit = (home and home in low) or "indextts" in low or (
            "server.py" in low and str(cfg_port or 10189) in low)
        if hit and "python" in low:
            info = {"pid": p.info["pid"], "cmdline": cl[:200]}
            m = re.search(r"--port\s+(\d+)", cl)
            if m:
                try:
                    info["port"] = int(m.group(1))
                except ValueError:
                    pass
            if "port" not in info:
                # 命令行没有 --port → 从进程树监听端口反查（取最小的高位端口，通常只有一个 API 端口）
                try:
                    proc = psutil.Process(p.info["pid"])
                    procs = [proc] + proc.children(recursive=True)
                except psutil.NoSuchProcess:
                    procs = []
                ports: set[int] = set()
                for q in procs:
                    try:
                        for c in q.net_connections(kind="inet"):
                            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port >= 1024:
                                ports.add(c.laddr.port)
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        continue
                if ports:
                    info["port"] = min(ports)
            return info
    return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        return True  # 探测不了就按还活着处理，宁可多等不双开


def _launch_cmd(base: str) -> tuple[str, str | None]:
    """拼 IndexTTS2 启动命令与工作目录（自定义命令优先，否则用 TTS_HOME）。"""
    if TTS_LAUNCH_CMD:
        return TTS_LAUNCH_CMD, None
    home = Path(TTS_HOME)
    py = home / "runtime" / "python.exe"
    srv = home / "server.py"
    if py.is_file() and srv.is_file():
        port = urlparse(base).port or 10189
        return f'"{py}" -u "{srv}" --port {port}', str(home)
    return "", None


def _align_launch_cmd(cmd: str, base: str) -> str:
    """自定义 TTS_LAUNCH_CMD 里的 --port 对齐到本次探活的 base URL（防止与 comfyui 同类的
    「启动绑 A 端口、探活看 B 端口」错配）。只改写 --port，不动其它参数。"""
    import re as _re
    port = urlparse(base).port
    if not port or not _re.search(r"--port\s+\S+", cmd):
        return cmd
    out = _re.sub(r"--port\s+\S+", f"--port {port}", cmd)
    if out != cmd:
        log.info("语音服务启动命令端口对齐：%s -> 探活地址 %s", cmd, base)
    return out


async def ensure_tts_running(base_url: str = "", timeout_s: int = 240) -> dict[str, Any]:
    """确保语音服务可用；未运行则自动拉起并轮询等待。

    启动状态校验（与 comfyui.ensure_comfyui_running 同一套规则，防双开抢卡）：
    1. 探活成功 → 直接返回；2. 本进程最近发起过启动且进程还活着 → 只等；
    3. 外部已拉起的实例还在启动 → 也只等；4. 都不是 → 才真正启动。
    启动输出落到 tts_boot.log，超时时把日志尾部带回；60s 内重复调用熔断快速失败。

    返回 {started, waited_ms, error}；error 非空表示拉不起来。
    GA_TTS_AUTOSTART=false 时直接返回（不自动启动）。
    """
    global _LAUNCH_RECORD, _ACTUAL_BASE
    base = (base_url or TTS_BASE_URL or "").strip().rstrip("/")
    if not base or not TTS_AUTOSTART:
        return {"started": False, "waited_ms": 0, "error": None}
    # 调用方（如 gpu_arbiter._start）常把配置里的地址原样传进来：一旦已自动改绑，
    # 这个旧地址要映射到改绑后的端口，否则每次都会去 bind 那个被系统拒绝的端口。
    config_base = _cfg_base()
    if _ACTUAL_BASE and base == config_base:
        base = _ACTUAL_BASE
    if (await health(base))["reachable"]:
        if _LAUNCH_RECORD is not None:
            log.info("语音服务已就绪（%s），清除启动记录", base)
            _LAUNCH_RECORD = None
        _ENSURE_FAIL_CACHE.pop(base, None)
        return {"started": False, "waited_ms": 0, "error": None}
    # 配置端口探不通：先看是不是「外部已拉起、端口 ≠ 配置」的实例（用户手动起 / 旧实例），
    # 找到就跟随它的实际端口并直接返回就绪，别对着配置端口空等、也别再拉一个（双实例抢同一块卡）。
    ext_base = await _discover_external(probe=True)
    if ext_base:
        log.info("语音服务已在外部就绪（%s），无需启动", ext_base)
        _LAUNCH_RECORD = None
        _ENSURE_FAIL_CACHE.pop(ext_base, None)
        return {"started": False, "waited_ms": 0, "error": None}
    # 配置端口被系统保留（WinError 10013）/ 被占用（10048）→ 服务端永远无法 bind，
    # 趁早改绑到 >10000 的可用端口，而不是傻等 240s 再报「未就绪」。改绑只动 base，
    # _ACTUAL_BASE 让后续合成/健康都走新端口；库里的档位配置不用改。
    base = _maybe_rebind(base)
    cached = _ENSURE_FAIL_CACHE.get(base)
    if cached and time.monotonic() - cached[0] < _ENSURE_FAIL_TTL_S:
        log.warning("语音服务 %s 刚启动失败过，熔断快速失败：%s", base, cached[1][:120])
        return {"started": False, "waited_ms": 0, "error": cached[1]}
    # 起语音前先停 ComfyUI 并等显存真正释放：两个本地服务同时驻留就是 OOM。
    # 必须放在 _AUTOSTART_LOCK 之外——GPU 锁的持有者也会走进这个函数来抢 _AUTOSTART_LOCK，
    # 在锁里面等 GPU 锁就是死锁。
    room = await gpu_arbiter.make_room("voice")
    if room.get("stopped"):
        log.info("启动语音服务前已腾出显存：%s", room["stopped"])
    async with _AUTOSTART_LOCK:
        if (await health(base))["reachable"]:      # 并发合成时双检
            _LAUNCH_RECORD = None
            return {"started": False, "waited_ms": 0, "error": None}
        t0 = time.monotonic()
        started_here = False

        ours = _LAUNCH_RECORD
        if ours is not None and _pid_alive(ours.get("pid")):
            log.info("语音服务正在启动中（本进程 %.0fs 前发起，pid=%s，%s），等待就绪不重复拉起",
                     t0 - ours["at"], ours["pid"], base)
        else:
            ext = await asyncio.to_thread(_find_starting_tts)
            if ext is not None:
                # 外部实例绑的端口可能 ≠ 配置端口（用户手动起 / 旧实例）：跟随它的实际端口
                # 探活，否则会对着配置端口空等 240s，表现为「我都启动完了，它还显示未连接」。
                if ext.get("port") and ext["port"] != (urlparse(base).port or 0):
                    _gu = urlparse(base)
                    base = (f"{_gu.scheme or 'http'}://"
                            f"{_gu.hostname or '127.0.0.1'}:{ext['port']}")
                    _ACTUAL_BASE = base
                log.info("检测到外部语音服务正在启动（pid=%s，端口=%s：%s），等待就绪不重复拉起",
                         ext["pid"], ext.get("port"), ext["cmdline"])
            else:
                if ours is not None:
                    log.info("上次启动的语音服务进程（pid=%s）已退出且未就绪，重新拉起", ours.get("pid"))
                _LAUNCH_RECORD = None
                # 真要先停 ComfyUI 再起自己：两个本地服务同时驻留就是显存 OOM，
                # 而「谁后起谁抢」在这里必须显式做，不能等合成时才发现。
                room = await gpu_arbiter.make_room("voice")
                if room.get("stopped"):
                    log.info("启动语音服务前已腾出显存：%s", room["stopped"])
                cmd, cwd = _launch_cmd(base)
                if not cmd:
                    log.error("找不到 IndexTTS2 启动入口（TTS_HOME=%s）", TTS_HOME)
                    return {"started": False, "waited_ms": 0,
                            "error": f"未找到 IndexTTS2 启动入口（TTS_HOME={TTS_HOME}），"
                                     f"请检查 runtime\\python.exe 与 server.py，或配置 TTS_LAUNCH_CMD"}
                cmd = _align_launch_cmd(cmd, base)
                try:
                    # 同步 Popen 仅创建进程（瞬时返回），等价于 start_server.bat 里的直启方式；
                    # DETACHED_PROCESS 让其脱离后端进程生命周期，后端重启不会带走语音服务
                    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                    boot_log = _boot_log_path()
                    boot_log.parent.mkdir(parents=True, exist_ok=True)
                    boot_fh = open(boot_log, "ab")  # noqa: SIM115 - 进程生命周期内持有
                    proc = subprocess.Popen(
                        cmd, shell=True, cwd=cwd,
                        stdout=boot_fh, stderr=subprocess.STDOUT,
                        creationflags=flags, close_fds=True)
                    boot_fh.close()
                    started_here = True
                    _LAUNCH_RECORD = {"at": time.monotonic(), "pid": proc.pid}
                    log.info("语音服务启动命令已发起（pid=%s，%s，启动日志=%s）",
                             proc.pid, base, boot_log)
                except Exception as e:  # noqa: BLE001
                    log.exception("语音服务启动命令执行失败")
                    err = f"启动语音服务失败：{type(e).__name__}: {e}"
                    _ENSURE_FAIL_CACHE[base] = (time.monotonic(), err)
                    return {"started": False, "waited_ms": 0, "error": err}
        while time.monotonic() - t0 < timeout_s:
            await asyncio.sleep(3)
            if (await health(base))["reachable"]:
                waited = int((time.monotonic() - t0) * 1000)
                log.info("语音服务就绪（%s，等待 %dms，本次%s发起）",
                         base, waited, "由本进程" if started_here else "外部/早前")
                _LAUNCH_RECORD = None
                return {"started": started_here, "waited_ms": waited, "error": None}
        tail = _read_boot_log_tail()
        err = (f"已执行启动命令但 {timeout_s}s 内未就绪（pid={(_LAUNCH_RECORD or {}).get('pid')}）"
               + (f"，启动日志尾部：{tail[-400:]}" if tail else
                  f"，可查看 {TTS_HOME}\\server_bg.log"))
        _ENSURE_FAIL_CACHE[base] = (time.monotonic(), err)
        log.error("语音服务 %ds 内未就绪（%s）\n启动日志尾部：\n%s", timeout_s, base, tail or "（无）")
        return {"started": False, "waited_ms": int((time.monotonic() - t0) * 1000), "error": err}


async def stop_tts_service(base_url: str = "") -> dict[str, Any]:
    """停掉监听该端口的语音服务进程（含子进程）。

    只杀「监听 TTS 端口」的那个进程树，不按进程名模糊匹配，
    避免误杀别的 python。端口可能已自动改绑（_ACTUAL_BASE）或用户手动起在别的端口：
    这里把配置端口 + 实际端口都纳入（去重），谁在监听就停谁。
    """
    import psutil

    base = _base(base_url)
    if not base:
        raise TTSError("未配置语音服务地址（.env TTS_BASE_URL）")
    ports: list[int] = []
    for b in [base, *_all_bases()]:
        try:
            p = urlparse(b).port
        except Exception:  # noqa: BLE001
            p = None
        if p and p not in ports:
            ports.append(p)
    if not ports:
        raise TTSError("语音服务地址缺少端口")

    def _kill() -> tuple[list[str], list[int]]:
        killed: list[str] = []
        hit_ports: list[int] = []
        for c in psutil.net_connections(kind="inet"):
            if (c.laddr and c.laddr.port in ports and c.status == psutil.CONN_LISTEN
                    and c.pid):
                try:
                    proc = psutil.Process(c.pid)
                    children = proc.children(recursive=True)
                    for p in [proc, *children]:
                        try:
                            p.kill()
                        except psutil.NoSuchProcess:
                            pass
                    killed.append(f"{c.pid}({proc.name()})")
                    if c.laddr.port not in hit_ports:
                        hit_ports.append(c.laddr.port)
                except psutil.NoSuchProcess:
                    pass
        return killed, hit_ports

    killed, hit_ports = await asyncio.to_thread(_kill)
    return {"stopped": bool(killed), "killed": killed,
            "port": hit_ports[0] if hit_ports else ports[0], "ports": ports}


async def list_presets(base_url: str = "") -> list[str]:
    """列出 IndexTTS2 服务端的音色预设（预设 = 参考音频 + 默认参数）。"""
    base = _base(base_url)
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
            r = await c.get(f"{base}/api/v1/presets")
            if r.status_code != 200:
                raise TTSError(f"取音色列表失败 HTTP {r.status_code}")
            return list(r.json().get("presets") or [])
    except TTSError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TTSError(f"取音色列表失败：{type(e).__name__}: {e}")


async def create_preset(
    name: str,
    audio: bytes,
    *,
    filename: str = "ref.wav",
    base_url: str = "",
    overwrite: bool = False,
    emo_weight: float | None = None,
    emo_text: str = "",
    emo_vector: Any = None,
    emo_audio: bytes | None = None,
    emo_audio_name: str = "emo.wav",
) -> dict[str, Any]:
    """用一段「现成音频」直接存成 IndexTTS2 音色预设（上传即预设）。

    和 design_save_preset 的区别：那条路只能存 VoxCPM2 造出来的候选（服务端只认
    voice_design 目录里的文件）；这里把用户上传/上传后合成得到的字节直接 POST 给
    IndexTTS2 的 /api/v1/presets（multipart: name + prompt_audio）。
    预设一旦落盘，合成侧（synthesize 只传 preset 名）和 agent 侧
    list_voice_presets / generate_audio(voice=...) 都能按名字复用它。

    返回服务端响应 {presets: [...]}；重名且未 overwrite 时抛 TTSError（文案含「已存在」）。
    """
    base = _base(base_url)
    if not str(name or "").strip():
        raise TTSError("预设名不能为空")
    if not audio:
        raise TTSError("音频为空")
    data: dict[str, Any] = {"name": str(name).strip()}
    if overwrite:
        data["overwrite"] = "true"
    if emo_weight is not None:
        data["emo_weight"] = str(emo_weight)
    if emo_text:
        data["emo_text"] = emo_text
    vec = parse_emo_vector(emo_vector)
    if vec is not None:
        # 存预设时把 8 维情感一起带上：之后按名复用这个音色时，情绪也跟着复用
        for i, v in enumerate(vec, 1):
            data[f"vec{i}"] = str(v)
        data["emo_control_method"] = "2"
    # 上传音频通常是 wav；保留原后缀让服务端的魔数嗅探/日志更准确
    suf = Path(filename or "ref.wav").suffix.lower() or ".wav"
    files: dict[str, Any] = {"prompt_audio": (f"ref{suf}", audio, "audio/wav")}
    if emo_audio:
        # 情感参考音频：另给一段"带目标情绪"的干声，预设复用时会一起复现该情绪
        esuf = Path(emo_audio_name or "emo.wav").suffix.lower() or ".wav"
        files["emo_audio"] = (f"emo{esuf}", emo_audio, "audio/wav")
    try:
        async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
            r = await c.post(f"{base}/api/v1/presets", data=data, files=files)
            if r.status_code == 409:
                raise TTSError(f"预设「{data['name']}」已存在")
            if r.status_code >= 400:
                body = r.text[:300]
                raise TTSError(f"保存预设失败 HTTP {r.status_code}：{body}")
            return r.json()
    except TTSError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TTSError(f"保存预设失败：{type(e).__name__}: {e}")


async def preset_audio(preset: str, base_url: str = "") -> bytes:
    """取某个预设的参考音频字节（合成时当 spk_audio 用）。"""
    base = _base(base_url)
    if not preset:
        raise TTSError("没有可用的音色：请选一个预设，或上传参考音频")
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
            r = await c.get(f"{base}/api/v1/presets/{preset}/audio/prompt")
            if r.status_code == 404:
                raise TTSError(f"音色预设「{preset}」不存在")
            if r.status_code != 200:
                raise TTSError(f"取参考音频失败 HTTP {r.status_code}")
            return r.content
    except TTSError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TTSError(f"取参考音频失败：{type(e).__name__}: {e}")


@media_queue.serialized("voice")
@gpu_arbiter.gpu_voice
async def synthesize(
    text: str,
    *,
    reference: bytes | None = None,
    preset: str = "",
    base_url: str = "",
    emo_mode: str | None = None,
    emo_text: str | None = None,
    emo_weight: float | None = None,
    emo_vector: Any = None,
    emo_audio: bytes | None = None,
    emo_auto: bool = False,
    speed: float | None = None,          # 语速 0.5~2.0（>1 慢 <1 快；IndexTTS-2.5 生效）
    lang: str | None = None,             # 语言 auto/zh/en/ja/es/ar（2.5 生效；auto 由服务端判定）
    timeout_s: int | None = None,
    on_done: "Callable[[dict[str, Any]], None] | None" = None,
) -> tuple[bytes, dict[str, Any]]:
    """克隆合成一段语音，返回 (wav 字节, 服务端 done 信息)。

    reference 优先；没有就给 preset（走预设的参考音频）。
    on_done：收到服务端 done 事件（成品已落盘）时立刻回调，供上层在随后
    下载/读流中断时把成品捡回来——「生成完了但没取到」的兜底。

    情绪三选一（emo_mode）：
      emo_audio  → 1 情感参考音频（必须是 bytes，会在内存里转成上传文件）
      emo_vector → 2 八维情感权重（list 或 "0.9,0,..." 字符串）
      emo_text   → 3 文本描述情感（走服务端 Qwen 情感分析）
    只选一种时其余参数会被忽略；都没给就沿用 .env 默认（默认 0=平）。

    emo_auto=True（读长文/对话朗读用）：没显式给情绪素材时，用「待念文本自身」
    作为 emo_text 走 mode=3，让服务端 Qwen 按这段话的语气自动配情绪——这不引入
    任何新依赖，也不会再退回"三句一个调"。显式给了素材/Mode 时 auto 不生效。
    """
    base = _base(base_url)
    if not text.strip():
        raise TTSError("文本为空")
    ref = reference or (await preset_audio(preset, base))
    # 参数缺失时才回落到 .env 默认；显式传 ""/None 的语义交给下面按可用性判定
    explicit = emo_mode not in (None, "")
    if (emo_auto and not explicit and emo_vector in (None, "", [], ())
            and not emo_audio and not str(emo_text or "").strip()):
        # 未指定任何情绪 → 用台词本身做情感文本（服务端 Qwen 分析），避免平静腔。
        # 服务端情感文本上限 500 字，长回复截前 480 字做语气判断即可（整段仍按原文念）。
        emo_mode = "3"
        emo_text = text[:480]
        explicit = True
    mode = str(emo_mode).strip() if explicit else str(TTS_EMO_MODE or "0")
    eff_text = str(emo_text if emo_text is not None else TTS_EMO_TEXT or "").strip()
    vec = parse_emo_vector(emo_vector)
    # 调用方没显式指定情绪（mode 也没指定）时，才用 .env 的「默认向量 / 默认情感音频」兜底。
    # 显式选了 0=平 也要尊重——不然界面/档位里点「沿用参考情绪」会被 .env 悄悄改掉。
    if not explicit and emo_vector in (None, "", [], ()) and not emo_audio:
        cfg_vec = parse_emo_vector(TTS_EMO_VECTOR)
        if cfg_vec is not None:
            vec = cfg_vec
        elif TTS_EMO_AUDIO:
            p = Path(TTS_EMO_AUDIO)
            try:
                if p.is_file():
                    emo_audio = await asyncio.to_thread(p.read_bytes)
            except OSError:
                log.warning("读取 TTS_EMO_AUDIO 失败：%s", TTS_EMO_AUDIO)
    # 来源优先级与"模式"解耦：显式给了哪种情绪素材就按哪种走，避免出现
    # 「传了 emo_audio 却因为 emo_mode 还是默认 0 而被静默丢弃」这种坑。
    if emo_audio:
        mode = "1"
        vec = None
    elif vec is not None:
        mode = "2"
    elif mode == "3" and not eff_text:
        # 选了文本模式却没描述（且 .env 也没配）→ 回落 0，别让服务端按主文本乱猜
        mode = "0"
    if mode == "1" and not emo_audio:
        mode = "0"   # 选了情感音频却没给音频文件
    if mode == "2" and vec is None:
        mode = "0"
    t0 = time.monotonic()
    log.info("语音合成开始：preset=%r 参考音频=%s 字数=%d emo_mode=%s %s",
             preset, reference is not None, len(text), mode, base)
    payload = {
        "text": text,
        "emo_mode": mode,
        "emo_text": eff_text if mode == "3" else "",
        "emo_weight": str(emo_weight if emo_weight is not None else TTS_EMO_WEIGHT),
        "file_naming": "title_time",
    }
    # 语速 / 语言：只给 2.5 服务透传（2.0 会忽略未知表单字段，不必担心兼容）。
    if speed is not None:
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            speed = None
    if speed is not None:
        payload["duration_factor"] = str(max(0.5, min(2.0, speed)))
    if lang and str(lang).strip():
        payload["lang"] = str(lang).strip()
    if vec is not None and mode == "2":
        # 服务端按 vec1..vec8 逐个接（顺序：高兴/愤怒/悲伤/害怕/厌恶/忧郁/惊讶/平静）
        for i, v in enumerate(vec, 1):
            payload[f"vec{i}"] = str(v)
    files: dict[str, Any] = {"spk_audio": ("ref.wav", ref, "audio/wav")}
    if mode == "1" and emo_audio:
        files["emo_audio"] = ("emo.wav", emo_audio, "audio/wav")
    timeout = timeout_s or TTS_TIMEOUT_S
    done: dict[str, Any] | None = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0),
                                     trust_env=False) as c:
            async with c.stream("POST", f"{base}/api/v1/tts", data=payload, files=files) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", "replace")[:300]
                    raise TTSError(f"合成请求被拒 HTTP {r.status_code}：{body}")
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type")
                    if t == "error":
                        raise TTSError(ev.get("message") or "合成失败")
                    if t == "stopped":
                        raise TTSError("生成被中断")
                    if t == "done":
                        done = ev
                        break
            if done is None:
                raise TTSError("服务端没有返回 done 事件")
            # 成品已落盘：先回调，让上层即便随后下载失败也能把成品捡回来
            if on_done is not None:
                try:
                    on_done(done)
                except Exception:  # noqa: BLE001 - 回调失败不能影响合成主流程
                    log.warning("synthesize on_done 回调异常", exc_info=True)
            # done.wav 是相对路径，成品要再取一次
            wav_path = str(done.get("wav") or "")
            if not wav_path:
                raise TTSError("服务端没有返回音频路径")
            a = await c.get(f"{base}{wav_path}")
            if a.status_code != 200:
                raise TTSError(f"下载合成结果失败 HTTP {a.status_code}")
            data = a.content
    except TTSError as e:
        log.warning("语音合成失败（耗时 %.1fs）：%s", time.monotonic() - t0, e)
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise TTSError(
            f"语音服务连不上（{base}）：IndexTTS2 没启动或端口不通，先在终端把它起起来。"
            f"（{type(e).__name__}）")
    except (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.WriteError) as e:
        raise TTSError(
            f"语音服务中途断开（{base}）：常见原因是单卡 8G 显存同时驻留了生图/语音两个"
            f"服务导致 OOM，或模型加载中/服务刚重启。若服务端其实已生成完成，成品会留在"
            f"服务端 outputs 目录，可重试或直接取用。（{type(e).__name__}）")
    except Exception as e:  # noqa: BLE001
        raise TTSError(f"合成失败：{type(e).__name__}: {e}")

    done["wall_ms"] = int((time.monotonic() - t0) * 1000)
    done["preset"] = preset
    done["emo_mode"] = mode      # 回传实际生效的情绪模式，界面/工具能如实显示
    log.info("语音合成完成：耗时 %.1fs，%d bytes，emo_mode=%s",
             done["wall_ms"] / 1000, len(data), mode)
    return data, done


# ---------------- 音色设计（文字描述造音色） ----------------

@media_queue.serialized("voice")
@gpu_arbiter.gpu_voice
async def design_voice(desc: str, say: str, n: int = 1, base_url: str = "",
                       timeout_s: int = 1800) -> dict[str, Any]:
    """用文字描述造 1~5 条候选参考音频（VoxCPM2 Voice Design）。

    注意：服务端会先卸载 IndexTTS2 腾显存，所以这个调用之后第一次合成会重新加载，
    属于正常现象。返回 {"files": [{name, url, seconds, elapsed}], "elapsed": ...}。
    """
    base = _base(base_url)
    if not desc.strip():
        raise TTSError("音色描述不能为空")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0),
                                     trust_env=False) as c:
            r = await c.post(f"{base}/api/v1/voice-design",
                             data={"desc": desc, "say": say or "你好，我是这个声音。",
                                   "n": str(max(1, min(5, int(n))))})
            if r.status_code >= 400:
                raise TTSError(f"音色设计失败 HTTP {r.status_code}：{r.text[:300]}")
            j = r.json()
            if not j.get("ok"):
                raise TTSError(f"音色设计失败：{j}")
            return j
    except TTSError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TTSError(f"音色设计失败：{type(e).__name__}: {e}")


@media_queue.serialized("voice")
@gpu_arbiter.gpu_voice
async def design_audio(name: str, base_url: str = "") -> bytes:
    """取某条音色设计候选的音频字节。"""
    base = _base(base_url)
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
            r = await c.get(f"{base}/api/v1/voice-design/audio/{name}")
            if r.status_code != 200:
                raise TTSError(f"取候选音频失败 HTTP {r.status_code}")
            return r.content
    except TTSError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TTSError(f"取候选音频失败：{type(e).__name__}: {e}")


async def design_save_preset(name: str, file: str, base_url: str = "",
                             overwrite: bool = False) -> dict[str, Any]:
    """把某条候选存成 IndexTTS2 预设（之后就能按名字复用）。"""
    base = _base(base_url)
    if not name.strip():
        raise TTSError("预设名不能为空")
    try:
        async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
            data = {"name": name, "file": file}
            if overwrite:
                data["overwrite"] = "true"
            r = await c.post(f"{base}/api/v1/voice-design/presets", data=data)
            if r.status_code == 409:
                raise TTSError(f"预设「{name}」已存在")
            if r.status_code >= 400:
                raise TTSError(f"保存预设失败 HTTP {r.status_code}：{r.text[:200]}")
            return r.json()
    except TTSError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TTSError(f"保存预设失败：{type(e).__name__}: {e}")
