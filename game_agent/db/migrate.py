"""数据库初始化 / 迁移。

lite 模式与快速起步用 ``create_all``（设计 §2.1 允许）；
server 模式生产建议改用 Alembic（``game_agent/db/alembic`` 预留）。
这里统一暴露 ``init_db()`` 供启动时调用。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text

from game_agent.config import DATABASE_URL, is_sqlite
from game_agent.db.models import Base
from game_agent.db.session import engine
from game_agent.logging_setup import get_logger

log = get_logger("db.migrate")


async def init_db() -> None:
    # 创建所有表（幂等）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_columns()
    # 轻量版本记录（Alembic 未启用时的兼容位）
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version ("
                    "version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
        except Exception:  # noqa: BLE001 - 幂等兜底
            pass
    log.info("database initialized", extra={"url": _mask(DATABASE_URL)})
    await _migrate_chat_images()


async def _migrate_chat_images() -> None:
    """把老数据里存在工作区 ``.trace/uploads/`` 的聊天附图搬进稳定目录 DATA_DIR/chat_uploads，
    并把消息 meta 里的 image_paths 改写成新标记 ``chat-uploads/<team>/<file>``。

    为什么要在启动时做：老附图存于项目工作区，工作区一旦被回滚/清理/换机器就没了。搬走之后
    附图与工作区解耦，历史消息的图就能长期可看。幂等：新标记跳过；旧文件已不在工作区时按
    文件名在目标目录里再确认一次，仍找不到就保持原样（前端会显示「已失效」，不报错）。
    """
    import shutil

    from game_agent.config import CHAT_UPLOADS_ROOT, PROJECTS_ROOT
    from game_agent.core.chat_images import CHAT_IMG_PREFIX
    from game_agent.db.session import SessionLocal
    from game_agent.db.models import Message, Project

    old_prefix = ".trace/uploads/"
    team_dir_cache: dict[str, object] = {}
    moved = 0

    # 裸 SQL 拿回来的是 32 位无横线 hex（如 5d83c8bf7a8d...），而 ORM/鉴权上下文里的
    # team_id 是带横线的 UUID 字符串。两边直接比会全部 miss（这正是本迁移第一版一条没搬的
    # 原因）。统一去掉横线再比，落盘仍用带横线的 team_id（与 ctx.team_id 一致，鉴权前置
    # 才能对上）。
    def _norm(v: object) -> str:
        return str(v).replace("-", "").lower()

    try:
        async with SessionLocal() as s:
            # 只捞带 image_paths 的消息：JSON 里含关键字的行（MySQL/SQLite 都可用的 LIKE）
            rows = (await s.execute(
                text("SELECT id, session_id, meta_json FROM messages "
                     "WHERE CAST(meta_json AS CHAR) LIKE '%image_paths%'"))).all()
            if not rows:
                return
            # session -> (project_id, repo_path)
            sid_to_proj = {}
            for sid, pid, repo in (await s.execute(
                    text("SELECT s.id, s.project_id, p.repo_path FROM sessions s "
                         "JOIN projects p ON p.id = s.project_id"))).all():
                sid_to_proj[_norm(sid)] = (pid, repo)
            proj_team = {_norm(x.id): str(x.team_id)
                         for x in (await s.execute(select(Project))).scalars().all()}
            for mid, sid, meta_raw in rows:
                try:
                    meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
                except Exception:  # noqa: BLE001
                    continue
                paths = meta.get("image_paths") if isinstance(meta.get("image_paths"), list) else []
                if not paths:
                    continue
                proj_info = sid_to_proj.get(_norm(sid))
                if not proj_info:
                    continue
                pid, repo_path = proj_info
                team_id = proj_team.get(_norm(pid))
                if not team_id:
                    continue
                tdir = team_dir_cache.get(team_id)
                if tdir is None:
                    tdir = CHAT_UPLOADS_ROOT / team_id
                    tdir.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
                    team_dir_cache[team_id] = tdir
                ws = Path(repo_path)
                if not ws.is_absolute():
                    ws = PROJECTS_ROOT / repo_path
                new_paths = []
                changed = False
                for p in paths:
                    ps = str(p).replace("\\", "/")
                    if ps.startswith(CHAT_IMG_PREFIX + "/"):
                        new_paths.append(ps)
                        continue
                    if not ps.startswith(old_prefix):
                        new_paths.append(ps)
                        continue
                    name = ps[len(old_prefix):].rsplit("/", 1)[-1]
                    dst = CHAT_UPLOADS_ROOT / team_id / name  # type: ignore[operator]
                    src = ws / ps
                    try:
                        if not dst.exists():
                            if src.is_file():
                                shutil.copy2(src, dst)
                                moved += 1
                            else:
                                new_paths.append(ps)  # 原文件已丢，保持原样
                                continue
                        new_paths.append(f"{CHAT_IMG_PREFIX}/{team_id}/{name}")
                        changed = True
                    except OSError:
                        new_paths.append(ps)
                if changed:
                    meta["image_paths"] = new_paths
                    await s.execute(
                        text("UPDATE messages SET meta_json = :m WHERE id = :i"),
                        {"m": json.dumps(meta, ensure_ascii=False), "i": mid})
            await s.commit()
        if moved:
            log.info("migrated: chat attachments moved to DATA_DIR/chat_uploads (%d files)", moved)
    except Exception:  # noqa: BLE001 - 迁移失败不影响启动；老路径读取仍可用
        log.exception("聊天附图迁移失败（忽略，下次启动重试）")



async def _ensure_columns() -> None:
    """create_all 不会给已存在的表加列，这里补齐轻量迁移。"""
    stmts = [
        # v0.4: LLM 调用耗时（含重试），用于生成速度统计
        ("llm_calls", "ALTER TABLE llm_calls ADD COLUMN latency_ms INTEGER NOT NULL DEFAULT 0"),
        # v0.5: 会话归档/软删状态
        ("sessions", "ALTER TABLE sessions ADD COLUMN status VARCHAR(16) "
                     "NOT NULL DEFAULT 'active'"),
        # 历史列：git 接管已下线，运行时不再读取，保留只为避免破坏性迁移
        ("projects", "ALTER TABLE projects ADD COLUMN git_enabled BOOLEAN "
                     "NOT NULL DEFAULT FALSE"),
        # 媒体档位：model_profiles.kind 区分对话/图像/音频模型档位（存量档位都是 llm）
        ("model_profiles", "ALTER TABLE model_profiles ADD COLUMN kind VARCHAR(10) "
                           "NOT NULL DEFAULT 'llm'"),
        # 计划机制：runs.plan_json 存组长维护的待办清单（存量行留 NULL，读取时按 [] 处理）
        ("runs", "ALTER TABLE runs ADD COLUMN plan_json JSON"),
        # 用量统计：llm_calls.cached_tokens 记命中前缀缓存的 prompt token（算缓存命中率）
        ("llm_calls", "ALTER TABLE llm_calls ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0"),
        # 供应商是否上报缓存字段：0 = 未上报（命中率显示"未知"，不能假报 0%），1 = 已上报
        ("llm_calls", "ALTER TABLE llm_calls ADD COLUMN cache_reported INTEGER NOT NULL DEFAULT 0"),
        # 供应商模式：对话档位改为引用 llm_providers，base_url/key 不再逐档位重复存
        ("model_profiles", "ALTER TABLE model_profiles ADD COLUMN provider_id CHAR(32) NULL"),
        ("model_profiles", "ALTER TABLE model_profiles ADD INDEX "
                           "ix_model_profiles_provider_id (provider_id)"),
        # 会话挂载技能（渐进式披露）；NULL=未指定（跟随项目默认）
        ("sessions", "ALTER TABLE sessions ADD COLUMN skills_json JSON"),
        # 个人外观偏好（主题/背景/字体色/字号）
        ("users", "ALTER TABLE users ADD COLUMN prefs_json JSON"),
        # 个人资料头像（/uploads/avatars/xxx 或外链）
        ("users", "ALTER TABLE users ADD COLUMN avatar VARCHAR(500) NULL"),
        # 生图预设参考图（界面每个预设选择旁展示的样图）
        ("image_presets", "ALTER TABLE image_presets ADD COLUMN image_file VARCHAR(200) NULL"),
        # 项目记忆 v2：结构化条目（标题/标签）+ 来源会话 + 更新时间；
        # 编排只注入标题索引，正文由 memory_read 工具按需取（渐进式披露）
        ("project_memories", "ALTER TABLE project_memories ADD COLUMN title VARCHAR(160) NULL"),
        ("project_memories", "ALTER TABLE project_memories ADD COLUMN tags VARCHAR(240) NULL"),
        ("project_memories", "ALTER TABLE project_memories ADD COLUMN session_id CHAR(32) NULL"),
        ("project_memories", "ALTER TABLE project_memories ADD COLUMN updated_at DATETIME NULL"),
        # 运行能力开关（自动化测试/联网/媒体）：默认 {} = 全开
        ("runs", "ALTER TABLE runs ADD COLUMN options_json JSON"),
        # 会话权限作用域：workspace=仅工作区（默认，安全边界）/ full=全部权限
        ("sessions", "ALTER TABLE sessions ADD COLUMN permission_scope VARCHAR(16) "
                     "NOT NULL DEFAULT 'workspace'"),
        # LLM 往返用途：agent=正文产出（默认）/ summarize=压缩历史的摘要调用。
        # 用量统计按它把摘要的小 prompt 从「上下文占用」里排除（llm_transcripts 14 天会清）。
        ("llm_calls", "ALTER TABLE llm_calls ADD COLUMN purpose VARCHAR(16) "
                      "NOT NULL DEFAULT 'agent'"),
    ]
    for table, ddl in stmts:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(ddl))
            log.info("migrated: %s", table)
        except Exception:  # noqa: BLE001 - 列已存在等情况
            pass
    await _relax_artifacts_run_id()
    await _dedupe_auto_memories()
    await _fix_event_seq()
    await _deround_role_prompts()
    await _refresh_role_prompts()
    await _collapse_agent_roles()
    await _sweep_transcripts()
    await _backfill_llm_providers()
    await _backfill_llm_call_purpose()


async def _backfill_llm_call_purpose() -> None:
    """存量回填：给历史 llm_calls 补 purpose。

    新列默认 'agent'，但历史上「压缩历史的摘要调用」也记在 llm_calls 里（agent_key=main），
    它们的 prompt 只是被压的那段历史（几 k），会把顶栏「上下文占用」带成 3%。旧代码靠
    llm_transcripts 关联来排除，但那张表 14 天就清。这里按 call_id 把已知的摘要调用标成
    'summarize'，让回填后即使 transcripts 被清也仍能正确排除。带 purpose='agent' 条件，
    跑过一次即空 → 天然幂等；失败不阻塞启动。
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE llm_calls SET purpose='summarize' "
                "WHERE purpose='agent' AND call_id IN "
                "(SELECT call_id FROM llm_transcripts WHERE purpose='summarize')"))
    except Exception:  # noqa: BLE001 - 回填失败不影响启动
        pass


async def _backfill_llm_providers() -> None:
    """把存量对话档位按 (协议, base_url, api_key) 归并成供应商，档位只留 provider_id。

    凭据一条都不能丢：归并键取完整三元组，只有真正相同的档位才共享同一个供应商。
    base_url 和 key 都为空的档位表示"用环境变量默认"，不建供应商（保持 provider_id
    为空），否则会被凭空造出的空供应商盖掉环境默认。
    归并后清掉档位上的 inline base_url / params_json.api_key，让界面与运行时同源。
    查询条件带 provider_id IS NULL，跑完一次即空 → 天然幂等。
    """
    import json
    import uuid as _uuid
    from urllib.parse import urlparse

    try:
        async with engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT id, team_id, provider, base_url, params_json "
                "FROM model_profiles WHERE kind = 'llm' AND provider_id IS NULL"))).all()
        llm_rows = []
        for pid, team_id, provider, base_url, params in rows:
            try:
                params = json.loads(params) if isinstance(params, str) else (params or {})
            except Exception:  # noqa: BLE001
                params = {}
            key = (params.get("api_key") or "").strip()
            url = (base_url or "").strip()
            if not url and not key:
                continue  # 环境变量默认档，不建供应商
            llm_rows.append((str(pid), str(team_id), provider or "openai", url, key))
        if not llm_rows:
            return

        # 按 (团队, 协议, base_url, key) 分组
        groups: dict[tuple, list[str]] = {}
        for pid, team_id, provider, url, key in llm_rows:
            groups.setdefault((team_id, provider, url, key), []).append(pid)

        created = 0
        used_labels: set[tuple[str, str]] = set()
        for (team_id, provider, url, key), profile_ids in groups.items():
            host = urlparse(url).netloc if url else ""
            base_slug = (host or key[:6] or "default").lower()
            slug = "".join(c if c.isalnum() else "-" for c in base_slug).strip("-")[:32] or "default"
            pkey, n = slug, 0
            while True:  # 同 host 多协议/多密钥时避免撞 key
                async with engine.begin() as conn:
                    dup = (await conn.execute(text(
                        "SELECT COUNT(*) FROM llm_providers "
                        "WHERE team_id = :t AND `key` = :k"),
                        {"t": team_id, "k": pkey})).scalar()
                if not dup:
                    break
                n += 1
                pkey = f"{slug}-{n}"[:40]
            new_id = _uuid.uuid4().hex  # 与建库时 Uuid 类型一致：CHAR(32) 存 32 位 hex
            label = host or pkey
            if (team_id, label) in used_labels:  # 同域名不同协议/密钥：显示名带上协议才不会撞脸
                label = f"{label} ({provider})"
            used_labels.add((team_id, label))
            async with engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO llm_providers (id, team_id, `key`, label, protocol, "
                    "base_url, api_key, enabled, models_json, created_at) "
                    "VALUES (:id, :t, :k, :l, :p, :u, :a, 1, '[]', CURRENT_TIMESTAMP)"),
                    {"id": new_id, "t": team_id, "k": pkey, "l": label,
                     "p": provider, "u": url or None, "a": key or None})
                remove = "JSON_REMOVE"  # MySQL / SQLite 内置函数名均不区分大小写
                for pid in profile_ids:
                    await conn.execute(text(
                        f"UPDATE model_profiles SET provider_id = :pv, base_url = NULL, "
                        f"params_json = COALESCE({remove}(COALESCE(params_json, '{{}}'), "
                        f"'$.api_key'), '{{}}') WHERE id = :pid"),
                        {"pv": new_id, "pid": pid})
                created += 1
        log.info("migrated: llm providers backfilled (%d providers / %d profiles)",
                 created, len(llm_rows))
    except Exception:  # noqa: BLE001
        log.exception("llm_providers 回填失败（忽略，档位仍可用 inline base_url 走旧路径）")


async def _sweep_transcripts() -> None:
    """清掉超过保留期的上下文现场（llm_transcripts 每轮存整份请求，是最大的表）。"""
    from datetime import datetime, timedelta, timezone

    from game_agent.config import LLM_TRANSCRIPT_KEEP_DAYS
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, LLM_TRANSCRIPT_KEEP_DAYS))
        async with engine.begin() as conn:
            r = await conn.execute(text(
                "DELETE FROM llm_transcripts WHERE created_at < :cutoff"),
                {"cutoff": cutoff})
        if r.rowcount:
            log.info("transcripts swept", extra={"deleted": r.rowcount,
                                                 "keep_days": LLM_TRANSCRIPT_KEEP_DAYS})
    except Exception:  # noqa: BLE001 - 表还不存在（首次建库）或被占用
        pass


async def _dedupe_auto_memories() -> None:
    """项目 auto 记忆改为「只保留一份最新要点」的覆盖式设计，
    历史流水记忆（旧版每次 run 追加一条）每个项目只留最新一条，其余清理。"""
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "DELETE FROM project_memories WHERE kind = 'auto' AND id NOT IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER ("
                "      PARTITION BY project_id ORDER BY created_at DESC"
                "    ) AS rn FROM project_memories WHERE kind = 'auto'"
                "  ) t WHERE rn = 1"
                ")"))
    except Exception:  # noqa: BLE001 - 幂等兜底
        log.exception("auto 记忆去重失败（忽略）")


async def _relax_artifacts_run_id() -> None:
    """artifacts.run_id 需可空（素材导入/外置拉取不关联 run）。

    SQLite 不能直接改列约束，需重建表；MySQL 用 MODIFY COLUMN 即可。
    """
    is_sqlite = DATABASE_URL.startswith("sqlite")
    if is_sqlite:
        async with engine.begin() as conn:
            rows = (await conn.execute(
                text("PRAGMA table_info(artifacts)"))).mappings().all()
        col = next((r for r in rows if r["name"] == "run_id"), None)
        if col is None or not col["notnull"]:
            return  # 表不存在（首次建库走 create_all）或已可空
    try:
        async with engine.begin() as conn:
            if not is_sqlite:
                await conn.execute(text(
                    "ALTER TABLE artifacts MODIFY COLUMN run_id CHAR(32) NULL"))
                log.info("migrated: artifacts.run_id nullable (mysql)")
                return
            # SQLite：建新表 → 拷贝 → 换名 → 重建索引
            await conn.execute(text(
                "CREATE TABLE artifacts_new ("
                "id CHAR(32) NOT NULL PRIMARY KEY, "
                "team_id CHAR(32) NOT NULL REFERENCES teams (id) ON DELETE CASCADE, "
                "project_id CHAR(32) NOT NULL REFERENCES projects (id) ON DELETE CASCADE, "
                "run_id CHAR(32) REFERENCES runs (id), "
                "task_id CHAR(32) REFERENCES tasks (id), "
                "asset_kind VARCHAR(12) NOT NULL, slug VARCHAR(120) NOT NULL, "
                "rel_path TEXT NOT NULL, provider VARCHAR(40), "
                "is_placeholder BOOLEAN NOT NULL, params_json JSON NOT NULL, "
                "size INTEGER, created_at TIMESTAMP NOT NULL)"))
            await conn.execute(text(
                "INSERT INTO artifacts_new SELECT id, team_id, project_id, run_id, task_id, "
                "asset_kind, slug, rel_path, provider, is_placeholder, params_json, size, "
                "created_at FROM artifacts"))
            await conn.execute(text("DROP TABLE artifacts"))
            await conn.execute(text("ALTER TABLE artifacts_new RENAME TO artifacts"))
            await conn.execute(text(
                "CREATE INDEX ix_artifacts_run_slug ON artifacts (run_id, task_id, slug)"))
        log.info("migrated: artifacts.run_id nullable (sqlite rebuild)")
    except Exception:  # noqa: BLE001
        log.exception("artifacts.run_id 迁移失败（若为全新库可忽略）")


async def _fix_event_seq() -> None:
    """events.seq 曾按 max+1 并发生成，高频 emit 会撞号导致事件乱序/丢失。

    迁移：对存在重复 seq 的项目按时间重编 seq，然后建 (project_id, seq) 唯一索引
    兜底（配合 repository.append_event 的冲突重试）。
    """
    is_sqlite = DATABASE_URL.startswith("sqlite")
    try:
        async with engine.begin() as conn:
            # 找出有重复 seq 的项目
            if is_sqlite:
                dups = (await conn.execute(text(
                    "SELECT DISTINCT project_id FROM events "
                    "WHERE project_id IS NOT NULL GROUP BY project_id, seq "
                    "HAVING COUNT(*) > 1"))).scalars().all()
            else:
                dups = (await conn.execute(text(
                    "SELECT DISTINCT project_id FROM ("
                    "  SELECT project_id, seq FROM events "
                    "  WHERE project_id IS NOT NULL GROUP BY project_id, seq "
                    "  HAVING COUNT(*) > 1) t"))).scalars().all()
            for pid in dups:
                ids = (await conn.execute(text(
                    "SELECT id FROM events WHERE project_id = :pid "
                    "ORDER BY created_at, id"), {"pid": pid})).scalars().all()
                for i, eid in enumerate(ids, 1):
                    await conn.execute(text(
                        "UPDATE events SET seq = :seq WHERE id = :eid"),
                        {"seq": i, "eid": eid})
                log.info("events seq renumbered for project %s (%d rows)", pid, len(ids))
            # 建唯一索引（幂等）
            if is_sqlite:
                await conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_project_seq "
                    "ON events (project_id, seq)"))
            else:
                exists = (await conn.execute(text(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = 'events' "
                    "AND index_name = 'uq_events_project_seq'"))).scalar()
                if not exists:
                    await conn.execute(text(
                        "CREATE UNIQUE INDEX uq_events_project_seq ON events (project_id, seq)"))
        if dups:
            log.info("migrated: events seq deduped (%d projects)", len(dups))
    except Exception:  # noqa: BLE001
        log.exception("events seq 迁移失败（忽略，新事件由 emit 锁保证有序）")


async def _deround_role_prompts() -> None:
    """把库里存量提示词里「按轮次干活」的措辞就地换掉。

    以前系统给每个角色定了工具轮次预算（组长 60 次），提示词里还写着「本轮轮次有限」
    「工具轮次预算靠这个活」——等于亲口教模型把 128 条语音拆成好几批、每批只做一个阶段，
    最后交回一份「本轮汇报（截至信息收集与执行阶段结束）」。现在预算已删，库里那份
    管理员可能改过的提示词不能整段覆盖，只做定点替换。
    """
    pairs = (
        ("═══ 二、写代码类任务的节奏（工具轮次预算靠这个活）═══",
         "═══ 二、写代码类任务的节奏（一次做完，别切阶段）═══"),
        ("重新截图，最多迭代 3 轮；", "重新截图，最多反复 3 次；"),
    )
    try:
        n = 0
        for old, new in pairs:
            async with engine.begin() as conn:
                res = await conn.execute(
                    text("UPDATE agents SET system_prompt = "
                         "REPLACE(system_prompt, :old, :new) "
                         "WHERE system_prompt LIKE :like"),
                    {"old": old, "new": new, "like": f"%{old[:24]}%"})
                n += res.rowcount or 0
        if n:
            log.info("migrated: round-budget wording removed from prompts (%d rows)", n)
    except Exception:  # noqa: BLE001
        log.exception("提示词去轮次化迁移失败（忽略）")


async def _refresh_role_prompts() -> None:
    """v3 单角色（main）提示词迁移。

    库里存的旧提示词若还引用已删除的工具/角色（write_work_order / dispatch_development /
    dispatch_testing / request_art / request_audio，以及 v2 的 dispatch_art / dispatch_audio
    派发体系），刷新为新默认——工具已不存在，留着只会让模型反复去调不存在的工具、
    把轮次烧光。管理员不含这些痕迹的自定义提示词保持不动。
    """
    from game_agent.agents.roles import ROLE_PROMPTS

    stale_marks = ("write_work_order", "dispatch_development", "dispatch_testing",
                   "request_art", "request_audio",
                   "dispatch_art", "dispatch_audio")
    newp = ROLE_PROMPTS.get("main") or ""
    try:
        async with engine.begin() as conn:
            for mark in stale_marks:
                res = await conn.execute(text(
                    "UPDATE agents SET system_prompt = :np "
                    "WHERE `key` = 'main' AND system_prompt LIKE :m"),
                    {"np": newp, "k": mark, "m": f"%{mark}%"})
                if res.rowcount:
                    log.info("migrated: main prompt refreshed (removed stale ref %s, %d rows)",
                             mark, res.rowcount)
            res = await conn.execute(text(
                "DELETE FROM agents WHERE `key` IN ('developer', 'tester', 'tester_qa')"))
            if res.rowcount:
                log.info("migrated: retired agent roles removed (%d rows)", res.rowcount)
            res = await conn.execute(text(
                "UPDATE agents SET params_json = JSON_REMOVE(params_json, '$.max_tool_rounds') "
                "WHERE JSON_EXTRACT(params_json, '$.max_tool_rounds') IS NOT NULL"))
            if res.rowcount:
                log.info("migrated: max_tool_rounds overrides removed (%d rows)", res.rowcount)
            # v3 提示词迭代（环境信息去重 / 占位模式前置 / 二·五压缩成硬规则）：
            # 只刷新带旧版指纹的行（旧默认提示词里的事故叙事），管理员自定义的不动。
            res = await conn.execute(text(
                "UPDATE agents SET system_prompt = :np WHERE `key` = 'main' AND "
                "(system_prompt LIKE '%49 分钟、348 次工具调用%' "
                "OR system_prompt LIKE '%先改后证」纪律（2026-10-28 实测教训）%' "
                "OR (system_prompt LIKE '%配音只有一个入口%' "
                "AND system_prompt NOT LIKE '%list_voice_presets%'))"),
                {"np": newp})
            if res.rowcount:
                log.info("migrated: main prompt refreshed (v3.2, %d rows)", res.rowcount)
            # 2026-11-01：收尾总结改版（原因 / 说明 / 风险点必须写清）。
            # ⚠️ 不能整段覆盖 system_prompt：管理员可能只改了别的章节，整段覆盖会把他的
            # 自定义一起冲掉（v3.2 的 _is_stock_ancestor 兜底在这里也救不了——本次第九节
            # 是「改写」不是「新增」，逐行比对会判定为管理员改过而拒绝升级）。
            # 正确做法：只对「仍原封不动带着旧第九节」的行，做一次精确子串替换——
            # 第九节之外的自定义原样保留，第九节已被管理员改过的行（旧块匹配不上）不动。
            _old_tail = (
                "═══ 九、汇报（report_to_user，五段式，简明）═══\n"
                "目标 / 改动表（文件→做了什么）/ 新增素材（类型+路径）/ 验证证据（编译、单测、服务 url、\n"
                "截图结论）/ 注意事项。没做完就如实写缺什么，不要美化。"
            )
            _idx = newp.find("═══ 九、汇报（report_to_user")
            _new_tail = newp[_idx:] if _idx >= 0 else ""
            if _new_tail:
                res = await conn.execute(text(
                    "UPDATE agents SET system_prompt = REPLACE(system_prompt, :old, :new) "
                    "WHERE `key` = 'main' AND system_prompt LIKE :pat"),
                    {"old": _old_tail, "new": _new_tail, "pat": "%五段式，简明%"})
                if res.rowcount:
                    log.info("migrated: main 汇报第九节升级为含原因/说明/风险点 (%d rows)",
                             res.rowcount)
            # v3.2 数据修复：把团队唯一的音频/图像档位自动补绑到 main 的 params_json
            # （角色合并时旧行里本来就没有 *_profile 字段 → 档位明明配了，
            # generate_audio/generate_image 却解析不到、静默走占位）。
            # 管理员已显式绑定的不动；该 kind 有多个档位时不猜，留给用户绑。
            # 用「先读后写」的 Python 循环而不是 UPDATE...JOIN：MySQL 的
            # `UPDATE agents a SET a.col=...` 在 SQLite 上是语法错误（打包版首启即报
            # near "a"，2026-10-30 实测），Python 侧处理两库都稳。
            # ⚠️ params_json 可能存的是 JSON 文档 `null`（不是 SQL NULL），所以解析后
            # 非 dict 一律当空对象，再 JSON_SET 回写。
            for kind in ("audio", "image"):
                rows = (await conn.execute(text(
                    "SELECT a.id, a.params_json, "
                    "(SELECT COUNT(*) FROM model_profiles p2 "
                    " WHERE p2.team_id = a.team_id AND p2.kind = :kind) AS cnt, "
                    "(SELECT p.`key` FROM model_profiles p "
                    " WHERE p.team_id = a.team_id AND p.kind = :kind LIMIT 1) AS pk "
                    "FROM agents a WHERE a.`key` = 'main'"), {"kind": kind})).all()
                for rid, pj, cnt, pk in rows:
                    if isinstance(pj, str):
                        try:
                            pj = json.loads(pj)
                        except ValueError:
                            pj = None
                    pj = pj if isinstance(pj, dict) else {}
                    if pj.get(f"{kind}_profile") is not None or cnt != 1 or not pk:
                        continue
                    pj[f"{kind}_profile"] = pk
                    await conn.execute(
                        text("UPDATE agents SET params_json = :pj WHERE id = :rid"),
                        {"pj": json.dumps(pj, ensure_ascii=False), "rid": rid})
                    log.info("migrated: main %s_profile auto-bound (unique %s profile)",
                             kind, kind)
            # 出厂提示词的后续迭代此前只活在代码里：seed_default_agents 建行之后再也不碰，
            # 于是「一次问全 / 先取数字再截图 / 入口只撞一次」这几条改完几天都没上线
            # （2026-10-29 实测：库里的 main 提示词还停在加这三条之前的版本）。
            # 判定口径：逐行比对，存的内容能在当前默认里全找到（只有新增、没有删改）
            # = 只是版本落后的出厂词，升级；有任何一行当前默认里没有 = 管理员改过，不动。
            up = 0
            res = await conn.execute(text(
                "SELECT team_id, system_prompt, params_json FROM agents WHERE `key` = 'main'"))
            for team_id, sp, pj in res.all():
                if not sp or sp == newp:
                    continue
                if isinstance(pj, str):
                    import json as _json
                    try:
                        pj = _json.loads(pj)
                    except ValueError:
                        pj = None
                if (pj or {}).get("prompt_custom") is True:
                    continue  # 管理员在设置里存过自定义正文，绝不覆盖
                if not _is_stock_ancestor(sp, newp):
                    log.info("main prompt kept as custom (team=%s)", str(team_id)[:8])
                    continue
                await conn.execute(text(
                    "UPDATE agents SET system_prompt = :np "
                    "WHERE `key` = 'main' AND team_id = :t"), {"np": newp, "t": team_id})
                up += 1
            if up:
                log.info("migrated: main prompt upgraded to current default (%d rows)", up)
            # 2026-11-04：出厂词 v3.3 —— 新增 generate_video 用法段，并把「美术验收写文件」
            # 那两行改写成更硬的三行（这是替换，不是纯新增）。上面的 _is_stock_ancestor
            # 只认「纯新增」，这里若仍用它会整批拒绝升级，导致视频工具对模型不可见。
            # 只对「旧指纹行 + 宽松模式判定为出厂滞后 + 未标记 prompt_custom」的行升级；
            # 管理员动过的行两边都匹配不上，保持不动。
            v33_marks = ("只有选定某张需要检查细节时才单独 view_image。看图后把结论写进正文",
                         "要视频（动态素材/过场动画")
            if "generate_video" in newp and "看完必须把判断写成文件" in newp:
                res = await conn.execute(text(
                    "SELECT team_id, system_prompt, params_json FROM agents WHERE `key` = 'main'"))
                up2 = 0
                for team_id, sp, pj in res.all():
                    sp = sp or ""
                    if not sp or sp == newp:
                        continue
                    if "generate_video" in sp:
                        continue  # 已经带着视频段，无需升级
                    if not any(m in sp for m in v33_marks):
                        continue  # 连 v3.3 之前的指纹都没有 → 不是本次的滞后版本
                    if isinstance(pj, str):
                        try:
                            pj = json.loads(pj)
                        except ValueError:
                            pj = None
                    if (pj or {}).get("prompt_custom") is True:
                        continue
                    if not _is_stock_ancestor_allow_replace(sp, newp):
                        log.info("main prompt kept as custom (v3.3, team=%s)", str(team_id)[:8])
                        continue
                    await conn.execute(text(
                        "UPDATE agents SET system_prompt = :np "
                        "WHERE `key` = 'main' AND team_id = :t"), {"np": newp, "t": team_id})
                    up2 += 1
                if up2:
                    log.info("migrated: main prompt upgraded to v3.3 (video tool + art-review note, %d rows)",
                             up2)
            # 2026-11-05：出厂词 v3.4 —— 新增「八方画像 / 多视角 views」与 negative_prompt 用法。
            # 这是纯新增段（不删改旧行），用 _is_stock_ancestor 判定即可：只有仍是出厂词
            # （旧内容能在新默认里全找到）的团队才升级；管理员自定义的行匹配不上，保持不动。
            if "八方画像" in newp and "views=\"all\"" in newp:
                res = await conn.execute(text(
                    "SELECT team_id, system_prompt, params_json FROM agents WHERE `key` = 'main'"))
                up3 = 0
                for team_id, sp, pj in res.all():
                    sp = sp or ""
                    if not sp or sp == newp or "八方画像" in sp:
                        continue  # 空/已最新/已带多视角段，无需升级
                    if isinstance(pj, str):
                        try:
                            pj = json.loads(pj)
                        except ValueError:
                            pj = None
                    if (pj or {}).get("prompt_custom") is True:
                        continue
                    if not _is_stock_ancestor(sp, newp):
                        log.info("main prompt kept as custom (v3.4, team=%s)", str(team_id)[:8])
                        continue
                    await conn.execute(text(
                        "UPDATE agents SET system_prompt = :np "
                        "WHERE `key` = 'main' AND team_id = :t"), {"np": newp, "t": team_id})
                    up3 += 1
                if up3:
                    log.info("migrated: main prompt upgraded to v3.4 (multi-view views + negative_prompt, %d rows)",
                             up3)
    except Exception:  # noqa: BLE001
        log.exception("角色提示词 v3 迁移失败（忽略）")


def _is_stock_ancestor(old: str, new: str) -> bool:
    """旧提示词是否只是「当前默认的早期出厂版本」：按行比对只允许 new 侧新增。"""
    import difflib

    a = [ln.rstrip() for ln in (old or "").splitlines() if ln.strip()]
    b = [ln.rstrip() for ln in (new or "").splitlines() if ln.strip()]
    if not a or not b:
        return False
    return all(tag in ("equal", "insert")
               for tag, *_ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes())


def _is_stock_ancestor_allow_replace(old: str, new: str) -> bool:
    """比 _is_stock_ancestor 松一档：允许 new 侧「替换掉一条已知的出厂旧行」。

    仅用于已核实的出厂迭代。判定：
      1) 逐行比对，每段 opcode 必须是 equal / insert / replace（不允许 delete）；
      2) 被替换掉的旧行，要么仍能在 new 全文里找到（搬走/改写保留），要么精确等于
         本迭代白名单里那条「出厂被改写的旧行」——其余一律判 False。
    这样：出厂滞后（只差 v3.3 那几处）→ True；管理员删/改过任何别的行 → 有对不上的
    旧行 → False，绝不会把管理员的自定义整段冲掉。
    """
    import difflib

    known_stock_drops = {
        # v3.2 → v3.3：美术验收那句被改写成「必须写成 .trace/art-review.md」
        "只有选定某张需要检查细节时才单独 view_image。看图后把结论写进正文，像素不重复发送。",
    }
    a = [ln.rstrip() for ln in (old or "").splitlines() if ln.strip()]
    b = [ln.rstrip() for ln in (new or "").splitlines() if ln.strip()]
    if not a or not b:
        return False
    bset = {ln.strip() for ln in b}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("equal", "insert"):
            continue
        if tag == "delete":
            return False
        for ln in a[i1:i2]:
            s = ln.strip()
            if s not in bset and s not in known_stock_drops:
                return False
    return True


async def _collapse_agent_roles() -> None:
    """v2 三角色（planner/artist/voice）→ v3 单角色（main）的存量迁移。

    每个团队：若还没有 main 行，则
      - model_profile_key ← planner 行的（会话模型）；
      - params_json.image_profile ← artist 行的（美术模型）；
      - params_json.audio_profile ← voice 行的（配音模型）；
      - system_prompt ← planner 的旧提示词，若为空或还引用 dispatch_* 派发体系
        则直接用 v3 默认 main 提示词（派发工具已删除，旧提示词只会误导）。
    然后删除三个旧角色行（幂等：再跑一遍时旧行已不存在）。
    """
    import json as _json

    from sqlalchemy import select

    from game_agent.agents.roles import ROLE_PROMPTS
    from game_agent.db.models import Agent
    from game_agent.db.session import SessionLocal

    default_prompt = ROLE_PROMPTS.get("main") or ""
    try:
        async with SessionLocal() as s:
            rows = (await s.execute(select(Agent))).scalars().all()
            by_team: dict[str, list] = {}
            for r in rows:
                by_team.setdefault(r.team_id, []).append(r)
            created = removed = 0
            for team_id, team_rows in by_team.items():
                by_key = {r.key: r for r in team_rows}
                if not ({"planner", "artist", "voice"} & set(by_key)):
                    continue
                main = by_key.get("main")
                if main is None:
                    planner = by_key.get("planner")
                    artist = by_key.get("artist")
                    voice = by_key.get("voice")
                    params: dict = {}
                    try:
                        if planner and planner.params_json:
                            params = dict(_json.loads(planner.params_json))
                    except Exception:  # noqa: BLE001
                        params = {}
                    try:
                        if artist and artist.params_json:
                            ip = (_json.loads(artist.params_json) or {}).get("image_profile")
                            if ip:
                                params["image_profile"] = ip
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        if voice and voice.params_json:
                            ap = (_json.loads(voice.params_json) or {}).get("audio_profile")
                            if ap:
                                params["audio_profile"] = ap
                    except Exception:  # noqa: BLE001
                        pass
                    old_prompt = (planner.system_prompt if planner else "") or ""
                    if ("dispatch_art" in old_prompt or "dispatch_audio" in old_prompt
                            or not old_prompt.strip()):
                        old_prompt = default_prompt
                    main = Agent(
                        team_id=team_id, key="main",
                        system_prompt=old_prompt,
                        model_profile_key=(planner.model_profile_key if planner else None),
                        params_json=_json.dumps(params, ensure_ascii=False) if params else None,
                    )
                    s.add(main)
                    created += 1
                for r in team_rows:
                    if r.key in ("planner", "artist", "voice"):
                        await s.delete(r)
                        removed += 1
            await s.commit()
            if created or removed:
                log.info("migrated: agent roles collapsed to main (created %d, removed %d)",
                         created, removed)
    except Exception:  # noqa: BLE001
        log.exception("三角色合并迁移失败（忽略，下次启动重试）")


def _mask(url: str) -> str:
    if "://" in url and "@" in url:
        head, _, tail = url.partition("@")
        return f"{head.split('://')[0]}://***@{tail}"
    return url
