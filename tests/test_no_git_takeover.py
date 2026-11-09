"""git 接管已完全下线的钉子：谁把分支/提交/合并/dirty 检查加回来，这里就红。

移除原因：接管逻辑要求工作区 clean，用户「脱离 git」的项目里 .git 只是碰巧存在，
每次启动又被回填成 git 项目 → run 直接失败在「工作区不干净，请先提交或 stash」。
改动审阅/回滚走工具层写前快照（snapshots），与 git 无关。
"""
from __future__ import annotations

import importlib

import pytest


def test_git_ops_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("game_agent.orchestration.git_ops")


def test_merge_router_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("game_agent.api.routers.approvals")


def test_project_api_has_no_git_knobs() -> None:
    from game_agent.api.routers import projects
    assert not hasattr(projects, "ProjectPatch")
    assert "git_enabled" not in projects.ProjectIn.model_fields
    assert "base_branch" not in projects.ProjectIn.model_fields


def test_change_report_has_no_git_fields(tmp_path) -> None:
    from game_agent.orchestration import change_report
    cr = change_report.build_change_report(tmp_path, run_id="00000000000000000000000000000000",
                                           run_type="modify", test_report=None,
                                           has_placeholder=False, artifacts=[], model_map=None)
    assert set(cr) == {"run_type", "files", "summary", "test_report", "has_placeholder",
                       "placeholder_count", "placeholders", "model_map"}


def test_change_report_excludes_scratch_files(tmp_path, monkeypatch) -> None:
    """用户可见的「改动的文件」卡片不能混进 .trace/tmp/ 过程文件（run 9a9656bc 回归）。

    那次变更清单报 14 个文件，其中 9 个是 .trace/tmp/*（探针、argfile 生成脚本、ps1），
    真正产品改动只有 5 个。_landed_actions 已经过滤过程文件，build_change_report 漏了。
    """
    from game_agent.orchestration import change_report, snapshots
    fake = {
        "files": [
            {"path": "src/Main.java", "added": 10, "deleted": 2, "status": "modified"},
            {"path": "docs/api.md", "added": 4, "deleted": 0, "status": "modified"},
            {"path": ".trace/tmp/gen_argfile.py", "added": 30, "deleted": 0, "status": "added"},
            {"path": ".trace/tmp/probe/run_probe.ps1", "added": 5, "deleted": 0, "status": "added"},
        ],
        "summary": {"count": 4, "added": 49, "deleted": 2},
    }
    monkeypatch.setattr(snapshots, "list_changes", lambda *a, **k: fake)
    cr = change_report.build_change_report(tmp_path, run_id="x", run_type="modify",
                                           test_report=None, has_placeholder=False,
                                           artifacts=[], model_map=None)
    paths = {f["file"] for f in cr["files"]}
    assert paths == {"src/Main.java", "docs/api.md"}, paths
    # summary 必须按过滤后的文件重算，而不是沿用被污染的原 summary
    assert cr["summary"] == {"count": 2, "added": 14, "deleted": 2}, cr["summary"]


def test_list_changes_excludes_scratch_files(tmp_path, monkeypatch) -> None:
    """「已编辑 N 个文件」卡片的数据源 (GET /changes → snapshots.list_changes)
    也不能混进 .trace/tmp/ 过程文件。过滤以前只在 change_report 层，接口层漏了。"""
    from game_agent.orchestration import snapshots, tempfiles
    monkeypatch.setattr(snapshots, "SNAPSHOT_ROOT", tmp_path / "snaps")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "src").mkdir()
    (ws / "src" / "Main.java").write_text("class Main {}\n", encoding="utf-8")
    (ws / ".trace" / "tmp").mkdir(parents=True)
    (ws / ".trace" / "tmp" / "gen_argfile.py").write_text("print(1)\n", encoding="utf-8")

    rid = "a" * 32
    snapshots.remember(rid, ws, "src/Main.java", tool="write_file")
    snapshots.remember(rid, ws, ".trace/tmp/gen_argfile.py", tool="write_file")
    # 快照后真正改动内容，list_changes 才会把文件报出来
    (ws / "src" / "Main.java").write_text("class Main { int x; }\n", encoding="utf-8")
    (ws / ".trace" / "tmp" / "gen_argfile.py").write_text("print(2)\n", encoding="utf-8")

    data = snapshots.list_changes(rid, ws)
    paths = {f["path"] for f in data["files"]}
    assert paths == {"src/Main.java"}, paths
    assert data["summary"]["count"] == 1, data["summary"]

    # include_scratch=True 时仍能看到（调试/内部用）
    full = snapshots.list_changes(rid, ws, include_scratch=True)
    assert {f["path"] for f in full["files"]} == {"src/Main.java", ".trace/tmp/gen_argfile.py"}

    # 「全部回滚」默认只动用户文件，不去删过程文件（过程文件交给 tempfiles 收尾）
    snapshots.revert(rid, ws)
    assert (ws / ".trace" / "tmp" / "gen_argfile.py").read_text(encoding="utf-8") == "print(2)\n"
    assert (ws / "src" / "Main.java").read_text(encoding="utf-8") == "class Main {}\n"


def test_run_context_has_no_branch_fields() -> None:
    from game_agent.core.context import RunContext
    for name in ("branch", "base_branch", "base_commit"):
        assert name not in RunContext.__dataclass_fields__
