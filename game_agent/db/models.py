"""数据模型（设计 §9）。MySQL 8.0+ 与 SQLite 共用同一套模型（SQLAlchemy 2 async）。

表清单：
  身份: users / auth_sessions / api_tokens
  租户: teams / team_members
  项目: projects / project_members / project_locks
  对话: sessions / messages
  执行: runs / tasks / work_orders
  产出: artifacts / approvals / comments
  遥测: events / audit_logs / usage_ledger / llm_calls
  配置: model_profiles / asset_providers / agents / voice_profiles / settings
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    BigInteger,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# MySQL 下 Text 只有 64KB，装不下一次完整 LLM 请求（含工具定义常超 100KB）→ 用 LONGTEXT。
# SQLite（测试/单机）没有该类型，用普通 TEXT。
from sqlalchemy.dialects.mysql import LONGTEXT  # noqa: E402

_LongText = Text().with_variant(LONGTEXT(), "mysql")


class UTCDateTime(TypeDecorator):
    """MySQL DATETIME 不存时区：写入时丢 tzinfo（值已是 UTC），读回为裸时间。
    读回统一补 UTC 标记，API 序列化才会带 +00:00，
    前端 new Date 才不会把 UTC 当本地时间（导致「已进行 480 分」差 8 小时）。"""
    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


# ---------------- 身份 ----------------
class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # 个人头像：/uploads/avatars/xxx.png 或外链；空 = 用首字母配色头像
    avatar: Mapped[str | None] = mapped_column(String(500), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    oidc_sub: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # 个人外观偏好：主题/主色/背景图/字体颜色/字号（appearance 设置页写这里）
    prefs_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    teams: Mapped[list["TeamMember"]] = relationship(
        back_populates="user", foreign_keys="TeamMember.user_id"
    )
    auth_sessions: Mapped[list["AuthSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    api_tokens: Mapped[list["ApiToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="auth_sessions")


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="api_tokens")


# ---------------- 租户 ----------------
class Team(Base):
    __tablename__ = "teams"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    budget_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    members: Mapped[list["TeamMember"]] = relationship(back_populates="team", cascade="all, delete-orphan")


class TeamMember(Base):
    __tablename__ = "team_members"
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # owner|admin|developer|viewer
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    team: Mapped[Team] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="teams", foreign_keys=[user_id])

    __table_args__ = (CheckConstraint("role IN ('owner','admin','developer','viewer')", name="ck_tm_role"),)


# ---------------- 项目 ----------------
class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)  # PROJECTS_ROOT 下的相对路径
    git_remote: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_branch: Mapped[str] = mapped_column(String(80), default="main", nullable=False)
    git_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    project_type: Mapped[str] = mapped_column(String(20), default="python", nullable=False)
    python_bin: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_point: Mapped[str | None] = mapped_column(Text, nullable=True)
    commands_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    budget_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    branch_ttl_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (Index("ix_projects_team_slug", "team_id", "slug"),)


class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # admin|developer|viewer

    __table_args__ = (CheckConstraint("role IN ('admin','developer','viewer')", name="ck_pm_role"),)


class ProjectLock(Base):
    __tablename__ = "project_locks"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    mode: Mapped[str] = mapped_column(String(12), nullable=False)  # exclusive|shared
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    holder_worker: Mapped[str] = mapped_column(String(80), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


# ---------------- 项目记忆 ----------------
class ProjectMemory(Base):
    """项目级长期记忆：run 结束由模型融合成结构化条目（auto），或成员手动补充（manual）。
    后续编排只注入标题索引（渐进式披露），正文由 memory_read 工具按需读取。"""
    __tablename__ = "project_memories"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"),
                                                  nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), default="auto", nullable=False,
                                      server_default="auto")  # auto|manual
    # 结构化字段：标题/标签给索引与界面用；正文仍在 content（老数据 title 为空时从正文首行推导）
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    tags: Mapped[str | None] = mapped_column(String(240), nullable=True)
    # 会话与更新时间：融合时保留每条记忆来自哪次对话，界面按更新时间倒序
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


# ---------------- MCP 服务器（项目级可配置界面管理） ----------------
class McpServer(Base):
    """项目声明的 MCP 服务器：启动 run 时动态注册为 mcp_<name>_<tool> 工具。
    transport=stdio 走 command+args+env；transport=http 走 url（streamable）。"""
    __tablename__ = "mcp_servers"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"),
                                                  nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)  # 工具前缀，限 [a-zA-Z0-9_-]
    transport: Mapped[str] = mapped_column(String(8), default="stdio", nullable=False,
                                           server_default="stdio")  # stdio|http
    command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    args_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    env_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False,
                                          server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


# ---------------- 对话 ----------------
class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False,
                                        server_default="active")  # active|archived|deleted
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # 会话挂载的技能名列表（渐进式披露：清单注入上下文，正文按需 load_skill）。
    # None = 未指定（跟随项目 .trace/skills 默认行为）；[] = 明确不挂任何技能。
    skills_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 会话权限作用域：workspace=仅工作区（默认）；full=全部权限（可访问工作区外）。
    # 新会话初始化时取设置里的默认值，之后每个会话可单独改。
    permission_scope: Mapped[str] = mapped_column(
        String(16), default="workspace", nullable=False, server_default="workspace")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    last_active: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    author_type: Mapped[str] = mapped_column(String(12), nullable=False)  # user|agent|system
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    agent_key: Mapped[str | None] = mapped_column(String(20), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (Index("ix_messages_session_created", "session_id", "created_at"),)


# ---------------- 执行 ----------------
class RunQuestion(Base):
    """组长「拿不准就问」：运行过程中弹给用户的待答问题（如业务口径 A/B 不确定）。

    为什么落库而不是只发消息：问题要能跨刷新/重连存活，答案要能被审计
    （谁在什么时候选了哪个方案），且回答后要能重新入队继续同一个会话。
    """
    __tablename__ = "run_questions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(20), default="main", nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)  # [{label, detail?}]
    allow_text: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False, index=True)
    # pending|answered|skipped
    answer_index: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-based，-1=自由文本/跳过
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    triggered_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    trigger_msg_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    run_type: Mapped[str] = mapped_column(String(12), default="modify", nullable=False)  # modify|analyze
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False, index=True)
    # queued|claimed|running|blocked|orphaned|succeeded|failed|cancelled
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    branch: Mapped[str | None] = mapped_column(String(120), nullable=True)
    base_branch: Mapped[str | None] = mapped_column(String(120), nullable=True)
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lock_fencing: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 计划机制：组长维护的待办清单（[{id, content, status, note}]），DB 为权威源，UI 实时渲染勾选
    plan_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 运行能力开关（test/web/media）：前端「运行选项」勾选，默认全开；关掉的工具会被裁剪
    options_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    change_report_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    final_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    has_placeholder: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # plan|implement|art|voice|test|fix
    agent_key: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)
    # pending|running|done|failed|cancelled
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    spec_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    checklist_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    art_direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    asset_plan_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


# ---------------- 产出 ----------------
class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True,
                                                     index=True)  # 导入素材可无 run
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    asset_kind: Mapped[str] = mapped_column(String(12), nullable=False)  # image|voice|sfx|music
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)  # 工作区内相对路径
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    params_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_artifacts_run_slug", "run_id", "task_id", "slug"),
    )


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # merge|budget|force_unlock|danger_command
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)  # pending|approved|rejected|expired
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # run|task|artifact|work_order|diff_line
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    mentions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


# ---------------- 遥测 ----------------
class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("project_id", "seq", name="uq_events_project_seq"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)  # message_delta|tool_call|status|...
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_events_run_seq", "run_id", "seq"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=True, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    target: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    agent_key: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # tokens|images|audio
    amount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class LlmCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=True, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    call_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    agent_key: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 命中前缀缓存的 prompt token 数（DeepSeek prompt_cache_hit / OpenAI cached_tokens）
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 供应商是否上报了缓存字段（0/1）：没上报时命中率应显示"未知"而不是 0%
    cache_reported: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 含重试的总耗时
    # 这次往返是正文产出（agent）还是压缩历史的摘要调用（summarize）。
    # 摘要调用的 prompt 只是「被压的那段历史」，不能当成「模型看到的上下文」去算占用；
    # 用它而不是 LLM_TRANSCRIPT 关联，是因为 llm_transcripts 只保留 14 天，过期后仍然要对得上。
    purpose: Mapped[str] = mapped_column(String(16), default="agent", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class LlmTranscript(Base):
    """一次 LLM 往返的完整现场：压缩后真正发给模型的内容 + 模型返回的内容。

    为什么要单独一张表：llm_calls 只有计数，排查「模型为什么这么答／为什么原地打转」
    必须看到发出去的原文（尤其上下文压缩丢弃了什么）。大字段走 LONGTEXT，
    列表查询只取轻列，别把 blob 拖进排序（同 messages 那次 1038 Out of sort memory）。
    """
    __tablename__ = "llm_transcripts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=True, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True)
    # 对应 llm_calls.call_id（同一轮往返的两条记录互相对齐）
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    agent_key: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    round_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # agent 循环内第几轮
    purpose: Mapped[str] = mapped_column(String(16), default="agent", nullable=False)  # agent|summarize
    compressed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 本轮请求是否带压缩说明
    dropped_msgs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 被丢弃的历史条数
    req_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resp_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # 分类 token 占比等
    request_json: Mapped[str] = mapped_column(_LongText, nullable=False)  # {"messages":[...],"tools":[...]}
    response_json: Mapped[str] = mapped_column(_LongText, nullable=False)  # {text,reasoning,tool_calls,usage}
    truncated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 原文过大被截断存储
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (Index("ix_llm_transcripts_run_created", "run_id", "created_at"),)


# ---------------- 配置 ----------------
class LlmProvider(Base):
    """LLM 供应商：base_url / api_key / UA 只在这里配一次，对话档位只选供应商 + 模型。

    之前每个 ModelProfile 各存一份 base_url+key，改密钥要挨个档位改、还容易漏改
    导致部分角色悄悄回退到环境变量。供应商独立成表后档位只需引用 provider_id。
    """
    __tablename__ = "llm_providers"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    # 协议：openai=兼容 /v1/chat/completions | anthropic | google | ollama
    protocol: Mapped[str] = mapped_column(String(20), default="openai", nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 部分站点前置 WAF 会按 UA 拦脚本（见 GA_LLM_USER_AGENT）；这里可按供应商覆盖
    user_agent: Mapped[str | None] = mapped_column(String(160), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # /models 拉取结果缓存，供档位下拉直接选
    models_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (Index("ix_llm_providers_team_key", "team_id", "key"),)


class ModelProfile(Base):
    __tablename__ = "model_profiles"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    # 档位类型：llm=对话模型 | image=图像生成模型 | audio=音频生成模型
    kind: Mapped[str] = mapped_column(String(10), default="llm", nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)  # openai|anthropic|google|ollama|comfyui|openai_image|openai_tts
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    # 对话档位优先用 provider_id（供应商集中配）；base_url 仅作 inline 覆盖
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("llm_providers.id", ondelete="SET NULL"), nullable=True, index=True)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    params_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (Index("ix_model_profiles_team_key", "team_id", "key"),)


class AssetProvider(Base):
    __tablename__ = "asset_providers"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    driver_key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # image|voice|sfx|music
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class Agent(Base):
    """团队级智能体配置（prompt / 绑模型 / 参数）。设计 §4.1：配置粒度为团队级。"""
    __tablename__ = "agents"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(20), nullable=False)  # planner|artist|voice
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model_profile_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    params_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (Index("ix_agents_team_key", "team_id", "key"),)


class VoiceProfile(Base):
    __tablename__ = "voice_profiles"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    params_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)


class ImagePreset(Base):
    """生图工作台参数预设：工作流方式 / 尺寸 / LoRA / 强度 / 提示词增强等一套出图配方。"""
    __tablename__ = "image_presets"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    params_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 预设参考图（DATA_DIR/objects/imagegen/presets 下的文件名），None=没有样图
    image_file: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)

    __table_args__ = (Index("ix_image_presets_team_key", "team_id", "key"),)


class Settings(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, nullable=False)
