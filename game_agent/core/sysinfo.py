"""本机 CPU / 内存 / GPU 的瞬时状态：给会话顶栏那条「机器」读数用。

取舍：
  * 不做后台常驻采样——没人盯着界面的时候不该每几秒 spawn 一次 nvidia-smi。
    取一次约 80ms（几乎全在 nvidia-smi 的进程启动上），所以按需取 + 短缓存；
  * 单飞：并发请求里只有一个去刷新，其余直接拿上一次的值，
    避免前端几个组件同时轮询时把 nvidia-smi 排队串起来（那才会真的卡请求）。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

from game_agent.logging_setup import get_logger

log = get_logger("sysinfo")

TTL_S = 4.0                 # 缓存有效期：比前端 5s 轮询略短，每次轮询基本都拿到新数
GPU_TIMEOUT_S = 5.0          # 超时即判定「这台机器没有可用的 nvidia-smi」
GPU_RETRY_S = 300.0          # 探不到 GPU 时，隔这么久再试一次（非 N 卡机器别刷屏）

_snapshot: dict = {}
_taken_at = 0.0
_gpu_failed_at = 0.0
_lock = asyncio.Lock()
_refreshing = False


def _cpu_mem() -> dict:
    try:
        import psutil
    except ImportError:
        return {}
    vm = psutil.virtual_memory()
    me = psutil.Process()
    # 交换分区（虚拟内存）在部分 Windows 上是「性能计数器被禁用」，psutil.swap_memory()
    # 会直接抛 RuntimeError（PdhAddEnglishCounterW failed）。以前整个 _cpu_mem 会被这个
    # 异常带崩、snapshot() 的 except 又把整份快照吞成空 → 顶栏 CPU/内存/GPU 全都不显示。
    # 这里单独兜底：拿不到交换就按 0 处理，不影响 CPU / 内存 / GPU 读数。
    try:
        sm = psutil.swap_memory()
        swap_total, swap_used, swap_pct = sm.total, sm.used, sm.percent
    except Exception:  # noqa: BLE001
        swap_total = swap_used = 0
        swap_pct = 0.0
    return {
        # interval=None：取「距上次调用之间」的平均占用。第一次调用会回 0.0，
        # 前端 5s 一轮，第二轮起就是真实值。
        "cpu_pct": round(psutil.cpu_percent(interval=None), 1),
        "cores": psutil.cpu_count() or 0,
        "mem_total_mb": round(vm.total / 2 ** 20),
        "mem_used_mb": round(vm.used / 2 ** 20),
        "mem_pct": round(vm.percent, 1),
        "swap_total_mb": round(swap_total / 2 ** 20),
        "swap_used_mb": round(swap_used / 2 ** 20),
        "swap_pct": round(swap_pct, 1),
        # 本服务自己占多少：跑批/压图时排查是谁吃的光
        "proc_mem_mb": round(me.memory_info().rss / 2 ** 20),
        "proc_cpu_pct": round(me.cpu_percent(interval=None), 1),
    }


def _nvidia_smi() -> str:
    """nvidia-smi 可执行文件路径。

    服务进程从 PowerShell / 计划任务起来时 PATH 可能不含 System32，
    只靠裸名字会一直 FileNotFoundError（顶栏就永远显「没读到 nvidia-smi」），
    所以按 PATH → %SystemRoot%\\System32 两级兜底。
    """
    found = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    if found:
        return found
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    p = Path(root) / "System32" / "nvidia-smi.exe"
    return str(p) if p.is_file() else "nvidia-smi"


async def _gpus() -> list[dict]:
    """nvidia-smi 逐卡读数；没装/超时/非 N 卡都回空列表（界面只显 CPU 与内存）。"""
    global _gpu_failed_at
    if _gpu_failed_at and time.time() - _gpu_failed_at < GPU_RETRY_S:
        return _snapshot.get("gpus") or []
    args = ("--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits")
    exe = _nvidia_smi()
    def run() -> bytes:
        # 必须走线程：main.py 在 Windows 上强制了 WindowsSelectorEventLoopPolicy，
        # 而 Selector 循环在 Windows 上不支持 asyncio.create_subprocess_exec
        # （直接 NotImplementedError，GPU 读数永远是空）。
        return subprocess.run(
            [exe, *args], capture_output=True, timeout=GPU_TIMEOUT_S,
            # Windows：不建控制台窗口，否则每 5s 闪一下黑框
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        ).stdout
    try:
        out = await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001 - FileNotFoundError / 超时 / 驱动忙，一律当「没有 GPU」
        # 静默失败过一次：顶栏只显示「没读到」，看不出是 PATH、超时还是驱动问题
        log.warning("nvidia-smi 读取失败（%s，%.0fs 后重试）：%s: %s",
                    exe, GPU_RETRY_S, type(e).__name__, e)
        _gpu_failed_at = time.time()
        return []
    gpus: list[dict] = []
    for line in (out or b"").decode(errors="replace").splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 5:
            continue
        def num(i: int) -> int:
            try:
                return int(float(f[i]))
            except ValueError:
                return 0
        gpus.append({"name": f[0], "util_pct": num(1), "mem_used_mb": num(2),
                     "mem_total_mb": num(3), "temp_c": num(4)})
    return gpus


async def snapshot() -> dict:
    """最近一次机器状态（带 TTL 缓存）。任何异常都不抛：拿不到就是空 dict。"""
    global _snapshot, _taken_at, _refreshing
    now = time.time()
    if _snapshot and now - _taken_at < TTL_S:
        return _snapshot
    if _refreshing:
        return _snapshot  # 已有人在取数，这次先用旧值
    async with _lock:
        _refreshing = True
        try:
            data = _cpu_mem()
            data["gpus"] = await _gpus()
            data["sampled_at"] = int(now)
            _snapshot = data
            _taken_at = time.time()
        except Exception:  # noqa: BLE001 - 读数失败不该把接口打挂
            log.debug("sysinfo snapshot failed", exc_info=True)
            return _snapshot
        finally:
            _refreshing = False
    return _snapshot
