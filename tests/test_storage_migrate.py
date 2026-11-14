"""「设置 / 存储空间」——数据目录换位置 + 自动迁移的回归测试。

为什么专门锁这几条（都是踩过的坑）：
  1. 指针文件写的是 JSON ``{"data_dir": ...}``，读的时候必须解析出路径。以前直接把整段
     文本当路径返回，重启后 DATA_DIR 变成当前目录下名叫 ``{"data_dir": …}`` 的怪目录，
     用户看到的是「迁移完重启，数据全没了」。
  2. 迁移是「先复制、复制成功才写指针」，旧目录**迁移当刻不删**：中途失败还能退回。
  3. DB 不能直接复制文件（正在被写，可能读到写了一半的页），必须走 sqlite backup 快照；
     顺带验证 WAL 里尚未 checkpoint 的数据也不会丢。
  4. 启动时删旧目录那一步必须有 ``Path`` 可用。以前 ``_cleanup_old_data_dir`` 里用了
     ``Path`` 却没导入，NameError 被 except 吞掉当成「没有旧目录」，于是旧目录（C 盘那
     10 GB）永远删不掉——这条用真实调用锁住，别再靠肉眼。
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from game_agent import config as cfg
from game_agent.api.deps import get_current_context
from game_agent.api.routers import storage
from game_agent.core.context import RequestContext


# ---------------------------------------------------------------- 指针文件
@pytest.fixture
def pointer(tmp_path, monkeypatch):
    """把指针文件重定向到 tmp（绝不碰用户真实的 ~/.trace_location）。"""
    pf = tmp_path / "ptr" / ".trace_location"
    monkeypatch.setattr(cfg, "_pointer_files", lambda: [pf])
    return pf


def test_pointer_roundtrip_json(pointer, tmp_path):
    """写进去的是 JSON，读出来必须是路径本身（不是整段 JSON 文本）。"""
    target = tmp_path / "D-data" / "TraceData"
    cfg.write_data_dir_pointer(str(target))
    assert pointer.read_text(encoding="utf-8").startswith("{")
    assert Path(cfg.read_data_dir_pointer()) == target
    assert Path(cfg.read_data_dir_pointer()).name == "TraceData"  # 不是 '{"data_dir": ...}'


def test_pointer_accepts_legacy_plain_text(pointer, tmp_path):
    """早期手写的纯路径一行也认；坏 JSON / 空文件不能当路径用。"""
    target = tmp_path / "old-style"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(target), encoding="utf-8")
    assert Path(cfg.read_data_dir_pointer()) == target

    pointer.write_text("", encoding="utf-8")
    assert cfg.read_data_dir_pointer() == ""
    pointer.write_text('{"wrong_key": 1}', encoding="utf-8")
    assert cfg.read_data_dir_pointer() == ""


def test_pointer_clear_restores_default(pointer, tmp_path):
    """恢复默认 = 删指针（空串），下次启动回到 ~/.trace。"""
    cfg.write_data_dir_pointer(str(tmp_path / "elsewhere"))
    assert pointer.is_file()
    cfg.write_data_dir_pointer("")
    assert not pointer.exists()
    assert cfg.read_data_dir_pointer() == ""


# ---------------------------------------------------------------- 接口沙箱
@pytest.fixture
def api(tmp_path, monkeypatch):
    """storage 路由 + 假登录态；DATA_DIR 钉到 tmp，环境变量 GA_DATA_DIR 清掉。"""
    data_dir = tmp_path / "datadir"
    data_dir.mkdir()
    monkeypatch.setattr(storage, "DATA_DIR", data_dir)
    monkeypatch.setattr(storage, "_DEFAULT_DATA_DIR", str(tmp_path / "home" / ".trace"))
    monkeypatch.delenv("GA_DATA_DIR", raising=False)
    # 读指针也要隔离：否则「开发机上真的换过数据目录」会让 pending_switch_to 非空，
    # 测试跟着那台机器的状态漂（这里踩过：本机指针指向 F:\game\trace 时用例开始失败）。
    monkeypatch.setattr(storage, "read_data_dir_pointer", lambda: "")
    # 写指针会写到用户主目录：测试里换成记录器，既不动真实文件也能断言参数
    written: list[str] = []
    monkeypatch.setattr(storage, "write_data_dir_pointer", lambda v: written.append(v))

    app = FastAPI()
    app.include_router(storage.router)
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user_id="u-test", team_id="t-test", role="owner")
    return {"app": app, "data_dir": data_dir, "written": written, "tmp": tmp_path}


def request(api, method, path, **kwargs):
    async def send():
        transport = httpx.ASGITransport(app=api["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, **kwargs)

    return asyncio.run(send())


def _seed_data_dir(d: Path) -> None:
    """造一份「像真的」数据目录：日志、项目工作区、聊天附图、以及一个真 sqlite 库。"""
    (d / "logs").mkdir(parents=True, exist_ok=True)
    (d / "logs" / "app.log").write_text("hello-log", encoding="utf-8")
    (d / "projects" / "p1").mkdir(parents=True, exist_ok=True)
    (d / "projects" / "p1" / "a.txt").write_text("project-file", encoding="utf-8")
    (d / "chat_uploads" / "team").mkdir(parents=True, exist_ok=True)
    (d / "chat_uploads" / "team" / "pic.png").write_bytes(b"\x89PNG-data")


def test_storage_info_shape(api):
    r = request(api, "GET", "/api/system/storage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data_dir"] == str(api["data_dir"])
    assert body["is_default"] is False
    assert body["env_override_dir"] == ""
    assert body["pending_switch_to"] == ""
    assert "used_human" in body and "disk_free_human" in body


def test_storage_info_reports_env_override(api, monkeypatch):
    """被 GA_DATA_DIR 钉死时，设置页要能知道（否则迁移注定白做）。"""
    monkeypatch.setenv("GA_DATA_DIR", str(api["tmp"] / "pinned"))
    body = request(api, "GET", "/api/system/storage").json()
    assert body["env_override_dir"] == str(api["tmp"] / "pinned")


def test_storage_info_reports_pending_switch(api, monkeypatch):
    """指针已经指向别处、但本进程还在用旧目录 = 「改过没重启」，界面上要说清楚。"""
    elsewhere = api["tmp"] / "new-place"
    monkeypatch.setattr(storage, "read_data_dir_pointer", lambda: str(elsewhere))
    body = request(api, "GET", "/api/system/storage").json()
    assert body["pending_switch_to"] == str(elsewhere)


def test_storage_info_picker_native_flag(api):
    """前端据此决定「浏览…」是按原生窗口还是回退内置目录浏览器。"""
    body = request(api, "GET", "/api/system/storage").json()
    assert isinstance(body["picker_native"], bool)


# ---------------------------------------------------------------- 迁移进度
def test_migrate_progress_shapes():
    """进度快照：扫描阶段不给百分比（前端显示不确定条），复制阶段按字节算，收尾到 100%。"""
    prog = storage.MigrateProgress()
    assert prog.snapshot()["phase"] == "idle"
    assert prog.snapshot()["running"] is False

    prog.begin(Path("C:/src"), Path("D:/dst"))
    assert prog.snapshot()["percent"] is None
    prog.phase = "copy"
    prog.total_bytes, prog.done_bytes = 1000, 250
    snap = prog.snapshot()
    assert snap["running"] is True and 24.9 < snap["percent"] < 25.1
    assert snap["speed_bps"] >= 0
    prog.finish()
    snap = prog.snapshot()
    assert snap["phase"] == "done" and snap["percent"] == 100.0 and snap["running"] is False


def test_progress_endpoint_reports_state(api, monkeypatch):
    """接口直接暴露当前进度（前端每 400ms 轮询它画进度条）。"""
    prog = storage.MigrateProgress()
    prog.begin(Path("C:/src"), Path("D:/dst"))
    prog.phase, prog.total_bytes, prog.done_bytes = "copy", 2000, 500
    monkeypatch.setattr(storage, "PROGRESS", prog)
    body = request(api, "GET", "/api/system/storage/migrate/progress").json()
    assert body["running"] is True and body["phase"] == "copy"
    assert 24.9 < body["percent"] < 25.1 and body["total_bytes"] == 2000


def test_do_migrate_skips_marker_and_counts_progress(api, tmp_path):
    """复制循环：跳过待清理标记、DB 走一致性快照，且进度计数与实际复制量一致。"""
    src = tmp_path / "src"
    _seed_data_dir(src)
    db = src / storage._DB_NAME
    conn = sqlite3.connect(str(db))
    conn.execute("create table t(x integer)")
    conn.execute("insert into t values (7)")
    conn.commit()
    conn.close()
    (src / storage.PENDING_CLEANUP_FILE).write_text("C:/old", encoding="utf-8")
    dst = tmp_path / "dst"

    storage._do_migrate(src, dst)
    assert (dst / "logs" / "app.log").is_file()
    assert (dst / storage._DB_NAME).is_file()
    assert not (dst / storage.PENDING_CLEANUP_FILE).exists()
    conn = sqlite3.connect(str(dst / storage._DB_NAME))
    assert conn.execute("select x from t").fetchone() == (7,)
    conn.close()
    snap = storage.PROGRESS.snapshot()
    assert snap["files_done"] == snap["files_total"] and snap["files_total"] >= 3
    assert snap["done_bytes"] == snap["total_bytes"] > 0


# ---------------------------------------------------------------- 原生选目录
def test_pick_dir_returns_selected_path(api, monkeypatch):
    """「浏览…」走系统原生窗口：接口把用户选中的绝对路径带回前端。"""
    import game_agent.platform_ui as ui

    monkeypatch.setattr(ui, "pick_directory", lambda title="": r"D:\TraceData")
    body = request(api, "POST", "/api/system/storage/pick-dir", json={"title": "选一下"}).json()
    assert body["path"] == r"D:\TraceData" and body["cancelled"] is False


def test_pick_dir_cancel_is_not_error(api, monkeypatch):
    """用户点取消：不是错误，返回空路径（前端保留他手填的内容）。"""
    import game_agent.platform_ui as ui

    monkeypatch.setattr(ui, "pick_directory", lambda title="": "")
    r = request(api, "POST", "/api/system/storage/pick-dir", json={})
    assert r.status_code == 200 and r.json()["cancelled"] is True


def test_pick_dir_busy_and_unavailable(api, monkeypatch):
    """窗口已开着 → 409；这台机器弹不出窗口 → 501（前端据此回退内置浏览器）。"""
    import game_agent.platform_ui as ui

    def busy(title=""):
        raise ui.PickerBusy("已经有一个选择窗口开着")

    monkeypatch.setattr(ui, "pick_directory", busy)
    assert request(api, "POST", "/api/system/storage/pick-dir", json={}).status_code == 409

    def nope(title=""):
        raise ui.PickerUnavailable("没有桌面")

    monkeypatch.setattr(ui, "pick_directory", nope)
    assert request(api, "POST", "/api/system/storage/pick-dir", json={}).status_code == 501


# ---------------------------------------------------------------- 迁移：拒绝项
@pytest.mark.parametrize(
    "target_maker, needle",
    [
        (lambda a: str(a["data_dir"]), "无需迁移"),
        (lambda a: str(a["data_dir"] / "inner"), "内部"),
        (lambda a: "relative/dir", "绝对路径"),
        (lambda a: "", "请选择目标目录"),
    ],
)
def test_migrate_rejects_dangerous_targets(api, target_maker, needle):
    r = request(api, "POST", "/api/system/storage/migrate", json={"target_dir": target_maker(api)})
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]
    assert api["written"] == []          # 拒绝了就不能写指针
    assert not (api["data_dir"] / storage.PENDING_CLEANUP_FILE).exists()


def test_migrate_rejects_non_empty_target(api):
    """非空目标不接：混进别的文件之后没人分得清哪份是平台的。"""
    target = api["tmp"] / "not-empty"
    target.mkdir()
    (target / "existing.txt").write_text("x", encoding="utf-8")
    r = request(api, "POST", "/api/system/storage/migrate", json={"target_dir": str(target)})
    assert r.status_code == 400
    assert "非空" in r.json()["detail"]


def test_migrate_rejected_when_env_pins_data_dir(api, monkeypatch):
    """环境变量钉死时直接拒绝，并说清怎么解开——别让用户白等一次全量复制。"""
    monkeypatch.setenv("GA_DATA_DIR", str(api["tmp"] / "pinned"))
    target = api["tmp"] / "target"
    r = request(api, "POST", "/api/system/storage/migrate", json={"target_dir": str(target)})
    assert r.status_code == 400
    assert "GA_DATA_DIR" in r.json()["detail"]
    assert not target.exists()
    assert api["written"] == []


# ---------------------------------------------------------------- 迁移：正常路径
def test_migrate_copies_everything_and_keeps_source(api):
    """文件全搬、DB 走 backup 快照（含 WAL 未 checkpoint 的数据）、源目录不删。"""
    src = api["data_dir"]
    _seed_data_dir(src)

    db = src / storage._DB_NAME
    conn = sqlite3.connect(str(db))
    conn.execute("create table t(x int)")
    conn.execute("insert into t values (42)")
    conn.commit()
    conn.execute("pragma journal_mode=wal")
    conn.execute("insert into t values (99)")   # 只进 WAL，没 checkpoint
    conn.commit()                               # 连接故意不关：模拟「库正在被写」
    # 旧清理标记不该被搬到新目录（否则新实例启动会删掉刚搬过去的这东西）
    (src / storage.PENDING_CLEANUP_FILE).write_text("stale", encoding="utf-8")

    target = api["tmp"] / "newdisk" / "TraceData"
    r = request(api, "POST", "/api/system/storage/migrate", json={"target_dir": str(target)})
    conn.close()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["need_restart"] is True
    assert body["is_default"] is False

    # 1) 普通文件都在
    assert (target / "logs" / "app.log").read_text(encoding="utf-8") == "hello-log"
    assert (target / "projects" / "p1" / "a.txt").read_text(encoding="utf-8") == "project-file"
    assert (target / "chat_uploads" / "team" / "pic.png").read_bytes() == b"\x89PNG-data"
    # 2) DB 一致性快照：WAL 里没 checkpoint 的那行也不能丢
    c2 = sqlite3.connect(str(target / storage._DB_NAME))
    try:
        rows = sorted(x for (x,) in c2.execute("select x from t").fetchall())
    finally:
        c2.close()
    assert rows == [42, 99]
    # 3) 指针写成新目录 + 新目录里留「旧目录待清理」标记；旧目录此刻还在
    assert api["written"] == [str(target)]
    marker = (target / storage.PENDING_CLEANUP_FILE).read_text(encoding="utf-8")
    assert Path(marker) == src.resolve()
    assert src.is_dir(), "迁移当刻绝不能删旧目录（迁移失败/新盘不可用时要能退回）"


def test_migrate_back_to_default_clears_pointer(api):
    """搬回默认位置时指针要清空（空串=走默认），不能把默认路径写死进指针。"""
    src = api["data_dir"]
    _seed_data_dir(src)
    default = Path(api["tmp"] / "home" / ".trace")
    r = request(api, "POST", "/api/system/storage/migrate", json={"target_dir": str(default)})
    assert r.status_code == 200, r.text
    assert r.json()["is_default"] is True
    assert api["written"] == [""]


# ---------------------------------------------------------------- 启动时清理旧目录
def test_cleanup_old_data_dir_actually_deletes(tmp_path, monkeypatch):
    """迁移后重启删旧目录：这条以前是「静默不生效」（Path 没导入被 except 吞掉）。"""
    from game_agent import main as ga_main

    old = tmp_path / "old"
    (old / "logs").mkdir(parents=True)
    (old / "logs" / "app.log").write_text("x", encoding="utf-8")
    new = tmp_path / "new"
    new.mkdir()
    (new / cfg.PENDING_CLEANUP_FILENAME).write_text(str(old), encoding="utf-8")
    monkeypatch.setattr(cfg, "DATA_DIR", new)

    ga_main._cleanup_old_data_dir(_SilentLog())
    assert not old.exists(), "旧数据目录应该被删掉（否则 C 盘那份永远躺着）"
    assert not (new / cfg.PENDING_CLEANUP_FILENAME).exists()


@pytest.mark.parametrize("bad", ["current", "home", "root"])
def test_cleanup_old_data_dir_refuses_suspicious_targets(tmp_path, monkeypatch, bad):
    """护栏：标记指向当前目录 / 用户主目录 / 盘符根时一律不删。"""
    from game_agent import main as ga_main

    new = tmp_path / "new"
    new.mkdir()
    victim = {"current": new, "home": Path.home(), "root": Path(new.anchor)}[bad]
    (new / cfg.PENDING_CLEANUP_FILENAME).write_text(str(victim), encoding="utf-8")
    monkeypatch.setattr(cfg, "DATA_DIR", new)

    ga_main._cleanup_old_data_dir(_SilentLog())
    assert victim.exists(), f"{bad} 不该被删"
    assert not (new / cfg.PENDING_CLEANUP_FILENAME).exists(), "可疑标记也要清掉，别每次启动都试"


class _SilentLog:
    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass
