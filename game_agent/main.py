"""循迹（Trace）主入口（设计 §15）。lite / server 双模式启动。

lite：单进程（API + 执行），SQLite + 进程内 5 接口。
server：同套代码，仅替换 5 接口实现 + 用容器编排（此处装配 worker）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from game_agent.config import HOST, LOG_DIR, PORT, MODE, DEBUG_INSTANCE
from game_agent.api.state import state
from game_agent.logging_setup import configure_logging, get_logger

if sys.platform == "win32":
    # Windows 上统一用 Selector 事件循环（默认是 Proactor）。
    # 历史上这是因为 aiomysql 在 Proactor 下不兼容；现在只跑 SQLite，但整套工具链
    # （native.py 的 Popen 子进程读线程、sysinfo 的 to_thread 调用）都是按 Selector
    # 环境调好的，保持不动可避免无谓的运行时行为漂移。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def main() -> None:
    configure_logging()
    log = get_logger("main")
    from game_agent.config import ensure_dirs
    ensure_dirs()

    from game_agent.core.skills import seed_builtin_skills
    try:
        created = seed_builtin_skills()
        if created:
            log.info("seeded builtin skills: %s", ", ".join(created))
    except Exception:  # noqa: BLE001 - 播种失败不挡启动
        log.exception("seed builtin skills failed")

    from game_agent.db.migrate import init_db
    from game_agent.db.session import SessionLocal, engine
    from game_agent.core.lite.container import build_lite_services
    from game_agent.orchestration.orchestrator import Orchestrator
    from game_agent.bootstrap import bootstrap

    if not DEBUG_INSTANCE:
        _write_pidfile()
    try:
        asyncio.run(_start(engine, SessionLocal, build_lite_services, Orchestrator, bootstrap, log))
    finally:
        if not DEBUG_INSTANCE:
            _remove_pidfile()


def _pidfile_path():
    from game_agent.config import DATA_DIR
    return DATA_DIR / "game_agent.pid"


def _write_pidfile() -> None:
    """把当前进程 PID 写到 <DATA_DIR>/game_agent.pid。

    纯供 Windows 的 stop.cmd 使用：没有它就只能靠 PowerShell 去查 Win32_Process
    的命令行来匹配进程（目标机不一定有 PowerShell，或被执行策略挡住）。有了 pid 文件，
    stop.cmd 用纯批处理 + taskkill /PID 就够，干净且不会误杀别的程序。
    lite 模式单进程单实例，一个 pid 文件即可。
    """
    try:
        from game_agent.config import DATA_DIR
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _pidfile_path().write_text(str(os.getpid()), encoding="ascii")
    except Exception:  # noqa: BLE001 - 写不了 pid 不影响启动
        pass


def _remove_pidfile() -> None:
    try:
        p = _pidfile_path()
        if p.exists():
            p.unlink()
    except Exception:  # noqa: BLE001
        pass


def _cleanup_old_data_dir(log) -> None:  # type: ignore[no-untyped-def]
    """数据目录迁移后，删除旧目录。

    只在新实例用新目录成功打开 DB 之后调用。迁移流程会在新目录里留一个
    `.trace_pending_cleanup` 文件，内容是「旧目录的绝对路径」；这里读它、确认它
    不是当前目录（防止误删正在用的目录），再删掉整个旧目录和这个标记文件。
    尽力而为：删不掉只记 warning，不影响服务运行（旧目录顶多一些冗余文件）。

    注意 ``Path`` 必须在本函数里导入：main.py 顶层没有 ``from pathlib import Path``，
    以前这里直接写 ``Path(...)`` → NameError 被下面的 except 吞掉 → 当成「没有旧目录」
    静默返回，于是「迁移完旧目录自动清理」从来没生效过，C 盘那 10 GB 一直躺着。
    """
    import shutil as _shutil
    from pathlib import Path

    from game_agent.config import DATA_DIR, PENDING_CLEANUP_FILENAME

    marker = DATA_DIR / PENDING_CLEANUP_FILENAME
    if not marker.is_file():
        return
    try:
        old = Path(marker.read_text(encoding="utf-8").strip()).resolve()
    except Exception:  # noqa: BLE001
        old = None
    if old is None or not str(old):
        marker.unlink(missing_ok=True)
        return
    # 安全护栏：绝不删当前目录 / 主目录 / 盘符根
    cur = DATA_DIR.resolve()
    home = Path.home().resolve()
    if old == cur or old == home or len(str(old)) <= 3:
        log.warning("skip cleaning suspicious old data dir: %s", old)
        marker.unlink(missing_ok=True)
        return
    try:
        if old.is_dir():
            _shutil.rmtree(old)
            log.info("cleaned old data dir after migration: %s", old)
        marker.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        log.warning("cleanup old data dir failed (ignored): %s", old)


def _want_open_browser() -> bool:
    """启动完成后要不要自动打开浏览器。

    打包成 exe（PyInstaller 冻结）双击启动时**默认要开**：这时它就是个单机应用，
    起来就该看见界面，不该让人去日志里翻端口再自己敲地址——用户提的就是这个。
    源码/dev 跑则默认不开（restart.cmd 会反复重启，每改一行弹一次浏览器没法工作），
    需要时用 GA_OPEN_BROWSER=1 打开。显式设 0/false 一律尊重（打包版也适用）。
    """
    v = (os.environ.get("GA_OPEN_BROWSER") or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return bool(getattr(sys, "frozen", False))


async def _start(engine, SessionLocal, build_lite_services, Orchestrator, bootstrap, log):  # type: ignore[no-untyped-def]
    import faulthandler

    from game_agent.config import DATA_DIR
    # 段错误/致命异常时把全部线程调用栈写进 stderr（Windows 访问违例会走到这里）。
    faulthandler.enable()
    # 45s 周期全线程快照默认关闭：2026-10-28 三次服务段错误（python313.dll 0xc0000005、
    # 偏移固定 0x2bdcd4）全部精确落在进程启动后 45s 的整数倍（+0~2s）——崩溃线程就是
    # dump_traceback_later 的 C 原生看门狗线程（faulthandler 转储里没有 Current thread 段，
    # 只有它这个 C 线程不可见）。排查「整体卡死」需要快照时设 GA_FH_WATCHDOG=1 再开。
    if os.environ.get("GA_FH_WATCHDOG") == "1":
        try:
            _fh = open(DATA_DIR / "fault_dump.txt", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
            faulthandler.dump_traceback_later(45, repeat=True, file=_fh)
            log.info("faulthandler watchdog on: %s", DATA_DIR / "fault_dump.txt")
        except Exception:  # noqa: BLE001
            pass

    from game_agent.db.migrate import init_db
    await init_db()

    # 数据目录迁移后的旧目录清理：只有「新实例用新目录把 DB 打开成功」才走到这里，
    # 才敢删旧目录——迁移期间旧数据一直保留，任何一步失败都能退回。
    # 调试实例与主实例共用一个库，绝不能在这里删东西，直接跳过。
    if not DEBUG_INSTANCE:
        _cleanup_old_data_dir(log)

    services = await build_lite_services(SessionLocal, worker_id="lite-1")
    orchestrator = Orchestrator(services, worker_id="lite-1", session_factory=SessionLocal)

    state.session_factory = SessionLocal
    state.services = services
    state.orchestrator = orchestrator
    state.worker_id = "lite-1"
    state.frontend_dir = _frontend_dir()

    # 本机身份（无登录）：保证库里有一个默认用户 + 一个工作区，老库复用最早的账号
    await bootstrap()

    # 启动清理：服务重启时遗留的 queued/running/claimed/blocked run 不会有人继续执行，
    # 标记为 failed，避免界面永远「运行中/排队中」的僵尸 run
    # 注意用 Python 侧 UTC（不用 func.now()：那是数据库本地时区，与 UTC 存储约定不一致）
    # 调试实例跳过：它和主实例共用数据库，主实例的 run 是活的，不能当成僵尸清掉。
    from datetime import datetime, timezone as _tz
    from sqlalchemy import func, select, update
    from game_agent.core.notices import post_session_notice
    from game_agent.db.models import Run
    dead: list[dict] = []
    if not DEBUG_INSTANCE:
        async with SessionLocal() as s:
            zombie = (await s.execute(select(Run).where(
                Run.status.in_(("queued", "running", "claimed", "blocked"))))).scalars().all()
            dead = [{"run_id": str(r.id), "session_id": r.session_id,
                     "team_id": r.team_id, "project_id": r.project_id} for r in zombie]
            if dead:
                await s.execute(update(Run).where(
                    Run.id.in_([r.id for r in zombie])).values(
                    status="failed", error="服务重启，运行中断，请重新发起",
                    finished_at=datetime.now(_tz.utc)))
                await s.commit()
                log.warning("cleaned %s zombie run(s) from previous process", len(dead))
        # 每个被中断的会话都补一条提示：否则会话里只剩用户那句话，聊天页看起来是空白的
        for d in dead:
            await post_session_notice(text="服务重启，本次执行被中断，请重新发起。", **d)

    # worker 池：多个 run 并行执行。claim 保证跨项目/多会话并行，
    # SAME_PROJECT_PARALLEL（默认开）决定同一个项目要不要串行
    from game_agent.config import MAX_CONCURRENT_RUNS

    async def _fail_crashed_run(run_id: str, exc: BaseException) -> None:
        """worker 侧崩溃兜底：把 run 落成 failed 并在会话里补一条说明。

        不做这件事的后果（实测）：run 永久停在 claimed → 界面一直「运行中」，
        用户再发消息也看不到任何回应（聊天区只剩自己那几句话）。
        """
        import uuid as _uuid

        snap = None
        try:
            async with SessionLocal() as s:
                rid = run_id if isinstance(run_id, _uuid.UUID) else _uuid.UUID(str(run_id))
                run = await s.get(Run, rid)
                if run is None:
                    return
                if run.status in ("queued", "claimed", "running", "blocked"):
                    snap = {"session_id": run.session_id, "team_id": run.team_id,
                            "project_id": run.project_id}
                    run.status = "failed"
                    run.error = f"执行中断（内部错误：{type(exc).__name__}）"
                    run.finished_at = datetime.now(_tz.utc)
                    await s.commit()
        except Exception:  # noqa: BLE001
            log.exception("mark crashed run failed: %s", run_id)
            return
        if snap:
            await post_session_notice(
                run_id=run_id,
                text="这次执行因内部错误中断，没有产出结果，请重新发起。",
                **snap)

    async def worker_loop(idx: int) -> None:
        log.info("worker started", extra={"worker": f"lite-{idx}"})
        while True:
            try:
                run_id = await services.queue.claim()
            except Exception:  # noqa: BLE001
                run_id = None
            if run_id is None:
                await asyncio.sleep(1)
                continue
            try:
                await orchestrator.execute_run(run_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("execute_run crashed")
                await _fail_crashed_run(run_id, e)
            await asyncio.sleep(0)

    async def worker_supervisor(idx: int) -> None:
        """单 worker 的看护：worker_loop 里任何非 Cancelled 异常都吞掉并重启，保证 worker 数不减。"""
        while True:
            try:
                await worker_loop(idx)
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 - worker 必须存活，否则队列无人消费
                log.exception("worker crashed, restarting", extra={"worker": f"lite-{idx}"})
                await asyncio.sleep(1)

    shutdown_event = asyncio.Event()

    # 保持强引用：asyncio task 若无引用可能被 GC 回收（表现为 worker 消失、run 永远卡 claimed）
    # supervisor 兜底：worker 意外抛出任何异常/CancelledError 都不会永久减少 worker 数量。
    # 另外挂 done-callback：supervisor 理论上不该结束，若真结束（比如被误 cancel）至少留一条
    # ERROR 日志并自动补一个，避免「悄悄少一个 worker、吞吐掉一截」这种难查的退化。
    worker_tasks: list[asyncio.Task] = []

    def _on_worker_done(t: asyncio.Task, idx: int) -> None:
        if shutdown_event.is_set():
            return  # 停机时主动 cancel 的，正常
        if t.cancelled():
            log.error("worker %s was cancelled unexpectedly, restarting", idx)
        elif t.exception() is not None:
            log.error("worker %s died: %r, restarting", idx, t.exception())
        else:
            log.error("worker %s exited unexpectedly, restarting", idx)
        if not shutdown_event.is_set():
            nt = asyncio.create_task(worker_supervisor(idx), name=f"worker-{idx}")
            nt.add_done_callback(lambda x: _on_worker_done(x, idx))
            worker_tasks.append(nt)

    workers = 0 if DEBUG_INSTANCE else max(1, MAX_CONCURRENT_RUNS)
    for i in range(workers):
        t = asyncio.create_task(worker_supervisor(i), name=f"worker-{i}")
        t.add_done_callback(lambda x, idx=i: _on_worker_done(x, idx))
        worker_tasks.append(t)
    log.info("worker tasks created: %s (workers=%s, debug=%s)",
             [t.get_name() for t in worker_tasks], workers, DEBUG_INSTANCE)

    # 兜底看门狗：把「已经没人管了」的 run 落成 failed，避免界面永远挂「运行中」。
    # 判据是**静默多久**，不是**跑了多久**——一次任务没有时长上限（组长可以一直做到做完
    # 为止），但 worker 死亡、或某次外部调用永久挂起时，事件流会彻底停下来：
    # LLM 往返、工具调用、批量素材的每一条进度都会落事件，正常执行不可能静默半小时。
    from datetime import timedelta
    from game_agent.config import RUN_STALE_S, CANCEL_GRACE_S
    from game_agent.db.models import Event

    async def stale_watchdog() -> None:
        while True:
            timed_out: list[dict] = []
            cancelled: list[dict] = []
            try:
                now = datetime.now(_tz.utc)
                async with SessionLocal() as s:
                    live = (await s.execute(select(Run).where(
                        Run.status.in_(("claimed", "running"))))).scalars().all()
                    dead: dict[str, Run] = {}
                    cancel_stuck: dict[str, Run] = {}
                    for r in live:
                        if r.status == "claimed":
                            # 领了活却 5 分钟没跑起来（worker 崩了 / 加载上下文报错）
                            if now - (r.created_at or now) > timedelta(seconds=300):
                                dead[str(r.id)] = r
                            continue
                        last = (await s.execute(select(func.max(Event.created_at)).where(
                            Event.run_id == r.id))).scalar() or r.claimed_at or r.created_at
                        # ① 用户已点「停止」但迟迟没落终态：正常的取消是秒级的，超过宽限期
                        #    仍在 running = 卡在不可取消的 await 上，强制收成 cancelled，
                        #    让「停止中」不再无限转圈（这是 2026-11-10 那个「停止无效」的兜底）。
                        if getattr(r, "cancel_requested", False) and \
                                now - last > timedelta(seconds=CANCEL_GRACE_S):
                            cancel_stuck[str(r.id)] = r
                            continue
                        # ② 一般静默超时（没人管 / 外部调用永久挂起）
                        if now - last > timedelta(seconds=RUN_STALE_S):
                            dead[str(r.id)] = r
                    if dead:
                        timed_out = [{"run_id": rid, "session_id": r.session_id,
                                      "team_id": r.team_id, "project_id": r.project_id}
                                     for rid, r in dead.items()]
                        await s.execute(update(Run).where(
                            Run.id.in_([r.id for r in dead.values()])).values(
                            status="failed", error=f"{RUN_STALE_S // 60} 分钟没有任何执行动静，已中止",
                            finished_at=now))
                        log.warning("watchdog failed %s silent run(s)", len(timed_out))
                    if cancel_stuck:
                        cancelled = [{"run_id": rid, "session_id": r.session_id,
                                      "team_id": r.team_id, "project_id": r.project_id}
                                     for rid, r in cancel_stuck.items()]
                        await s.execute(update(Run).where(
                            Run.id.in_([r.id for r in cancel_stuck.values()])).values(
                            status="cancelled", finished_at=now))
                        log.warning("watchdog force-cancelled %s stuck run(s)", len(cancelled))
                    if dead or cancel_stuck:
                        await s.commit()
                # 会话里补提示：否则该会话只剩用户那句话，聊天页看起来是空白的
                for d in timed_out:
                    await post_session_notice(
                        text=f"本次执行已 {RUN_STALE_S // 60} 分钟没有任何动静（服务端判定为卡死，"
                             "已中止），请重新发起。", **d)
                for d in cancelled:
                    await post_session_notice(
                        text="已按你的要求取消本次执行（该任务卡住未能自行退出，服务端已强制中止）。", **d)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(60)

    watchdog_task = asyncio.create_task(stale_watchdog()) if not DEBUG_INSTANCE else None

    import signal

    loop = asyncio.get_running_loop()
    import uvicorn
    from game_agent.api.app import create_app

    # Windows 会把一些端口段划进 Hyper-V/WSL 的「保留端口」(netsh int ipv4 show
    # excludedportrange)，在里头 bind 直接 WinError 10013，start.cmd 就起不来。这里
    # 启动前先探一下：占不到就顺延到最近能用的端口，并把实际端口打进日志/环境变量，
    # 免得用户对着「权限不足」字样猜。
    _bind_port = _resolve_bind_port(HOST, PORT)
    if _bind_port != PORT:
        os.environ["GA_PORT"] = str(_bind_port)
    app = create_app(frontend_dir=state.frontend_dir)
    # use_colors=False：打包版双击 start.cmd 时 stdout 是 cmd 窗口，uvicorn 默认会按
    # isatty 判定上色并插入 ANSI 转义（如 \x1b[36m、\x1b[?25l），在 GBK 控制台里会显示成
    # 一堆乱码（实测「 Started server process [」前面挂一串奇怪字符）。日志要给人看，
    # 统一纯文本最稳，也便于把输出贴进 issue。
    config = uvicorn.Config(app, host=HOST, port=_bind_port, log_level="info", use_colors=False)
    server = uvicorn.Server(config)
    log.info("Trace listening", extra={"host": HOST, "port": _bind_port, "mode": MODE})

    # 优雅停机：先置停机标志（让在跑的 run 收尾成「服务重启中断」而不是「用户取消」），
    # 再 cancel worker/看门狗，最后停 uvicorn。幂等，避免 CTRL_C 二次进入；
    # 无论从哪里先退出，都保证后台任务被 cancel、不泄漏。
    from game_agent.core import shutdown as _shutdown

    async def _graceful_stop(reason: str) -> None:
        if shutdown_event.is_set():
            return
        log.info("shutting down (%s): cancelling workers/watchdog", reason)
        shutdown_event.set()
        _shutdown.begin_shutdown()
        server.should_exit = True
        for t in worker_tasks:
            t.cancel()
        if watchdog_task is not None:
            watchdog_task.cancel()
        # 给在跑的 run 一点时间把状态落成 failed/interrupted、释放写锁、写提示消息；
        # 上限 20s，超时就放弃（启动清理会把它们当僵尸 run 兜底标 failed）。
        wait_for_tasks = [*worker_tasks, *([watchdog_task] if watchdog_task is not None else [])]
        try:
            if wait_for_tasks:
                await asyncio.wait_for(
                    asyncio.gather(*wait_for_tasks, return_exceptions=True), timeout=20)
        except asyncio.TimeoutError:
            log.warning("graceful stop timed out waiting for workers")
        # 关掉 LLM 的共享长连接（进程级客户端不随单次请求关闭，这里统一收尾）
        try:
            from game_agent.llm.openai_compat import aclose_shared_clients
            await aclose_shared_clients()
        except Exception:  # noqa: BLE001 - 收尾失败不影响退出
            log.warning("关闭 LLM 共享连接失败", exc_info=True)

    def _on_signal(sig) -> None:  # type: ignore[no-untyped-def]
        asyncio.create_task(_graceful_stop(f"signal {sig.name}"))

    for _sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if _sig is None:
            continue
        try:
            loop.add_signal_handler(_sig, _on_signal, _sig)
        except (NotImplementedError, RuntimeError):
            # Windows 的 Selector 事件循环不支持 add_signal_handler；
            # Ctrl-C 仍会让 server.serve() 正常返回，走下面的 finally。
            pass

    # 服务真正开始监听、并且端口确实能连上之后再开浏览器（端口可能已自动顺延，
    # 不能写死；也不能在 serve() 之前就开，否则浏览器会打到还没起来的端口）。
    async def _open_browser_when_ready() -> None:
        if not _want_open_browser():
            return
        browse_host = "127.0.0.1" if HOST in ("0.0.0.0", "", "::") else HOST
        url = f"http://{browse_host}:{_bind_port}/"
        # 等 uvicorn 报告已启动
        for _ in range(240):  # 最多 60s
            if getattr(server, "started", False) or getattr(server, "should_exit", False):
                break
            await asyncio.sleep(0.25)
        if getattr(server, "should_exit", False):
            return

        # 端口连通性探测：必须在**线程里**做（同步 socket/urllib 会阻塞事件循环，
        # 而 uvicorn 就在这个循环里 —— 同步探测自己的端口会互等超时，浏览器就永远开不出来）。
        def _port_open() -> bool:
            import socket as _s
            c = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
            c.settimeout(1.0)
            try:
                c.connect((browse_host, _bind_port))
                return True
            except OSError:
                return False
            finally:
                c.close()

        for _ in range(40):  # 最多 10s
            if await asyncio.to_thread(_port_open):
                break
            if getattr(server, "should_exit", False):
                return
            await asyncio.sleep(0.25)
        try:
            import webbrowser
            await asyncio.to_thread(webbrowser.open, url)
            log.info("已在浏览器打开 %s", url)
        except Exception:
            log.warning("自动打开浏览器失败，请手动访问 %s", url)

    _browser_task = loop.create_task(_open_browser_when_ready())

    try:
        await server.serve()
    finally:
        if not _browser_task.done():
            _browser_task.cancel()
        await _graceful_stop("server stopped")


def _port_is_bindable(host: str, port: int) -> bool:
    """能否在 host:port 上真正监听（含 Windows 保留端口检查）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host or "127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _resolve_bind_port(host: str, port: int) -> int:
    """返回一个可绑定的端口：优先用请求的 port，被占用/被系统保留则就近顺延。

    Windows 在 Hyper-V/WSL 启用后会把成段端口划进 excludedportrange，在段内 bind
    会抛 WinError 10013「以一种访问权限不允许的方式做了一个访问套接字的尝试」，
    用户看到这句根本不知道是端口问题。这里以请求端口为原点，向上、向下交替各探
    200 个端口（系统保留段常常成百上千个，只往一个方向不够）；还不行就原样返回，
    让 uvicorn 报真实错误。
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 8765
    if _port_is_bindable(host, port):
        return port
    for delta in range(1, 201):
        for cand in (port + delta, port - delta):
            if cand < 1024 or cand > 65535:
                continue
            if _port_is_bindable(host, cand):
                try:
                    print(f"[trace] port {port} unavailable (in use or in a Windows "
                          f"reserved range), using {cand}", flush=True)
                except Exception:
                    pass
                return cand
    return port


def _frontend_dir() -> str | None:
    from pathlib import Path
    cand = Path(__file__).resolve().parent.parent / "web" / "dist"
    return str(cand) if cand.exists() else None


if __name__ == "__main__":
    main()
