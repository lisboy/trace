from __future__ import annotations

import asyncio
import io
import os
import re
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from sqlalchemy import func, select

from game_agent.api.routers import sessions
from game_agent.api.state import state
from game_agent.auth.service import AuthService
from game_agent.config import PROJECTS_ROOT
from game_agent.core.context import RequestContext
from game_agent.db.models import Message, Project, Run, Session, Team, TeamMember, User


@pytest.fixture
def api(session_factory, monkeypatch):
    auth = AuthService(session_factory)

    async def seed():
        key = uuid4().hex
        async with session_factory() as db:
            team = Team(name="Uploads", slug=f"uploads-{key}")
            foreign_team = Team(name="Foreign", slug=f"foreign-{key}")
            users = {role: User(email=f"{role}-{key}@example.com", display_name=role)
                     for role in ("owner", "developer", "viewer", "outsider")}
            db.add_all([team, foreign_team, *users.values()])
            await db.flush()
            for role, user in users.items():
                db.add(TeamMember(user_id=user.id,
                                  team_id=foreign_team.id if role == "outsider" else team.id,
                                  role="owner" if role == "outsider" else role))
            projects = {}
            for name in ("project", "other", "foreign"):
                projects[name] = Project(
                    name=name, slug=f"{name}-{key}", repo_path=f"uploads-{name}-{key}",
                    team_id=foreign_team.id if name == "foreign" else team.id,
                    created_by=users["owner"].id,
                )
            db.add_all(projects.values())
            await db.flush()
            existing = {
                "own": Session(team_id=team.id, project_id=projects["project"].id),
                "other_project": Session(team_id=team.id, project_id=projects["other"].id),
                "other_team": Session(team_id=foreign_team.id, project_id=projects["foreign"].id),
                "wrong_team": Session(team_id=foreign_team.id, project_id=projects["project"].id),
                "deleted": Session(team_id=team.id, project_id=projects["project"].id,
                                   status="deleted"),
            }
            db.add_all(existing.values())
            await db.commit()
            ids = {name: str(p.id) for name, p in projects.items()}
            workspaces = {name: PROJECTS_ROOT / p.repo_path for name, p in projects.items()}
            session_ids = {name: str(s.id) for name, s in existing.items()}
            user_ids = {role: str(u.id) for role, u in users.items()}
        headers = {}
        for role, uid in user_ids.items():
            token = await auth.create_session(uid, None, None)
            headers[role] = {"Cookie": f"ga_session={token}"}
        return ids, workspaces, session_ids, headers

    ids, workspaces, session_ids, headers = asyncio.run(seed())
    for workspace in workspaces.values():
        workspace.mkdir(parents=True)
        assert workspace.resolve().is_relative_to(PROJECTS_ROOT.resolve())
    queue = SimpleNamespace(enqueue=AsyncMock())
    monkeypatch.setattr(state, "session_factory", session_factory)
    monkeypatch.setattr(state, "auth", auth)
    monkeypatch.setattr(state, "services", SimpleNamespace(queue=queue))
    app = FastAPI()
    app.include_router(sessions.router)
    return SimpleNamespace(app=app, ids=ids, ws=workspaces["project"], workspaces=workspaces,
                           session_ids=session_ids, headers=headers, queue=queue,
                           session_factory=session_factory)


def request(api, method, path, *, role="owner", **kwargs):
    async def send():
        headers = dict(api.headers.get(role, {}))
        headers.update(kwargs.pop("headers", {}))
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers, **kwargs)

    return asyncio.run(send())


def upload(api, data=b"attachment", name="data.txt", *, role="owner", project="project"):
    return request(api, "POST", f"/api/projects/{api.ids[project]}/uploads", role=role,
                   files={"file": (name, data, "application/octet-stream")})


def chat(api, *, role="owner", project="project", headers=None, **body):
    return request(api, "POST", f"/api/projects/{api.ids[project]}/chat", role=role,
                   headers=headers or {}, json={"content": "", **body})


def counts(api):
    async def read():
        async with api.session_factory() as db:
            return tuple([await db.scalar(select(func.count()).select_from(model))
                          for model in (Session, Message, Run)])

    return asyncio.run(read())


def directory_link(link: Path, target: Path):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True)
        assert result.returncode == 0, result.stderr


def file_link(link: Path, target: Path):
    try:
        link.symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows file symlinks require Developer Mode or symlink privilege")
        raise


@pytest.mark.parametrize("name,kind", [("数据分析.xlsx", "excel"), ("需求说明.docx", "word"),
                                      ("说明.PDF", "pdf"), ("日志.txt", "text"),
                                      ("binary.bin", "file")])
def test_upload_preserves_bytes_and_chinese_names(api, name, kind):
    data = bytes(range(256)) * 1024
    response = upload(api, data, name)
    assert response.status_code == 200, response.text
    item = response.json()
    dest = api.ws / item["path"]
    assert dest.read_bytes() == data
    assert dest.resolve().is_relative_to((api.ws / ".trace/uploads").resolve())
    assert item["size"] == len(data)
    assert item["name"] == name
    assert item["kind"] == kind
    assert re.fullmatch(r"[0-9a-f]{32}-.+", dest.name)
    assert UUID(dest.name[:32]).version == 4
    assert not (api.ws / ".git").exists()


@pytest.mark.parametrize("size,status", [(0, 400), (30 * 1024 * 1024, 200),
                                       (30 * 1024 * 1024 + 1, 413)])
def test_upload_size_boundaries(api, size, status):
    data = b"x" * size
    response = upload(api, data)
    assert response.status_code == status, response.text
    if status == 200:
        assert (api.ws / response.json()["path"]).read_bytes() == data
    else:
        assert not (api.ws / ".trace/uploads").exists()


def test_upload_sanitizes_display_name(api):
    response = upload(api, name="../中文:文件?.txt")
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "中文_文件_.txt"
    assert (api.ws / response.json()["path"]).is_file()


def test_long_chinese_name_fits_filesystem_and_display_limits(api):
    response = upload(api, name="测" * 100 + ".xlsx")
    assert response.status_code == 200, response.text
    item = response.json()
    assert len(item["name"]) <= 120
    assert len(Path(item["path"]).name.encode("utf-8")) <= 255
    assert item["kind"] == "excel"
    assert (api.ws / item["path"]).read_bytes() == b"attachment"


@pytest.mark.parametrize("case,status", [("success", None), ("empty", 400),
                                       ("oversized", 413), ("foreign", 404),
                                       ("git_failure", 500)])
def test_upload_stream_lifecycle_and_authorization_order(api, case, status):
    threads = []

    class TrackedStream(io.BytesIO):
        def read(self, size=-1):
            threads.append(threading.get_ident())
            return super().read(size)

    data = b"" if case == "empty" else b"x"
    if case == "oversized":
        data *= sessions.MAX_UPLOAD_BYTES + 1
    if case == "git_failure":
        (api.ws / ".git").mkdir()
        (api.ws / ".git/info").write_bytes(b"blocked")
    stream = TrackedStream(data)
    main_thread = threading.get_ident()

    async def invoke():
        async with api.session_factory() as db:
            project = await db.get(Project, UUID(api.ids["project"]))
            ctx = RequestContext(user_id=str(project.created_by), team_id=str(project.team_id),
                                 role="owner", project_ids=list(api.ids.values()))
        pid = api.ids["foreign"] if case == "foreign" else api.ids["project"]
        file = UploadFile(file=stream, filename="data.txt")
        if status is None:
            result = await sessions.upload_project_file(pid, file, ctx)
            assert (api.ws / result["path"]).read_bytes() == data
        else:
            with pytest.raises(HTTPException) as exc:
                await sessions.upload_project_file(pid, file, ctx)
            assert exc.value.status_code == status

    asyncio.run(invoke())
    assert stream.closed
    if case == "foreign":
        assert not threads
        assert not (api.workspaces["foreign"] / ".trace").exists()
    else:
        assert threads and all(t != main_thread for t in threads)


def test_chat_metadata_roundtrip_uses_server_facts(api):
    uploaded = upload(api, b"a,b\n1,2\n", "原始数据.csv").json()
    body = {**uploaded, "size": 999999, "name": "../伪造\x00\u202e" + "名" * 180}
    response = chat(api, files=[body, {**body, "size": -12}], analyze=True,
                    session_id=api.session_ids["own"])
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["session_id"] == api.session_ids["own"]
    api.queue.enqueue.assert_awaited_once_with(run["run_id"], priority=0)
    messages = request(api, "GET", f"/api/sessions/{run['session_id']}/messages").json()
    item = messages[0]["meta"]["files"][0]
    assert len(messages[0]["meta"]["files"]) == 1
    assert item["path"] == uploaded["path"]
    assert item["size"] == len(b"a,b\n1,2\n")
    assert item["name"] == Path(uploaded["path"]).name
    assert item["original_name"].startswith("伪造__")
    assert len(item["original_name"]) <= 120
    assert messages[0]["content"] == ""

    async def verify_db():
        async with api.session_factory() as db:
            stored_run = await db.get(Run, UUID(run["run_id"]))
            message = await db.get(Message, stored_run.trigger_msg_id)
            assert stored_run.project_id == UUID(api.ids["project"])
            assert stored_run.run_type == "analyze"
            assert message.meta_json == messages[0]["meta"]

    asyncio.run(verify_db())
    assert request(api, "GET", f"/api/sessions/{run['session_id']}/messages",
                   role="outsider").status_code == 404
    assert request(api, "GET", f"/api/sessions/{run['session_id']}/messages",
                   role="viewer").status_code == 200


def test_attachment_only_new_session_and_idempotency(api):
    item = upload(api).json()
    first = chat(api, files=[item], headers={"Idempotency-Key": "upload-retry"})
    assert first.status_code == 200, first.text
    before = counts(api)
    second = chat(api, files=[item], headers={"Idempotency-Key": "upload-retry"})
    assert second.status_code == 200, second.text
    assert second.json()["run_id"] == first.json()["run_id"]
    assert second.json()["dup"] is True
    assert counts(api) == before
    api.queue.enqueue.assert_awaited_once()


def test_chat_rejects_file_overflow_without_writes(api):
    item = upload(api).json()
    before = counts(api)
    response = chat(api, files=[item] * (sessions.MAX_FILES + 1))
    assert response.status_code == 400, response.text
    assert counts(api) == before
    api.queue.enqueue.assert_not_awaited()


@pytest.mark.parametrize("path", [
    "../secret.txt", "/.trace/uploads/a.txt", "C:/Windows/a.txt", "C:relative.txt",
    ".trace/uploads-sibling/a.txt", ".trace/uploads/../secret.txt",
    ".trace/uploads\\..\\secret.txt", ".trace/uploads/./a.txt",
    ".trace/uploads//a.txt", ".trace/uploads/", ".trace/uploads/a.txt:stream",
    ".trace/uploads/C:/a.txt", ".trace/uploads/a\x00.txt", ".trace/uploads/a\x1f.txt",
    ".trace/uploads/a\x7f.txt", ".trace/uploads/a\u202e.txt", ".trace/uploads/a.txt.",
    ".trace/uploads/a.txt ", ".trace/uploads/NUL", ".trace/uploads/a?.txt",
])
def test_chat_rejects_unsafe_paths(api, path):
    before = counts(api)
    response = chat(api, content="parse", files=[{"path": path}])
    assert response.status_code == 400, response.text
    assert counts(api) == before
    api.queue.enqueue.assert_not_awaited()
    assert not (api.ws / ".trace").exists()


@pytest.mark.parametrize("entry,status", [("missing", 400), ("directory", 400),
                                        ("empty", 400), ("oversized", 413)])
def test_chat_requires_existing_nonempty_regular_file(api, entry, status):
    dest = api.ws / ".trace/uploads/data.txt"
    if entry != "missing":
        dest.parent.mkdir(parents=True)
        if entry == "directory":
            dest.mkdir()
        else:
            with dest.open("wb") as fh:
                if entry == "oversized":
                    fh.truncate(sessions.MAX_UPLOAD_BYTES + 1)
    response = chat(api, files=[{"path": ".trace/uploads/data.txt", "size": 1}])
    assert response.status_code == status, response.text
    api.queue.enqueue.assert_not_awaited()
    if entry == "missing":
        assert not dest.parent.exists()


def test_chat_normalizes_backslashes_and_derives_missing_name(api):
    item = upload(api).json()
    response = chat(api, files=[{"path": item["path"].replace("/", "\\")}])
    assert response.status_code == 200, response.text
    messages = request(api, "GET", f"/api/sessions/{response.json()['session_id']}/messages")
    attached = messages.json()[0]["meta"]["files"][0]
    assert attached["path"] == item["path"]
    assert attached["name"] == Path(item["path"]).name
    assert attached["size"] == item["size"]


@pytest.mark.parametrize("component", [".trace", ".trace/uploads"])
def test_upload_and_chat_reject_directory_link_escape(api, component):
    outside = api.ws.with_name(api.ws.name + "-sibling")
    outside.mkdir()
    link = api.ws / component
    link.parent.mkdir(parents=True, exist_ok=True)
    target_uploads = outside / "uploads" if component == ".trace" else outside
    target_uploads.mkdir(exist_ok=True)
    (target_uploads / "secret.txt").write_bytes(b"private")
    directory_link(link, outside)
    before = sorted(p.relative_to(outside).as_posix() for p in outside.rglob("*"))
    assert upload(api).status_code == 400
    response = chat(api, files=[{"path": ".trace/uploads/secret.txt"}])
    assert response.status_code == 400, response.text
    assert (target_uploads / "secret.txt").read_bytes() == b"private"
    assert sorted(p.relative_to(outside).as_posix() for p in outside.rglob("*")) == before
    api.queue.enqueue.assert_not_awaited()


@pytest.mark.parametrize("relative", [".trace/uploads", ".git", ".git/info"])
def test_upload_rejects_internal_directory_redirection(api, relative):
    link = api.ws / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    target = link.with_name(link.name + "-private")
    target.mkdir()
    (target / "exclude").write_bytes(b"preserve")
    directory_link(link, target)
    response = upload(api)
    assert response.status_code == 400, response.text
    assert (target / "exclude").read_bytes() == b"preserve"
    assert list(target.iterdir()) == [target / "exclude"]


def test_chat_rejects_nested_junction_to_sibling_uploads_prefix(api):
    updir = api.ws / ".trace/uploads"
    sibling = api.ws / ".trace/uploads-private"
    updir.mkdir(parents=True)
    sibling.mkdir()
    (sibling / "secret.txt").write_bytes(b"private")
    directory_link(updir / "linked", sibling)
    response = chat(api, files=[{"path": ".trace/uploads/linked/secret.txt"}])
    assert response.status_code == 400, response.text
    assert (sibling / "secret.txt").read_bytes() == b"private"


def test_chat_rejects_file_symlink_escape(api):
    updir = api.ws / ".trace/uploads"
    sibling = api.ws / ".trace/uploads-private"
    updir.mkdir(parents=True)
    sibling.mkdir()
    target = sibling / "secret.txt"
    target.write_bytes(b"private")
    file_link(updir / "alias.txt", target)
    response = chat(api, files=[{"path": ".trace/uploads/alias.txt"}])
    assert response.status_code == 400, response.text
    assert target.read_bytes() == b"private"


def test_chat_rejects_hardlink_to_foreign_file(api):
    updir = api.ws / ".trace/uploads"
    updir.mkdir(parents=True)
    target = api.workspaces["foreign"] / "secret.txt"
    target.write_bytes(b"private")
    (updir / "alias.txt").hardlink_to(target)
    response = chat(api, files=[{"path": ".trace/uploads/alias.txt"}])
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("role,status", [("anonymous", 401), ("viewer", 403),
                                       ("outsider", 403)])
def test_permissions_precede_file_validation_and_writes(api, role, status):
    before = counts(api)
    assert upload(api, data=b"", role=role).status_code == status
    response = chat(api, role=role, files=[{"path": "../secret"}])
    assert response.status_code == status, response.text
    assert not (api.ws / ".trace").exists()
    assert counts(api) == before
    api.queue.enqueue.assert_not_awaited()


@pytest.mark.parametrize("role", ["owner", "developer"])
def test_authorized_team_roles_can_upload_and_chat(api, role):
    response = upload(api, role=role)
    assert response.status_code == 200, response.text
    response = chat(api, role=role, files=[response.json()])
    assert response.status_code == 200, response.text


def test_foreign_project_is_forbidden(api):
    assert upload(api, project="foreign").status_code == 403
    assert chat(api, project="foreign", content="hello").status_code == 403
    assert not (api.workspaces["foreign"] / ".trace").exists()
    api.queue.enqueue.assert_not_awaited()


@pytest.mark.parametrize("which", ["other_project", "other_team", "wrong_team", "deleted",
                                  "missing", "malformed", "empty"])
def test_foreign_or_invalid_session_rejected_before_run_and_idempotency(api, which):
    first = chat(api, content="valid", headers={"Idempotency-Key": "same-key"})
    assert first.status_code == 200, first.text
    api.queue.enqueue.reset_mock()
    before = counts(api)
    sid = {"missing": str(uuid4()), "malformed": "not-a-uuid", "empty": ""}.get(
        which, api.session_ids.get(which))
    response = chat(api, content="forbidden", session_id=sid,
                    headers={"Idempotency-Key": "same-key"}, files=[{"path": "../bad"}])
    assert response.status_code == 404, response.text
    assert counts(api) == before
    api.queue.enqueue.assert_not_awaited()


def test_git_exclude_preserves_all_bytes_and_other_files(api):
    info = api.ws / ".git/info"
    info.mkdir(parents=True)
    content = b"# existing\r\nnon-utf8-\xff\nkeep-without-final-newline"
    exclude = info / "exclude"
    exclude.write_bytes(content)
    config = api.ws / ".git/config"
    config.write_bytes(b"[core]\nrepositoryformatversion = 0\n")
    ignore = api.ws / ".gitignore"
    ignore.write_bytes(b"keep-ignore\r\n")
    assert upload(api).status_code == 200
    assert upload(api).status_code == 200
    assert exclude.read_bytes() == content + b"\n.trace/uploads/\n"
    assert config.read_bytes() == b"[core]\nrepositoryformatversion = 0\n"
    assert ignore.read_bytes() == b"keep-ignore\r\n"


@pytest.mark.parametrize("rule", [b".trace/uploads/", b"/.trace/uploads/"])
def test_git_exclude_existing_rule_is_untouched(api, rule):
    exclude = api.ws / ".git/info/exclude"
    exclude.parent.mkdir(parents=True)
    original = b"# custom\r\n" + rule
    exclude.write_bytes(original)
    assert upload(api).status_code == 200
    assert exclude.read_bytes() == original


def test_git_exclude_failure_is_not_silenced(api):
    git = api.ws / ".git"
    git.mkdir()
    (git / "info").write_bytes(b"not-a-directory")
    response = upload(api)
    assert response.status_code == 500, response.text
    assert (git / "info").read_bytes() == b"not-a-directory"
    assert not (api.ws / ".trace/uploads").exists()


def test_git_info_junction_escape_is_rejected(api):
    git = api.ws / ".git"
    git.mkdir()
    outside = api.ws.with_name(api.ws.name + "-git")
    outside.mkdir()
    (outside / "exclude").write_bytes(b"private")
    directory_link(git / "info", outside)
    response = upload(api)
    assert response.status_code == 400, response.text
    assert (outside / "exclude").read_bytes() == b"private"
    assert not (api.ws / ".trace/uploads").exists()


def test_git_exclude_symlink_escape_is_rejected(api):
    info = api.ws / ".git/info"
    info.mkdir(parents=True)
    outside = api.workspaces["foreign"] / "exclude"
    outside.write_bytes(b"private")
    file_link(info / "exclude", outside)
    assert upload(api).status_code == 400
    assert outside.read_bytes() == b"private"


def test_safe_local_gitfile_is_supported(api):
    gitdir = api.ws / "git-private"
    gitdir.mkdir()
    (api.ws / ".git").write_text("gitdir: git-private\n", encoding="utf-8")
    assert upload(api).status_code == 200
    assert (gitdir / "info/exclude").read_bytes() == b".trace/uploads/\n"
    assert (api.ws / ".git").read_text(encoding="utf-8") == "gitdir: git-private\n"


@pytest.mark.parametrize("external", [True, False])
def test_worktree_gitfile_never_modifies_external_or_shared_metadata(api, external):
    shared = api.workspaces["foreign"] / "git-common"
    shared.mkdir()
    (shared / "config").write_bytes(b"shared-config")
    (shared / "info").mkdir()
    (shared / "info/exclude").write_bytes(b"shared-exclude")
    gitdir = shared if external else api.ws / "git-private"
    if not external:
        gitdir.mkdir()
        (gitdir / "commondir").write_text(str(shared), encoding="utf-8")
    (api.ws / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="utf-8")
    response = upload(api)
    assert response.status_code == 400, response.text
    assert (shared / "config").read_bytes() == b"shared-config"
    assert (shared / "info/exclude").read_bytes() == b"shared-exclude"
    assert not (api.ws / ".trace/uploads").exists()
