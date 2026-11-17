"""单实例守卫回归测试：第二个实例不许动第一个实例正在跑的 run。

2026-11-17 用户连问「你老是重启」：主实例 11188 一直在 :18888 正常服务，但日志里
10:11:42 / 10:21:52 / 10:25:27 各有一条 ``cleaned N zombie run(s) from previous process``，
库里还有一批 ``status=succeeded`` 却带 ``error=服务重启，运行中断，请重新发起`` 的 run。
根因：另起的实例（联调/顺手又双击了一次 start.cmd）**没带 GA_DEBUG_INSTANCE=1**，
启动清理把所有 running/queued run 当成上个进程的尸体标成 failed（还往每个会话补一条
「服务重启，本次执行被中断」），而 pid 文件写在同一数据目录里、会被后来者直接覆盖。

这里锁四件事：
1. 抢到锁 → ``acquired``，并且**算数据目录的唯一主人**（可以清僵尸）；
2. 锁已被别的句柄占着 → ``already_running`` 且 **不是**主人，提示语里给得出占用者与地址；
3. ``GA_DEBUG_INSTANCE=1`` / ``GA_ALLOW_MULTI_INSTANCE=1`` → 放行但**不是**主人；
4. 锁随句柄释放而释放（进程崩溃时由操作系统回收），不会留下把人永久挡住的死锁。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from game_agent.core import instance_lock
from game_agent.core.instance_lock import (
    ALLOW_MULTI_ENV,
    InstanceLock,
    clear_instance_info,
    describe_owner,
    ensure_single_instance,
    guard_message,
    info_path,
    lock_path,
    read_instance_info,
    write_instance_info,
)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def test_acquired_when_nobody_holds_lock(data_dir: Path) -> None:
    res = ensure_single_instance(data_dir, debug=False, allow_multi=False)
    try:
        assert res.ok is True
        assert res.reason == "acquired"
        assert res.owns_data_dir is True  # 只有它才允许清僵尸 run / 删旧数据目录
        assert res.lock is not None and res.lock.held
        assert lock_path(data_dir).is_file()
    finally:
        if res.lock is not None:
            res.lock.release()


def test_second_instance_is_refused_and_does_not_own_data_dir(data_dir: Path) -> None:
    first = InstanceLock(data_dir)
    assert first.acquire() is True
    try:
        second = ensure_single_instance(data_dir, debug=False, allow_multi=False)
        assert second.ok is False
        assert second.reason == "already_running"
        # 关键断言：被拦下的实例绝不能「拥有」这套数据目录——否则它会去清别人的 run
        assert second.owns_data_dir is False
        assert second.lock is None
        assert "已经在被另一个 Trace 实例使用" in second.message
        assert str(data_dir) in second.message
        # 提示里要给出可照做的三条出路（打开地址 / 先停再起 / 加调试开关）
        assert "GA_DEBUG_INSTANCE=1" in second.message
        assert ALLOW_MULTI_ENV in second.message
    finally:
        first.release()


def test_lock_is_reentrant_per_holder_and_released_on_release(data_dir: Path) -> None:
    first = InstanceLock(data_dir)
    assert first.acquire() is True
    assert first.acquire() is True  # 同一个持有者重复抢不算失败
    first.release()
    # 释放后别人应该能抢到（模拟：旧进程退出 → 新进程正常启动）
    res = ensure_single_instance(data_dir, debug=False, allow_multi=False)
    try:
        assert res.ok is True and res.reason == "acquired"
    finally:
        if res.lock is not None:
            res.lock.release()


def test_debug_instance_is_allowed_but_never_owns_data_dir(data_dir: Path) -> None:
    owner = InstanceLock(data_dir)
    assert owner.acquire() is True
    try:
        res = ensure_single_instance(data_dir, debug=True, allow_multi=False)
        assert res.ok is True
        assert res.reason == "debug_instance"
        assert res.owns_data_dir is False  # 联调实例永不清库
    finally:
        owner.release()


def test_allow_multi_is_allowed_but_never_owns_data_dir(data_dir: Path) -> None:
    owner = InstanceLock(data_dir)
    assert owner.acquire() is True
    try:
        res = ensure_single_instance(data_dir, debug=False, allow_multi=True)
        assert res.ok is True
        assert res.reason == "multi_instance_allowed"
        assert res.owns_data_dir is False
    finally:
        owner.release()


def test_allow_multi_reads_env(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    owner = InstanceLock(data_dir)
    assert owner.acquire() is True
    monkeypatch.setenv(ALLOW_MULTI_ENV, "1")
    try:
        res = ensure_single_instance(data_dir, debug=False)
        assert res.ok is True and res.reason == "multi_instance_allowed"
    finally:
        owner.release()
        monkeypatch.delenv(ALLOW_MULTI_ENV, raising=False)


def test_instance_info_roundtrip_and_owner_description(data_dir: Path) -> None:
    write_instance_info(data_dir, pid=os.getpid(), host="127.0.0.1", port=18888)
    info = read_instance_info(data_dir)
    assert info is not None
    assert info["pid"] == os.getpid()
    assert info["port"] == 18888
    assert info["url"] == "http://127.0.0.1:18888/"
    desc = describe_owner(data_dir)
    assert f"PID {os.getpid()}" in desc
    assert "http://127.0.0.1:18888/" in desc
    clear_instance_info(data_dir)
    assert info_path(data_dir).exists() is False


def test_owner_description_falls_back_to_lockfile_pid(data_dir: Path) -> None:
    """没有 info json（老版本实例）时，退回去读锁文件里的 PID 也要说得出「谁占着」。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path(data_dir).write_text("4242", encoding="ascii")  # 只有锁文件里的 PID
    assert info_path(data_dir).exists() is False
    desc = describe_owner(data_dir)
    assert "PID 4242" in desc
    assert str(data_dir) in guard_message(data_dir)


def test_owner_description_survives_locked_lockfile(data_dir: Path) -> None:
    """锁文件被操作系统锁着时别的进程读不了它——不能因此抛异常，必须给出可读提示。"""
    owner = InstanceLock(data_dir)
    assert owner.acquire() is True
    try:
        clear_instance_info(data_dir)  # 只剩被锁住的锁文件，内容读不到
        desc = describe_owner(data_dir)
        assert "已经在被另一个 Trace 实例使用" in guard_message(data_dir)
        assert desc  # 非空即可：可能是「占用者未知」，「写不出来」才是 bug
        res = ensure_single_instance(data_dir, debug=False, allow_multi=False)
        assert res.ok is False and res.reason == "already_running"
    finally:
        owner.release()


def test_legacy_pidfile_instance_is_detected(data_dir: Path) -> None:
    """过渡期：正在跑的是「加锁之前的老版本」（不持锁，只在 pid 文件里留了 PID）。

    光靠锁拦不住它，第二个实例照样会去清它正在跑的 run —— 所以还要查 pid 文件。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    # 造一个「活着且命令行像本服务」的进程：用当前测试进程顶替（命令行含 pytest，故打桩）
    (data_dir / instance_lock.PIDFILE_NAME).write_text(str(99999), encoding="ascii")
    fake = _FakePsutil(alive_pid=99999, cmdline="/usr/bin/python -m game_agent.main")
    real = sys.modules.get("psutil")
    sys.modules["psutil"] = fake  # type: ignore[assignment]
    try:
        assert instance_lock._live_other_instance(data_dir) == 99999
        res = ensure_single_instance(data_dir, debug=False, allow_multi=False)
        assert res.ok is False and res.reason == "already_running"
        assert "PID 99999" in res.message  # 老实例不写 info json，也要报出 PID
    finally:
        if real is not None:
            sys.modules["psutil"] = real
        else:
            sys.modules.pop("psutil", None)


def test_legacy_pidfile_ignored_when_process_gone(data_dir: Path) -> None:
    """pid 文件里的 PID 已经不存在 / 不是本服务 → 不拦，正常抢锁。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / instance_lock.PIDFILE_NAME).write_text("99999", encoding="ascii")
    fake = _FakePsutil(alive_pid=None, cmdline="")
    real = sys.modules.get("psutil")
    sys.modules["psutil"] = fake  # type: ignore[assignment]
    try:
        res = ensure_single_instance(data_dir, debug=False, allow_multi=False)
        try:
            assert res.ok is True and res.reason == "acquired"
        finally:
            if res.lock is not None:
                res.lock.release()
    finally:
        if real is not None:
            sys.modules["psutil"] = real
        else:
            sys.modules.pop("psutil", None)


def test_legacy_pidfile_ignored_for_other_program(data_dir: Path) -> None:
    """pid 文件里那个 PID 活着，但命令行不是本服务（PID 被别的程序复用了）→ 不拦。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / instance_lock.PIDFILE_NAME).write_text("99999", encoding="ascii")
    fake = _FakePsutil(alive_pid=99999, cmdline="C:/Windows/explorer.exe")
    real = sys.modules.get("psutil")
    sys.modules["psutil"] = fake  # type: ignore[assignment]
    try:
        assert instance_lock._live_other_instance(data_dir) is None
    finally:
        if real is not None:
            sys.modules["psutil"] = real
        else:
            sys.modules.pop("psutil", None)


def test_stale_instance_info_is_not_reported_as_owner(data_dir: Path) -> None:
    """info 文件是一个被强杀、没来得及清理的实例留下的 → 别把它的地址当占用者误导用户。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    write_instance_info(data_dir, pid=999999, host="127.0.0.1", port=18889)  # 这个 PID 不存在
    desc = describe_owner(data_dir)
    assert "18889" not in desc  # 死端口不能出现在「你去打开这个地址」里
    assert "（占用者未知）" == desc


def test_live_prefer_pid_wins_over_stale_info(data_dir: Path) -> None:
    """调用方用进程扫描确认过的活 PID 优先；info 里的死 PID 不进提示。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    write_instance_info(data_dir, pid=999999, host="127.0.0.1", port=18889)
    desc = describe_owner(data_dir, prefer_pid=os.getpid())
    assert f"PID {os.getpid()}" in desc
    assert "999999" not in desc


def test_listen_addrs_picks_listening_port() -> None:
    """占用者没写 info（老版本实例）时，从进程监听端口反推可点地址。"""

    class _Addr:
        def __init__(self, ip: str, port: int) -> None:
            self.ip = ip
            self.port = port

    class _Conn:
        def __init__(self, status: str, ip: str, port: int) -> None:
            self.status = status
            self.laddr = _Addr(ip, port)

    class _Proc:
        def __init__(self, conns: list[_Conn]) -> None:
            self._conns = conns

        def net_connections(self, kind: str = "inet"):  # noqa: ARG002
            return self._conns

    proc = _Proc([_Conn("ESTABLISHED", "127.0.0.1", 50000), _Conn("LISTEN", "127.0.0.1", 18888)])
    assert instance_lock._listen_addrs(proc) == "http://127.0.0.1:18888/"

    class _Denied:
        def net_connections(self, kind: str = "inet"):  # noqa: ARG002
            raise OSError("AccessDenied")

    assert instance_lock._listen_addrs(_Denied()) is None


class _FakePsutil:
    """替身：不做真进程查询，只按给定结论回答（避免真去开别人的进程）。"""

    def __init__(self, *, alive_pid: int | None, cmdline: str) -> None:
        self._alive_pid = alive_pid
        self._cmdline = cmdline

    def pid_exists(self, pid: int) -> bool:
        return self._alive_pid is not None and int(pid) == int(self._alive_pid)

    def Process(self, pid: int):  # noqa: N802 - 模仿 psutil 的命名
        outer = self

        class _P:
            def cmdline(self) -> list[str]:
                return outer._cmdline.split()

        return _P()


class _FakeScanPsutil:
    """替身：给 ``process_iter`` 一组预置进程（pid / cmdline / 可选环境变量）。"""

    def __init__(self, procs: list[dict]) -> None:
        self._procs = procs

    def process_iter(self, _attrs=None):  # noqa: ANN001
        outer = self

        class _P:
            def __init__(self, info: dict) -> None:
                self.info = {
                    "pid": info["pid"],
                    "name": info.get("name", "python.exe"),
                    "cmdline": info["cmdline"],
                }

            def environ(self) -> dict:
                env = dict(outer._procs[[p["pid"] for p in outer._procs].index(self.info["pid"])]
                           .get("env") or {})
                return env

        return [_P(p) for p in self._procs]


def _with_psutil(fake):  # noqa: ANN001, ANN202
    """把 sys.modules['psutil'] 临时换成替身。"""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        real = sys.modules.get("psutil")
        sys.modules["psutil"] = fake
        try:
            yield
        finally:
            if real is not None:
                sys.modules["psutil"] = real
            else:
                sys.modules.pop("psutil", None)

    return _cm()


def test_scan_detects_running_instance_on_same_data_dir(data_dir: Path) -> None:
    """全机扫：同一条数据目录上还有 game_agent.main 在跑 → 找到它（不是 pid 文件那条路）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    fake = _FakeScanPsutil([
        {"pid": 4242, "cmdline": ["python", "-m", "game_agent.main"],
         "env": {"GA_DATA_DIR": str(data_dir)}},
        {"pid": 5353, "cmdline": ["python", "-m", "game_agent.main"],
         "env": {"GA_DATA_DIR": r"C:\some\other\dir"}},  # 另一条数据目录，不关我事
        {"pid": 6464, "cmdline": ["not-game-agent.exe"], "env": {}},
    ])
    with _with_psutil(fake):
        assert instance_lock._scan_other_instance(data_dir) == 4242


def test_scan_ignores_cmd_shell_wrapper(data_dir: Path) -> None:
    """`cmd /c "set GA_PORT=.. && python -m game_agent.main"` 那个 cmd.exe 不是另一个实例。

    （实测踩过：只看命令行会把外壳进程当成占用者，结果服务被自己拦住起不来。）
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    fake = _FakeScanPsutil([
        {"pid": 5151, "name": "cmd.exe",
         "cmdline": ["cmd", "/c", "set GA_PORT=17785&& python -m game_agent.main"],
         "env": {"GA_DATA_DIR": str(data_dir)}},
    ])
    with _with_psutil(fake):
        assert instance_lock._scan_other_instance(data_dir) is None


def test_is_service_process_classification() -> None:
    ok = instance_lock._is_service_process
    # 真正的服务进程
    assert ok("python.exe", ["python", "-m", "game_agent.main"]) is True
    assert ok("python", ["D:/x/python.exe", "-m", "game_agent.main"]) is True
    assert ok("Trace.exe", ["Trace.exe"]) is True
    # 外壳 / 别的程序都不算
    assert ok("cmd.exe", ["cmd", "/c", "python -m game_agent.main"]) is False
    assert ok("bash", ["bash", "-c", "python -m game_agent.main"]) is False
    assert ok("explorer.exe", ["explorer.exe"]) is False
    assert ok("python.exe", ["python", "-m", "pytest"]) is False


def test_scan_ignores_debug_and_allowed_instances(data_dir: Path) -> None:
    """对方是联调实例 / 被放行实例时不拦——它们本来就不清僵尸、不抢队列。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    fake = _FakeScanPsutil([
        {"pid": 4242, "cmdline": ["python", "-m", "game_agent.main"],
         "env": {"GA_DATA_DIR": str(data_dir), "GA_DEBUG_INSTANCE": "1"}},
        {"pid": 4343, "cmdline": ["python", "-m", "game_agent.main"],
         "env": {"GA_DATA_DIR": str(data_dir), "GA_ALLOW_MULTI_INSTANCE": "1"}},
    ])
    with _with_psutil(fake):
        assert instance_lock._scan_other_instance(data_dir) is None
        res = ensure_single_instance(data_dir, debug=False, allow_multi=False)
        try:
            assert res.ok is True and res.reason == "acquired"
        finally:
            if res.lock is not None:
                res.lock.release()


def test_scan_skips_self(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    fake = _FakeScanPsutil([
        {"pid": os.getpid(), "cmdline": ["python", "-m", "game_agent.main"],
         "env": {"GA_DATA_DIR": str(data_dir)}},
    ])
    with _with_psutil(fake):
        assert instance_lock._scan_other_instance(data_dir) is None


def test_lock_unavailable_degrades_to_allowed(data_dir: Path) -> None:
    """锁文件建不出来（目录只读等）时不让服务起不来：放行，但不算数据目录主人。"""

    def _boom(*_a, **_kw):
        raise OSError("read-only")

    monkeypatch_target = instance_lock.InstanceLock
    try:
        instance_lock.InstanceLock = lambda d: _BrokenLock(d)  # type: ignore[assignment]
        res = ensure_single_instance(data_dir, debug=False, allow_multi=False)
        assert res.ok is True
        assert res.reason == "lock_unavailable"
        assert res.owns_data_dir is False
    finally:
        instance_lock.InstanceLock = monkeypatch_target


class _BrokenLock:
    """模拟「锁文件根本建不出来」：acquire 返回 False 且文件不存在。"""

    def __init__(self, data_dir: Path) -> None:
        self.path = lock_path(data_dir)

    @property
    def held(self) -> bool:
        return False

    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        return None
