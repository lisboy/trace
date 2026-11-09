"""改动快照：run 内第一次写某文件**之前**把原内容落盘，供「审阅 → 回滚」使用。

平台不接管 git，代码改动直接写进工作区，事后工作区里再也找不到「改之前长什么样」的
东西。所以在工具层（write_file / edit_file / delete_file）落一份快照，与项目类型无关，
任何项目都能审阅和回滚。

磁盘布局::

    <DATA_DIR>/run_snapshots/<run_id>/index.json      # 元数据：哪些文件被改过
    <DATA_DIR>/run_snapshots/<run_id>/files/<sha1>.orig  # 改之前的原始字节

同一 run 内同一文件被多次修改时只保留**第一次**改动前的内容（即 run 开始时的状态），
这样「回滚」= 把文件恢复成这次 run 之前的样子。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from game_agent.config import DATA_DIR
from game_agent.logging_setup import get_logger

log = get_logger("orchestration.snapshots")

SNAPSHOT_ROOT = DATA_DIR / "run_snapshots"
MAX_DIFF_BYTES = 400_000     # 单文件超过这个体积只报行数，不给逐行 diff
MAX_REVERT_LOG = 200

# 预览方式（按扩展名定；认不出来的先返回 unknown，由 API 层嗅探内容再定）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
WEB_EXTS = {".html", ".htm"}
MD_EXTS = {".md", ".markdown", ".mdx"}
PDF_EXTS = {".pdf"}
TEXT_EXTS = {
    ".txt", ".log", ".csv", ".tsv", ".json", ".jsonc", ".yml", ".yaml", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".xml", ".sql", ".graphql", ".proto", ".lock",
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".css", ".scss", ".sass", ".less", ".lua", ".gd", ".cs", ".java", ".kt", ".go",
    ".rs", ".c", ".h", ".cpp", ".hpp", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".gitignore", ".dockerignore", ".editorconfig", ".gitattributes",
}


def preview_kind(rel: str) -> str:
    """文件该用哪种方式预览：image / audio / html / markdown / pdf / text / unknown。"""
    name = Path(rel).name.lower()
    ext = Path(rel).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in WEB_EXTS:
        return "html"
    if ext in MD_EXTS:
        return "markdown"
    if ext in PDF_EXTS:
        return "pdf"
    # 没有扩展名的常见文本文件（Makefile / Dockerfile / LICENSE …）
    if ext in TEXT_EXTS or name in {".gitignore", ".env", "makefile", "dockerfile",
                                    "license", "readme", "procfile"}:
        return "text"
    return "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_dir(run_id: str) -> Path:
    return SNAPSHOT_ROOT / str(run_id)


def index_path(run_id: str) -> Path:
    return run_dir(run_id) / "index.json"


def blob_path(run_id: str, rel: str) -> Path:
    key = hashlib.sha1(rel.encode("utf-8")).hexdigest()
    return run_dir(run_id) / "files" / f"{key}.orig"


def _load(run_id: str) -> dict:
    p = index_path(run_id)
    if not p.exists():
        return {"files": {}, "reverted": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": {}, "reverted": []}
    data.setdefault("files", {})
    data.setdefault("reverted", [])
    return data


def _save(run_id: str, data: dict) -> None:
    d = run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    index_path(run_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def remember(run_id: str, workspace: Path, rel: str, *, tool: str = "",
             agent: str = "") -> None:
    """改动某个文件**之前**调用；幂等（重复调用不会覆盖第一次的快照）。"""
    if not run_id or not rel:
        return
    rel = rel.replace("\\", "/").lstrip("/")
    data = _load(run_id)
    if rel in data["files"]:
        return
    try:
        target = (Path(workspace) / rel).resolve()
        if not str(target).startswith(str(Path(workspace).resolve())):
            return  # 越界不记
        existed = target.is_file()
        entry = {"path": rel, "existed": existed, "tool": tool, "agent": agent,
                 "at": _now(), "size": target.stat().st_size if existed else 0}
        if existed:
            blob = blob_path(run_id, rel)
            blob.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, blob)
        data["files"][rel] = entry
        _save(run_id, data)
    except OSError as e:  # 快照失败绝不能影响工具本身
        log.warning("snapshot failed", extra={"run_id": run_id, "path": rel, "err": str(e)})


def _read_text(path: Path, limit: int = MAX_DIFF_BYTES) -> tuple[str, bool]:
    """读为文本；返回 (文本, 是否因过大被截断)。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return "", False
    truncated = len(raw) > limit
    return raw[:limit].decode("utf-8", errors="replace"), truncated


def _line_diff(before: str, after: str) -> list[dict]:
    """行级 diff → [{kind: ctx|add|del, old_no, new_no, text}]（前端直接渲染）。"""
    a = before.splitlines()
    b = after.splitlines()
    out: list[dict] = []
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out.append({"kind": "ctx", "old_no": i1 + k + 1, "new_no": j1 + k + 1,
                            "text": a[i1 + k]})
        elif tag == "delete":
            for k in range(i1, i2):
                out.append({"kind": "del", "old_no": k + 1, "new_no": None, "text": a[k]})
        elif tag == "insert":
            for k in range(j1, j2):
                out.append({"kind": "add", "old_no": None, "new_no": k + 1, "text": b[k]})
        else:  # replace：先删后加，读起来更接近 unified diff
            for k in range(i1, i2):
                out.append({"kind": "del", "old_no": k + 1, "new_no": None, "text": a[k]})
            for k in range(j1, j2):
                out.append({"kind": "add", "old_no": None, "new_no": k + 1, "text": b[k]})
    return out


def list_changes(run_id: str, workspace: Path, *, with_diff: bool = True,
                 max_lines: int = 4000, include_scratch: bool = False) -> dict:
    """本次 run 改过、且**当前还没回滚**的文件列表（含前后差异）。

    ``include_scratch=False``（默认）会排除过程文件（.trace/tmp/ 探针、target/、
    tmp/、scratch/、_probe_*.py…）——「已编辑 N 个文件」这类用户可见清单只该报
    对用户文件的真实改动。以前过滤只放在上层 change_report，导致 /changes 接口
    （改动卡片 / diff 评审的数据源）漏出 .trace/tmp/* 探针脚本。
    """
    data = _load(run_id)
    ws = Path(workspace)
    files: list[dict] = []
    for rel, entry in (data.get("files") or {}).items():
        try:
            target = (ws / rel).resolve()
        except OSError:
            continue
        blob = blob_path(run_id, rel)
        existed = bool(entry.get("existed"))
        before = ""
        if existed and blob.exists():
            before, before_trunc = _read_text(blob)
        else:
            before_trunc = False
        now_exists = target.is_file()
        after, after_trunc = _read_text(target) if now_exists else ("", False)
        if existed and not now_exists:
            status = "deleted"
        elif not existed and now_exists:
            status = "added"
        elif not now_exists:
            status = "deleted"   # 新建后又删掉：净效果等于没这个文件
        else:
            status = "modified"
        if before == after and existed == now_exists:
            continue  # 已经还原（内容与快照一致），不必再展示
        lines: list[dict] = []
        added = deleted = 0
        if with_diff and not (before_trunc or after_trunc):
            lines = _line_diff(before, after)
            if len(lines) > max_lines:
                lines = lines[:max_lines]
            added = sum(1 for x in lines if x["kind"] == "add")
            deleted = sum(1 for x in lines if x["kind"] == "del")
        elif with_diff:
            added = max(len(after.splitlines()) - len(before.splitlines()), 0)
            deleted = max(len(before.splitlines()) - len(after.splitlines()), 0)
        files.append({
            "path": rel, "status": status, "added": added, "deleted": deleted,
            "diff": lines, "binary_or_big": bool(before_trunc or after_trunc),
            "preview": preview_kind(rel),
            "size": target.stat().st_size if now_exists else 0,
            "tool": entry.get("tool", ""), "agent": entry.get("agent", ""),
            "at": entry.get("at", ""), "can_revert": existed or now_exists,
        })
    files.sort(key=lambda f: f["path"])
    reverted = (data.get("reverted") or [])[-20:]
    if not include_scratch:
        from game_agent.agents.tools import is_scratch_path
        files = [f for f in files if not is_scratch_path(f["path"])]
        reverted = [r for r in reverted
                    if not is_scratch_path(str((r or {}).get("path") or ""))]
    return {
        "run_id": str(run_id),
        "files": files,
        "summary": {
            "count": len(files),
            "added": sum(f["added"] for f in files),
            "deleted": sum(f["deleted"] for f in files),
        },
        "reverted": reverted,
    }


def revert(run_id: str, workspace: Path, paths: list[str] | None = None) -> dict:
    """回滚：把文件恢复成这次 run 之前的样子（``paths`` 为空表示全部）。

    ``paths`` 为空时只回滚用户文件（排除 .trace/tmp 等过程文件）——它们收尾会被
    tempfiles 清掉，没必要也不该在「全部回滚」里出现；传了具体 paths 则照单全收。
    """
    data = _load(run_id)
    entries: dict = data.get("files") or {}
    if not paths:
        from game_agent.agents.tools import is_scratch_path
        wanted = [rel for rel in entries if not is_scratch_path(rel)]
    else:
        wanted = [p.replace("\\", "/").lstrip("/") for p in paths if p]
    ws = Path(workspace).resolve()
    done: list[str] = []
    failed: list[dict] = []
    for rel in wanted:
        entry = entries.get(rel)
        if entry is None:
            failed.append({"path": rel, "error": "没有该文件的快照（可能已回滚或不属于本 run）"})
            continue
        try:
            target = (ws / rel).resolve()
            if not str(target).startswith(str(ws)):
                failed.append({"path": rel, "error": "路径越界"})
                continue
            if entry.get("existed"):
                blob = blob_path(run_id, rel)
                if not blob.exists():
                    failed.append({"path": rel, "error": "快照文件缺失，无法恢复"})
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(blob, target)
            elif target.is_file():
                target.unlink()
            done.append(rel)
            entries.pop(rel, None)
            data.setdefault("reverted", []).append({"path": rel, "at": _now()})
        except OSError as e:
            failed.append({"path": rel, "error": str(e)})
    data["reverted"] = (data.get("reverted") or [])[-MAX_REVERT_LOG:]
    data["files"] = entries
    _save(run_id, data)
    return {"ok": not failed, "reverted": done, "failed": failed}
