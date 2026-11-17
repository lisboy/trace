"""数据目录级「单实例」互斥锁（谁在用这套数据目录，只有一个进程说了算）。

为什么要这个：lite 模式是「单进程 + 一套数据目录（DB / 日志 / 项目工作区）」。
只要同时起第二个实例（开发时起个 :9998 联调、或用户双击了两次 start.cmd），第二个
进程启动时会跑「僵尸 run 清理」——它看到库里所有 queued/claimed/running/blocked 的
run 都会当成「上个进程留下的尸体」，全部标成 ``failed`` = 「服务重启，本次执行被中断」，
并在每个会话里补一条"请重新发起"。于是**第一个实例里正在跑的活活被第二个实例判死**，
用户看到的就是"服务老是重启 / 我的活被中断了"。

实测（2026-11-17）：app.log 里 10:11:42 / 10:21:52 / 10:25:27 各打了一条
``cleaned N zombie run(s) from previous process``，同一时刻主实例（PID 11188，:18888）
一直在正常服务——那三次都是「另起一个没带 GA_DEBUG_INSTANCE=1 的实例」干的好事。
（旧的 pid 文件兜不住：它写在数据目录里，后起的实例会直接覆盖成自己的 PID。）

解决方式：数据目录里放一把**随进程存活的操作系统文件锁**（Windows ``msvcrt.locking``
/ POSIX ``flock``）。非调试实例启动时先抢这把锁：

* 抢到 → 本进程是这套数据目录唯一的写入者，照旧清僵尸、删旧数据目录、跑 worker 池；
* 抢不到 → 说明已有实例在跑，**立刻打印人话并退出，一个字节都不写库**（绝不清别人的 run）；
* ``GA_DEBUG_INSTANCE=1``（联调实例）不参与抢锁——它本来就不该清库、不跑 worker；
* ``GA_ALLOW_MULTI_INSTANCE=1`` 可以强制放行，但放行的实例**同样不清僵尸**（见 main）。

锁自带的生命周期保证：进程崩溃/被 taskkill 时操作系统自动释放，不会留「死锁文件」
把人永久挡在外面，所以不需要「超时判死」这类逻辑。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

# 锁文件（内容只是「谁占着」的提示，权威判据是操作系统锁本身）
LOCK_FILENAME = "game_agent.instance.lock"
# 占用者信息（端口/URL/启动时间），只为把报错写得让人能直接照做
INFO_FILENAME = "game_agent.instance.json"
# 显式允许「明知有实例还再起一个」的逃生开关
ALLOW_MULTI_ENV = "GA_ALLOW_MULTI_INSTANCE"


def lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / LOCK_FILENAME


def info_path(data_dir: Path) -> Path:
    return Path(data_dir) / INFO_FILENAME


def _lock_byte(fh: IO[bytes]) -> None:
    """非阻塞地锁住文件第 0 个字节；抢不到抛 OSError。"""
    fh.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - POSIX 分支
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_byte(fh: IO[bytes]) -> None:
    fh.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - POSIX 分支
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class InstanceLock:
    """持有数据目录的独占锁。``acquire()`` 非阻塞，抢不到返回 False。"""

    def __init__(self, data_dir: Path) -> None:
        self.path = lock_path(data_dir)
        self._fh: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        if self._fh is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+b")  # noqa: SIM115 - 句柄要活到进程结束
        except OSError:
            # 锁文件建不出来（目录只读等）：宁可放行也不要拦死启动，调用方会降级处理
            return False
        try:
            _lock_byte(fh)
        except OSError:
            try:
                fh.close()
            except OSError:
                pass
            return False
        self._fh = fh
        try:  # 仅作提示：写自己的 PID 进去，方便人肉看「谁占着」
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode("ascii"))
            fh.flush()
        except OSError:
            pass
        # 同时写一份「占用者信息」给后来者读：锁文件里那一个字节被操作系统锁住了，
        # 别的进程**读不了**（Windows msvcrt 锁的是字节区间），所以报错文案只能从这里取。
        # main 拿到实际监听端口后会用 write_instance_info 再覆盖一次，补上地址。
        write_instance_info(self.path.parent, pid=os.getpid())
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock_byte(fh)
        except OSError:
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass


# ---------------- 占用者信息（只为把报错写清楚） ----------------

def write_instance_info(data_dir: Path, *, pid: int, host: str = "", port: int = 0) -> Path:
    """写下「谁占着这套数据目录」（端口未知时先写 PID，绑到端口后再补一次）。"""
    p = info_path(data_dir)
    data = {
        "pid": int(pid),
        "host": host,
        "port": int(port),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if port:
        data["url"] = f"http://{'127.0.0.1' if host in ('0.0.0.0', '', '::') else host}:{int(port)}/"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return p


def read_instance_info(data_dir: Path) -> dict[str, Any] | None:
    p = info_path(data_dir)
    try:
        if not p.is_file():
            return None
        raw = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    try:
        raw["pid"] = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        raw["pid"] = None
    return raw


def clear_instance_info(data_dir: Path) -> None:
    try:
        info_path(data_dir).unlink(missing_ok=True)
    except OSError:
        pass


def pid_alive(pid: int | None) -> bool:
    """粗判 PID 是否还活着（只用于拼提示语，判错也不影响安全）。"""
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # requirements.txt 里有

        return psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001 - 没装 psutil 也能跑
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
        except Exception:  # noqa: BLE001
            return False


def _listen_addrs(proc: Any) -> str | None:
    """在单个进程上找监听端口，返回可点地址。"""
    try:
        conns = proc.net_connections(kind="inet")
    except Exception:  # noqa: BLE001 - 权限/平台不支持
        return None
    try:
        import psutil

        listen_states = (psutil.CONN_LISTEN,)
    except Exception:  # noqa: BLE001
        listen_states = ("LISTEN",)
    for c in conns:
        if c.status not in listen_states or not c.laddr:
            continue
        host = getattr(c.laddr, "ip", None)
        port = getattr(c.laddr, "port", None)
        if host is None or port is None:
            try:
                host, port = c.laddr[0], c.laddr[1]
            except Exception:  # noqa: BLE001
                continue
        if port:
            h = "127.0.0.1" if str(host) in ("0.0.0.0", "::", "", "None") else str(host)
            return f"http://{h}:{int(port)}/"
    return None


def _listening_url(pid: int) -> str | None:
    """查这个进程（及其子进程）正在监听的端口，拼成可点地址（拿不到就 None）。

    用途：占用者不是「写过 info 文件的新版本实例」时（老版本 / info 是死进程残留），
    报错里也要给出用户真正能打开的地址，而不是让他自己 netstat 找端口。
    注意要连子进程一起看：Windows 上 `python -m game_agent.main` 常是「启动器父进程 +
    真正跑服务的子进程」，端口听在子进程上（实测父 5380 / 子 11188 就是这种结构）。
    """
    try:
        import psutil

        proc = psutil.Process(int(pid))
    except Exception:  # noqa: BLE001
        return None
    url = _listen_addrs(proc)
    if url:
        return url
    try:
        for child in proc.children(recursive=True):
            url = _listen_addrs(child)
            if url:
                return url
    except Exception:  # noqa: BLE001
        pass
    return None


def describe_owner(data_dir: Path, *, prefer_pid: int | None = None) -> str:
    """把「谁在占着这个数据目录」拼成一句人话。

    ``prefer_pid``：调用方已经用「进程扫描」确认过的活 PID（比 info 文件更可信——
    info 文件可能是一个被强杀、没来得及清理的实例留下的）。
    """
    info = read_instance_info(data_dir) or {}
    info_pid = info.get("pid")
    pid = info_pid
    bits: list[str] = []
    if prefer_pid:
        bits.append(f"PID {prefer_pid}")
        if info_pid and info_pid != prefer_pid and pid_alive(info_pid):
            bits.append(f"PID {info_pid}")  # 两个活着的都报出来，避免漏线索
    elif pid:
        # 写 info 的进程已经没了 = 上次被强杀留下的残档，别拿它当「占用者」误导用户
        if pid_alive(pid):
            bits.append(f"PID {pid}")
    # 地址/启动时间只在「info 说的那个进程就是占用者本人且还活着」时才可信；
    # 否则（老版本实例 / info 是上次被强杀留下的残档）贴上去会误导用户去访问死端口。
    info_trusted = bool(info_pid) and pid_alive(info_pid) and (
        prefer_pid is None or int(info_pid) == int(prefer_pid)
    )
    if info_trusted:
        if info.get("url"):
            bits.append(str(info["url"]))
        if info.get("started_at"):
            bits.append(f"启动于 {info['started_at']}")
    elif prefer_pid:  # 老版本/残留 info：现场查一下它监听的端口，尽量给可点地址
        url = _listening_url(prefer_pid)
        if url:
            bits.append(url)
    if not bits:  # 老版本实例没写过 info 文件，退回去读锁文件里的 PID
        try:
            raw = lock_path(data_dir).read_text(encoding="ascii", errors="ignore").strip()
        except OSError:
            # 锁文件那个字节正被操作系统锁着，Windows 下别的进程读不了——不是错误，
            # 只说明「确实有人占着，但没留 PID」。
            raw = ""
        if raw.isdigit():
            bits.append(f"PID {raw}")
    return " · ".join(bits) if bits else "（占用者未知）"


# ---------------- 老实例识别（过渡期兜底） ----------------
#
# 锁只对「加锁之后的版本」有效。用户机器上往往已经有一个**改动落地前就在跑**的实例：
# 它不持有锁，但第二个实例照样会去清它正在跑的 run。所以要再找一遍「同一条数据目录上
# 还有没有别的 game_agent.main 在跑」，找到就照样拦。
#
# 判据（按可靠性排序）：
#   ① 数据目录里 pid 文件记的进程还活着、命令行确实是本服务；
#   ② 全机扫进程：命令行是本服务、且不是我自己；环境变量 GA_DATA_DIR 有值就必须与本进程
#      相同，没值则视为相同（数据目录指针文件在各进程间共享，同一用户必然是同一个目录）。
PIDFILE_NAME = "game_agent.pid"


def _same_dir(a: str, b: Path) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except Exception:  # noqa: BLE001 - 路径解析失败就按不同处理，宁可漏拦也不误拦
        return False


def _is_service_process(name: str | None, cmdline: list[str] | None) -> bool:
    """这个进程本身是不是「跑本服务的那个进程」（而不是包着它的 cmd.exe / bash）。

    判据：进程名是 python/pyw/pythonw 或本项目的打包可执行（Trace.exe / trace），
    并且命令行里确实有 `-m game_agent.main` / `game_agent.main` / `--reset-password` 之类。
    只看命令行是不行的：`cmd /c "set GA_PORT=... && python -m game_agent.main"` 这个 cmd.exe
    的命令行里也含 `game_agent.main`，会被误判成另一个实例（实测：服务直接被自己拦住起不来）。
    """
    n = (name or "").strip().lower()
    # 打包版：可执行名本身就是本服务（Trace.exe），它的命令行里没有 game_agent.main
    if n in ("trace.exe", "trace") or (n.startswith("trace") and n.endswith(".exe")):
        return True
    py_ok = n.startswith("python") or n in ("py", "pyw", "python3")
    if not py_ok:
        return False
    joined = " ".join(cmdline or []).lower()
    return "game_agent.main" in joined or "game_agent.main" in joined.replace("\\", ".")


def _pidfile_instance_pid(data_dir: Path) -> int | None:
    """pid 文件里记着一个「活着的、确实是本服务的、且不是我自己」的进程 → 返回它的 PID。"""
    try:
        raw = (Path(data_dir) / PIDFILE_NAME).read_text(encoding="ascii", errors="ignore").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    pid = int(raw)
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return None
        cmd = " ".join(psutil.Process(pid).cmdline()).lower()
    except Exception:  # noqa: BLE001 - 没装 psutil / 权限不足：当作不可判，不去拦
        return None
    return pid if "game_agent" in cmd else None


def _implicit_data_dir() -> Path:
    """没设 GA_DATA_DIR 的实例实际会用哪条数据目录（指针文件 > 默认 ~/.trace）。

    指针文件与数据目录都是「同一个用户共用」的，所以别的进程的隐含目录和我算出来的
    一定是同一个——不用去读它的环境变量也能比对。
    """
    try:
        from game_agent.config import read_data_dir_pointer

        raw = (read_data_dir_pointer() or "").strip()
    except Exception:  # noqa: BLE001
        raw = ""
    return Path(raw).expanduser() if raw else Path.home() / ".trace"


def _parent_launcher_pid() -> int | None:
    """我自己的「venv 启动器父进程」PID（要跳过）。

    为什么必须跳过：Windows 上 `python -m game_agent.main` 常是两个进程——venv 的
    python.exe 启动器（父，命令行同样是 `-m game_agent.main`）和真正执行模块的子进程。
    父进程看起来完全像「另一个实例」，不排除的话刚起的服务会把自己判成重复实例然后自杀。

    只排除**直接父进程**且它确实是本服务的命令行：不能一路上溯把祖先全排掉——服务自己
    拉起的工具进程（`run_command` 起的 python）的祖先里就有那个正在跑的服务，
    全都排除掉就等于漏拦（实测过：12 个祖先后代全被排除，扫描结果变成 None）。
    """
    try:
        import psutil

        parent = psutil.Process().parent()
        if parent is None:
            return None
        joined = " ".join(parent.cmdline()).lower()
        if "game_agent.main" in joined or "trace.exe" in joined:
            return parent.pid
    except Exception:  # noqa: BLE001
        pass
    return None


def _scan_other_instance(data_dir: Path) -> int | None:
    """全机扫一遍：还有没有别的 game_agent.main 挂着同一条数据目录。"""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return None
    me = os.getpid()
    launcher = _parent_launcher_pid()
    implicit = _implicit_data_dir()
    try:
        procs = list(psutil.process_iter(["pid", "name", "cmdline"]))
    except Exception:  # noqa: BLE001
        return None
    for p in procs:
        try:
            pid = int(p.info.get("pid") or 0)
            if pid <= 0 or pid == me or pid == launcher:
                continue
            # 必须是「解释器/打包出来的可执行」本身，光看命令行不够：
            # `cmd /c "set GA_PORT=... && python -m game_agent.main"` 这种外壳进程的
            # 命令行里同样含 game_agent.main，但它只是个 cmd.exe，不是另一个实例——
            # 只看命令行会把它当成占用者，结果服务起不来（实测踩过）。
            if not _is_service_process(p.info.get("name"), p.info.get("cmdline")):
                continue
            try:
                env = p.environ()
            except Exception:  # noqa: BLE001 - 读不到环境（别的用户/权限）：按隐含目录算
                env = {}
            # 对方是「联调实例 / 被放行实例」时不拦：它们本来就不清僵尸、不抢队列，
            # 而且它们的 UI 正是用来跟主实例并存的——此时再起主实例必须能起来。
            if str(env.get("GA_DEBUG_INSTANCE") or "").strip().lower() in ("1", "true", "yes", "on"):
                continue
            if str(env.get("GA_ALLOW_MULTI_INSTANCE") or "").strip().lower() in ("1", "true", "yes", "on"):
                continue
            gd = (env.get("GA_DATA_DIR") or "").strip()
            other_dir = Path(gd).expanduser() if gd else implicit
            if not _same_dir(str(other_dir), data_dir):
                continue  # 明确是另一条数据目录上的实例，不关我事
            return pid
        except Exception:  # noqa: BLE001 - 单个进程读不到就跳过
            continue
    return None


def find_other_instance(data_dir: Path) -> int | None:
    """同一条数据目录上还活着的另一个本服务进程（没有则 None）。"""
    pid = _pidfile_instance_pid(data_dir)
    if pid is not None:
        return pid
    return _scan_other_instance(data_dir)


# 兼容旧名字（测试/调用方用原来那个）
def _live_other_instance(data_dir: Path) -> int | None:
    return find_other_instance(data_dir)


@dataclass
class GuardResult:
    """启动守卫结论。``reason`` 取值决定了本进程要不要写库（清僵尸 / 删旧数据目录）。

    * ``acquired``：抢到锁，是这套数据目录唯一的写入者 → 可以清僵尸、删旧目录；
    * ``debug_instance``：GA_DEBUG_INSTANCE=1 的联调实例 → 不清任何东西；
    * ``multi_instance_allowed``：GA_ALLOW_MULTI_INSTANCE=1 强制放行 → **同样不清**；
    * ``already_running``：已有实例在跑 → 调用方必须立刻退出；
    * ``lock_unavailable``：锁文件建不出来（目录只读等）→ 放行但不清库。
    """

    ok: bool
    reason: str
    message: str = ""
    lock: InstanceLock | None = None

    @property
    def owns_data_dir(self) -> bool:
        """是不是这套数据目录的唯一写入者（== 可以动别人的 run / 删旧目录）。"""
        return self.reason == "acquired"


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def guard_message(data_dir: Path, *, extra_pid: int | None = None) -> str:
    owner = describe_owner(data_dir, prefer_pid=extra_pid)
    if extra_pid is not None and f"PID {extra_pid}" not in owner:
        # 兜底：老版本实例不写 info 文件，只留了 pid 文件，别让用户看到「占用者未知」
        owner = f"PID {extra_pid} · {owner}" if owner else f"PID {extra_pid}"
    return (
        "[trace] 这套数据目录已经在被另一个 Trace 实例使用，本次启动已取消（没有写库、没有动任何 run）。\n"
        f"        数据目录：{data_dir}\n"
        f"        占用者　：{owner}\n"
        "        为什么必须拦：第二个实例启动时会跑「僵尸 run 清理」，会把第一个实例里正在跑的\n"
        "        任务全标成「服务重启，本次执行被中断」——那就是「老是重启、我的活被中断」的根因。\n"
        "        怎么办：\n"
        "          ① 只想打开界面 → 直接访问上面的地址；\n"
        "          ② 想重启服务     → 先在 start.cmd 菜单选 [2] / 双击 stop.cmd 停掉它再起；\n"
        "          ③ 只想另起一个联调实例（不清僵尸、不抢队列）→ 加 GA_DEBUG_INSTANCE=1 再起；\n"
        f"          ④ 确实要两个都跑 → 加 {ALLOW_MULTI_ENV}=1（放行的实例同样不会清僵尸）。\n"
    )


class ProcessGuard:
    """进程级守卫：启动时 ``ensure()`` 一把，退出时 ``release()``。

    main 只跟这个对象打交道，避免「锁句柄被 GC 掉 → 锁提前释放」这类坑
    （句柄挂在对象上，对象由模块级单例持有，活到进程结束）。
    """

    def __init__(self) -> None:
        self.result: GuardResult | None = None

    def ensure(self, data_dir: Path, *, log: Any = None) -> GuardResult:
        res = ensure_single_instance(data_dir)
        self.result = res
        if not res.ok:
            msg = res.message or guard_message(data_dir)
            # 同时写到 stderr 和日志：用户是双击 start.cmd 起的，窗口里要能直接看见；
            # 打包版/后台起的时候日志里也要留痕。
            try:
                print(msg, file=sys.stderr, flush=True)
            except Exception:  # noqa: BLE001
                pass
            if log is not None:
                try:
                    log.error("single-instance guard refused startup: %s", res.reason)
                except Exception:  # noqa: BLE001
                    pass
        elif log is not None:
            try:
                log.info("single-instance guard: %s", res.reason)
            except Exception:  # noqa: BLE001
                pass
        return res

    @property
    def owns_data_dir(self) -> bool:
        return bool(self.result and self.result.owns_data_dir)

    def release(self) -> None:
        if self.result and self.result.lock is not None:
            self.result.lock.release()


# main 用的模块级单例（锁句柄靠它活到进程退出）
GUARD = ProcessGuard()


def _allow_multi_from_config() -> bool:
    """config 里的 GA_ALLOW_MULTI_INSTANCE 口径（与直接读环境变量并行，两边任一生效）。"""
    try:
        from game_agent.config import ALLOW_MULTI_INSTANCE

        return bool(ALLOW_MULTI_INSTANCE)
    except Exception:  # noqa: BLE001 - 配置模块极端情况下读不到，就只看环境变量
        return False


def ensure_single_instance(
    data_dir: Path,
    *,
    debug: bool | None = None,
    allow_multi: bool | None = None,
) -> GuardResult:
    """启动前的单实例守卫。纯函数式返回结论，不打印、不退出，便于单测覆盖。"""
    if debug is None:
        from game_agent.config import DEBUG_INSTANCE

        debug = DEBUG_INSTANCE
    if allow_multi is None:
        allow_multi = _env_flag(ALLOW_MULTI_ENV) or _allow_multi_from_config()

    if debug:
        return GuardResult(True, "debug_instance")
    if allow_multi:
        return GuardResult(True, "multi_instance_allowed")

    # ① 过渡期兜底：正在跑的那个实例可能是「加锁之前的老版本」，它不持有锁，
    #    但它的 pid 文件/进程列表里还在。此时再起一个同样会去清它正在跑的 run，必须拦。
    other = find_other_instance(data_dir)
    if other is not None:
        return GuardResult(False, "already_running", guard_message(data_dir, extra_pid=other))

    # ② 正常判据：抢数据目录独占锁。
    lock = InstanceLock(data_dir)
    if lock.acquire():
        return GuardResult(True, "acquired", lock=lock)
    # 抢不到：要么已有实例，要么锁文件根本没建出来。区分一下：文件存在=有人占着。
    try:
        exists = lock_path(data_dir).exists()
    except OSError:
        exists = False
    if not exists:
        return GuardResult(True, "lock_unavailable")
    return GuardResult(False, "already_running", guard_message(data_dir))
