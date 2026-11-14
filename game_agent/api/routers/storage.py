"""数据存储位置：查看 / 更换数据根目录，并把已有数据迁移过去。

为什么需要：数据（SQLite 库 / 日志 / 上传 / 项目工作区）默认落在用户主目录的
`~/.trace`，时间久了会占满系统盘。用户希望能在设置里改到别的盘（如 D:），
并且「重新选一个地方后自动把数据搬过去」。

实现要点（安全第一）：
  * SQLite 正在被本进程写，**不能直接复制 .db 文件**（可能读到写了一半的页）。
    用 sqlite3 的在线 backup API 做一致性快照。
  * 迁移 = 先把整个数据目录复制到新位置，复制并校验成功后才写「指针文件」
    （`~/.trace_location`），并写一个待清理标记；旧目录**不在迁移时删**，
    等新实例用新目录正常启动、DB 打开成功后，再由启动逻辑删除旧目录。
    这样中途失败/新目录不可用，都还能退回旧目录，不会把数据弄丢。
  * 指针文件放在固定位置（用户主目录 / 打包版 exe 目录），不能放进数据目录自身——
    否则目录一搬走就记不住了。
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from game_agent.api.deps import require
from game_agent.auth.rbac import PERM_AGENT_CONFIG
from game_agent import config as _cfg
from game_agent.config import (
    DATA_DIR,
    PENDING_CLEANUP_FILENAME,
    _DEFAULT_DATA_DIR,
    data_dir_env_override,
    read_data_dir_pointer,
    write_data_dir_pointer,
)
from game_agent.core.context import RequestContext
from game_agent.logging_setup import get_logger
from game_agent.platform_ui import picker_supported

# DB 文件名（DATABASE_URL 若被 .env 显式指定，DB_PATH 可能不存在，按标准名兜底）
_DB_NAME = getattr(_cfg, "DB_PATH", None).name if getattr(_cfg, "DB_PATH", None) else "game_agent.db"

log = get_logger("storage")
router = APIRouter(prefix="/api", tags=["storage"])

# 待清理标记：迁移成功后在**新**目录里写一份「旧目录路径」，启动时据此删除旧目录
PENDING_CLEANUP_FILE = PENDING_CLEANUP_FILENAME
_SQLITE_SUFFIXES = (".db", ".db-wal", ".db-shm")

# 迁移是「整目录复制」的分钟级动作，同一时刻只允许一次：
# 连点两下会启动两轮复制，后一轮把前一轮刚建出来的目录判成「非空」直接失败，
# 更糟的是两轮都写完指针后互相覆盖，用户看到的结果取决于谁最后收尾。
_MIGRATE_LOCK = asyncio.Lock()

# 迁移可能几分钟（十几 GB 的数据目录实测 3~6 分钟），没有任何反馈时用户会以为卡死，
# 甚至直接关窗口——所以把复制进度落在一个模块级单例里，前端轮询拿去做进度条。
# 单例够用：同一时刻只允许一次迁移（_MIGRATE_LOCK），不存在多份进度。
class MigrateProgress:
    """迁移进度。写方是复制用的线程，读方是事件循环里的 GET。

    只做「几个标量的读/写」，CPython 下单个属性赋值是原子的，不需要额外加锁；
    快照一次取多个字段，最坏只差一拍，进度条上看不出来。
    """

    def __init__(self) -> None:
        self.running = False
        self.phase = "idle"  # idle|scan|copy|db|done|error
        self.total_bytes = 0
        self.done_bytes = 0
        self.files_total = 0
        self.files_done = 0
        self.current = ""
        self.error = ""
        self.started_at = 0.0
        self.finished_at = 0.0
        self.source = ""
        self.target = ""

    def begin(self, src: Path, target: Path) -> None:
        import time

        self.__init__()
        self.running = True
        self.phase = "scan"
        self.source = str(src)
        self.target = str(target)
        self.started_at = time.time()

    def finish(self, *, error: str = "") -> None:
        import time

        self.running = False
        self.phase = "error" if error else "done"
        self.error = error
        self.current = ""
        self.finished_at = time.time()
        if not error:
            # 收尾时把计数对齐：正常结束就该是 100%，别让进度条停在 99.x%
            self.done_bytes = self.total_bytes
            self.files_done = self.files_total

    def snapshot(self) -> dict:
        import time

        elapsed = 0.0
        if self.started_at:
            end = self.finished_at or time.time()
            elapsed = max(0.0, end - self.started_at)
        pct = None
        if self.phase in ("copy", "db"):
            # 扫描阶段总大小还没算完 → 不给百分比（前端显示「不确定」滚动条）
            if self.total_bytes > 0:
                pct = min(99.9, self.done_bytes * 100.0 / self.total_bytes)
        elif self.phase == "done":
            pct = 100.0
        speed = (self.done_bytes / elapsed) if elapsed > 0.5 and self.done_bytes else 0.0
        eta = None
        if speed > 0 and self.total_bytes > self.done_bytes and self.running:
            eta = int((self.total_bytes - self.done_bytes) / speed)
        return {
            "running": self.running,
            "phase": self.phase,
            "percent": pct,
            "done_bytes": self.done_bytes,
            "total_bytes": self.total_bytes,
            "done_human": _human(self.done_bytes),
            "total_human": _human(self.total_bytes),
            "files_done": self.files_done,
            "files_total": self.files_total,
            "current": self.current,
            "error": self.error,
            "elapsed_s": round(elapsed, 1),
            "speed_bps": int(speed),
            "speed_human": (_human(int(speed)) + "/s") if speed else "",
            "eta_s": eta,
            "source": self.source,
            "target": self.target,
        }


PROGRESS = MigrateProgress()


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def _is_default(d: Path) -> bool:
    return str(d).lower() == str(Path(_DEFAULT_DATA_DIR).expanduser().resolve()).lower()


@router.get("/system/storage")
async def get_storage(ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """当前数据目录、默认目录、占用大小、所在盘剩余空间、以及等待生效的新目录（若有）。"""
    cur = DATA_DIR
    used = await asyncio.to_thread(_dir_size, cur)
    try:
        du = shutil.disk_usage(str(cur))
        free = du.free
        total = du.total
    except OSError:
        free = total = 0
    # 指针文件里记的目录若和当前进程实际用的不一致，说明「改过但还没重启」
    pointer = read_data_dir_pointer()
    pending = ""
    if pointer:
        pp = Path(pointer).expanduser().resolve()
        if str(pp).lower() != str(cur).lower():
            pending = str(pp)
    return {
        "data_dir": str(cur),
        "default_dir": str(Path(_DEFAULT_DATA_DIR).expanduser()),
        "is_default": _is_default(cur),
        "used_bytes": used,
        "used_human": _human(used),
        "disk_free_bytes": free,
        "disk_total_bytes": total,
        "disk_free_human": _human(free),
        "pending_switch_to": pending,  # 非空=已改过目录，重启后生效
        # 非空=数据目录被环境变量 GA_DATA_DIR 钉死了，此时「迁移」不会生效，前端要说明白
        "env_override_dir": data_dir_env_override(),
        # 「浏览…」能不能弹出系统原生文件夹窗口；false 时前端回退内置目录浏览器
        "picker_native": picker_supported(),
        "app_running": True,
    }


@router.get("/system/storage/migrate/progress")
async def migrate_progress(ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """迁移进度（前端每 300~500ms 轮询一次）。phase: idle|scan|copy|db|done|error。"""
    return PROGRESS.snapshot()


class PickDirIn(BaseModel):
    title: str | None = None


@router.post("/system/storage/pick-dir")
async def pick_storage_dir(body: PickDirIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """在**服务所在机器**上弹出系统「选择文件夹」窗口，返回用户选的绝对路径。

    这里必须是原生窗口：用户要的就是「电脑文件管理器那种」，而且要能直接进别的盘、
    网络位置、U 盘。窗口是模态阻塞的，所以丢到线程里跑，别把事件循环一起卡住。
    """
    from game_agent.platform_ui import PickerBusy, PickerUnavailable, pick_directory

    title = (body.title or "").strip() or "选择数据存放位置"
    try:
        path = await asyncio.to_thread(pick_directory, title)
    except PickerBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    except PickerUnavailable as e:
        # 501：这台机器弹不出原生窗口（无桌面/无 GUI），前端据此回退内置目录浏览器
        raise HTTPException(status_code=501, detail=f"无法打开系统文件夹窗口：{e}")
    except Exception as e:  # noqa: BLE001
        log.exception("pick-dir failed")
        raise HTTPException(status_code=500, detail=f"打开系统文件夹窗口失败：{e}")
    return {"ok": True, "path": path, "cancelled": not path}


class MigrateIn(BaseModel):
    target_dir: str


def _db_copy(src: Path, dst: Path) -> None:
    """SQLite 一致性快照：用在线 backup API，避免直接复制正在被写的库文件。"""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _plan_copy(src: Path) -> list[tuple[Path, Path, int]]:
    """扫一遍源目录，列出要复制的 (源文件, 目标相对路径, 大小)，并累加总字节数。

    DB 文件（.db/-wal/-shm）和待清理标记不参与：DB 走 backup 快照，标记不该跟着搬。
    """
    import os

    src_res = src.resolve()
    plan: list[tuple[Path, Path, int]] = []
    for root, dirs, files in os.walk(src_res, followlinks=False):
        root_path = Path(root)
        # 符号链接目录 os.walk 默认不下钻（会漏数据），这里显式提示而不是静默丢
        for d in list(dirs):
            if (root_path / d).is_symlink():
                log.warning("storage migrate: 跳过符号链接目录 %s", root_path / d)
        for name in files:
            if name in _SQLITE_SUFFIXES or name == PENDING_CLEANUP_FILE:
                continue
            f = root_path / name
            try:
                size = f.stat().st_size
            except OSError:
                continue  # 拷不到的（被占用/权限）交给 copy 阶段报错，别在这里算坏总数
            plan.append((f, f.relative_to(src_res), size))
    return plan


def _do_migrate(src: Path, dst: Path) -> None:
    """把 src 整个目录复制到 dst（DB 走 backup，其余逐文件复制并上报进度）。

    用「先扫描后复制」两步：扫描拿到总字节数，复制阶段才能算出百分比；
    否则前端只能看到「已完成 N MB」而不知道还剩多少。
    """
    import os

    dst.mkdir(parents=True, exist_ok=True)
    src_res = src.resolve()

    PROGRESS.phase = "scan"
    plan = _plan_copy(src_res)
    db_src = src_res / _DB_NAME
    db_size = db_src.stat().st_size if db_src.is_file() else 0
    PROGRESS.total_bytes = sum(p[2] for p in plan) + db_size
    PROGRESS.files_total = len(plan) + (1 if db_size else 0)

    PROGRESS.phase = "copy"
    for f, rel, size in plan:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        PROGRESS.current = str(rel)
        try:
            shutil.copy2(f, target)
        except OSError as e:
            raise RuntimeError(f"复制 {rel} 失败：{e}") from e
        PROGRESS.done_bytes += size
        PROGRESS.files_done += 1

    # DB 快照（源文件存在才复制）
    if db_size:
        PROGRESS.phase = "db"
        PROGRESS.current = _DB_NAME
        _db_copy(db_src, dst / _DB_NAME)
        PROGRESS.done_bytes += db_size
        PROGRESS.files_done += 1
    PROGRESS.current = ""


@router.post("/system/storage/migrate")
async def migrate_storage(body: MigrateIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """把当前数据目录整体迁移到 target_dir，成功后写指针（重启生效）并标记旧目录待清理。"""
    # 被环境变量钉死时迁移注定不生效：直接说清楚，别让用户白等一次全量复制
    env_dir = data_dir_env_override()
    if env_dir:
        raise HTTPException(
            status_code=400,
            detail=(f"数据目录被环境变量 GA_DATA_DIR 固定为 {env_dir}，在设置页换位置不会生效。"
                    "请先删掉 .env（或启动脚本）里的 GA_DATA_DIR，再来迁移。"),
        )
    if _MIGRATE_LOCK.locked():
        raise HTTPException(status_code=409, detail="已有一次迁移正在进行，请等它结束再操作")
    async with _MIGRATE_LOCK:
        return await _migrate_locked((body.target_dir or "").strip())


async def _migrate_locked(raw: str) -> dict:
    """真正的迁移流程（调用方已持有 _MIGRATE_LOCK）。"""
    if not raw:
        raise HTTPException(status_code=400, detail="请选择目标目录")
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="目标目录必须是绝对路径")
    target = target.resolve()
    src = DATA_DIR.resolve()

    if str(target).lower() == str(src).lower():
        raise HTTPException(status_code=400, detail="目标目录就是当前数据目录，无需迁移")

    # 目标不能落在当前数据目录里面（否则会递归复制自己）
    try:
        target.relative_to(src)
        raise HTTPException(status_code=400, detail="目标目录不能位于当前数据目录内部")
    except ValueError:
        pass
    # 反过来：源在目标内部也不行
    try:
        src.relative_to(target)
        raise HTTPException(status_code=400, detail="当前数据目录位于目标目录内部，请换一个目录")
    except ValueError:
        pass

    # 目标必须不存在、或为空目录
    if target.exists():
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="目标路径已存在且不是文件夹")
        try:
            if any(target.iterdir()):
                raise HTTPException(status_code=400, detail="目标文件夹非空，请选择一个空文件夹")
        except PermissionError:
            raise HTTPException(status_code=400, detail="目标文件夹无访问权限")
    # 可写性探测
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".trace_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"目标目录不可写：{e}")

    # 空间检查：目标盘剩余空间要大于源目录大小的 1.1 倍
    need = await asyncio.to_thread(_dir_size, src)
    try:
        free = shutil.disk_usage(str(target)).free
        if free < need * 1.1:
            raise HTTPException(
                status_code=400,
                detail=f"目标磁盘空间不足：需要约 {_human(int(need * 1.1))}，可用 {_human(free)}",
            )
    except OSError:
        pass

    log.info("storage migrate start: %s -> %s (%s)", src, target, _human(need))
    PROGRESS.begin(src, target)
    try:
        await asyncio.to_thread(_do_migrate, src, target)
    except Exception as e:  # noqa: BLE001
        log.exception("storage migrate failed")
        PROGRESS.finish(error=str(e))
        # 清掉半成品，避免用户下次选到非空目录
        try:
            for child in target.iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=500, detail=f"迁移失败（旧数据未动）：{e}")

    # 复制成功 → 写指针（重启后使用新目录）+ 在新目录里留下「旧目录待清理」标记
    pointer_arg = "" if _is_default(target) else str(target)
    write_data_dir_pointer(pointer_arg)
    try:
        (target / PENDING_CLEANUP_FILE).write_text(str(src), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 写不了标记只是不能自动清理旧目录，不影响切换
        log.warning("write pending-cleanup marker failed")

    log.info("storage migrate done: now=%s (restart to apply)", target)
    PROGRESS.finish()
    return {
        "ok": True,
        "target_dir": str(target),
        "is_default": _is_default(target),
        "need_restart": True,
        "message": "数据已复制到新目录，重启后生效；重启时会自动清理旧目录。",
    }
