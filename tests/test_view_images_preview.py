"""view_images 合图落盘 + 结果带缩略图路径的回归测试。

2026-11-02 用户截图复现：view_images 把多张图拼成一张联系表后，模型看得到（data-url），
但前端轨迹里既没有缩略图也没有「截图已保存：xxx」这种可解析路径——因为合图从来没落盘，
结果文本里也只有「第1格=…」的原始图路径。前端 ShotThumbs 靠「合图预览：<path>」取图。

这里锁住：合图确实落到 .trace/previews/，且结果文本里带着这个工作区相对路径。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from game_agent.agents.tools import ToolContext  # noqa: E402
from game_agent.agents.tools.service import view_images  # noqa: E402
from game_agent.core.context import RunContext  # noqa: E402


def _ctx(workspace: Path) -> ToolContext:
    rc = RunContext(run_id=str(uuid.uuid4()), team_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
                    triggered_by=str(uuid.uuid4()), run_type="modify", worker_id="test",
                    repo=None, bus=None, locks=None, store=None,  # type: ignore[arg-type]
                    user_request="验收美术")
    return ToolContext(run_ctx=rc, workspace=workspace, role="main", task_id="t")


def test_view_images_saves_sheet_and_reports_path(tmp_path: Path) -> None:
    shots = tmp_path / "target" / "shots"
    shots.mkdir(parents=True)
    for i, color in enumerate([(200, 30, 30), (30, 120, 200)]):
        Image.new("RGB", (64, 64), color).save(shots / f"a{i}.png")

    ctx = _ctx(tmp_path)
    result = __import__("asyncio").run(
        view_images({"paths": ["target/shots/a0.png", "target/shots/a1.png"]}, ctx)
    )

    # 结果文本里带「合图预览：<相对路径>」，前端靠它取缩略图
    assert "合图预览：" in result.text
    rel = result.text.split("合图预览：", 1)[1].split("）", 1)[0].strip()
    rel = rel.strip("`")
    assert rel.startswith(".trace/previews/") and rel.endswith(".jpg")

    saved = tmp_path / rel
    assert saved.is_file() and saved.stat().st_size > 0

    # 合图仍以 data-url 回灌模型（视觉输入不丢）
    assert result.images and result.images[0].startswith("data:image/")


def test_run_raw_serves_saved_sheet(tmp_path: Path, session_factory, monkeypatch) -> None:
    """端到端：合图落盘后，/runs/{id}/raw/<path> 能把它当图片直出（前端 <img> 就取这个地址）。

    这个版本没有登录：请求不带凭证，服务端用本机唯一身份 —— fixture 必须把 run 挂到
    该身份的 team 下，否则 /raw 会因为 team 不一致返回 404。
    """
    import asyncio

    import httpx
    from fastapi import FastAPI

    from game_agent.api.routers import runs as runs_router
    from game_agent.api.state import state
    from game_agent.auth.local import ensure_local_identity
    from game_agent.config import PROJECTS_ROOT
    from game_agent.db.models import Project, Run, Session as DbSession

    async def seed():
        key = uuid.uuid4().hex
        async with session_factory() as db:
            user, team = await ensure_local_identity(db)
            workspace = PROJECTS_ROOT / f"sheet-ws-{key}"
            project = Project(name="sheet", slug=f"sheet-p-{key}", team_id=team.id,
                              repo_path=workspace.name, created_by=user.id)
            db.add(project)
            await db.flush()
            sess = DbSession(team_id=team.id, project_id=project.id, created_by=user.id)
            db.add(sess)
            await db.flush()
            run = Run(team_id=team.id, project_id=project.id, session_id=sess.id,
                      triggered_by=user.id, run_type="modify")
            db.add(run)
            await db.commit()
            return str(run.id), workspace

    run_id, workspace = asyncio.run(seed())
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, "session_factory", session_factory)

    # 在项目工作区内造一张图（等价 view_images 落盘产物）
    previews = workspace / ".trace" / "previews"
    previews.mkdir(parents=True)
    Image.new("RGB", (32, 32), (10, 200, 10)).save(previews / "sheet-x.jpg")

    app = FastAPI()
    app.include_router(runs_router.router)

    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(f"/api/runs/{run_id}/raw/.trace/previews/sheet-x.jpg")

    resp = asyncio.run(send())
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/")
    assert resp.content[:2] == b"\xff\xd8"  # JPEG magic：说明是原样 JPEG 字节

