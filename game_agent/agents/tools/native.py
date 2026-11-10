"""原生 fs/shell 工具（设计 §7.3.1）。"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from game_agent.agents.tools import ToolContext, ToolError
from game_agent.config import (
    COMMAND_WHITELIST, DANGEROUS_PATTERNS, MAX_CONCURRENT_RUNS, PLATFORM,
    PROTECTED_PREFIXES, SEARCH_TIMEOUT_S, SHELL_POLICY, TOOL_TIMEOUT_S, env_examples,
)
from game_agent.core.skills import SKILL_SCOPE_LABELS
from game_agent.orchestration import envfacts

MAX_READ = 16000
MAX_WRITE = 2 * 1024 * 1024  # 2MB

# run_command 并发上限（**不是**串行锁）：见 _run_shell_cmd docstring。
# 历史：2026-10-28 两次 python313 段错误曾被归因为「并发 subprocess」，于是加了一把进程级
# threading.Lock 把 run_command 整体串行化。后来定位到真正的元凶是 faulthandler 看门狗的
# 原生线程（已默认关闭，见 main.py），锁已无必要。而且这把全局锁有致命副作用：一条挂死的
# 子进程会长期占着它，连坐**所有 run** 的 run_command（64 个 worker 共用一个锁）——
# 2026-11-10 实测就是这样「好几个 run 一起卡死」。改成并发信号量：限流仍在，但不再连坐。
_SHELL_SEM = asyncio.Semaphore(max(4, min(int(MAX_CONCURRENT_RUNS or 8), 16)))

# 扫描剪枝：这些目录名在任何层级都不进（node_modules 可达十万级文件，同步遍历会冻结事件循环）。
# 「项目过大后搜索特别慢」的根因就在这——此前只剪了少数几个名字，用户 project 里自带的
# .ga-dev（Chrome profile/快照，动辄几百 MB 上千文件）、.trace（截图/上传/临时文件）、
# .npm-cache、dist_exe 全被逐个 open 再读，一次 grep 生生扫近 2GB。
_SKIP_DIRS = {
    # 依赖 / 构建产物
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
    "target", "out", "vendor", "Pods", "DerivedData",
    "bin", "obj", "site-packages", ".next", ".nuxt", ".turbo", ".parcel-cache",
    # 编辑器 / 工具缓存
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
    "coverage", ".nyc_output",
    # 本产品自己的运行/开发副产物（不是用户源码，且体积巨大）
    ".ga", ".ga-dev", ".trace", ".npm-cache",
    ".cv-backup", ".tmp-qiyi-prompts", "dist_exe", ".svelte-kit",
    # 游戏引擎的自动生成目录（本产品面向游戏开发，这些目录动辄几万文件且全是
    # 引擎产物，搜源码时没有任何价值）：Unity / Unreal / Godot
    "Library", "Temp", "Obj", "Logs", "UserSettings", "MemoryCaptures",
    "Binaries", "Intermediate", "Saved", "DerivedDataCache", ".vs", ".godot",
}

# 工作区根下这些目录永远不是「用户想搜的源码」，且实测占了绝大部分扫描体积。
# 即使某天改名没进 _SKIP_DIRS，也靠这里兜底跳过，避免搜索被运行时垃圾拖慢。
_ROOT_NOISE_PREFIXES = (".ga", ".ga-dev", ".trace")

# 明显不是文本的后缀：整个跳过（否则 grep 结果里全是 dll 二进制噪音，白烧轮次）
_BINARY_EXT = {
    ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll", ".exe", ".lib", ".obj", ".pdb",
    ".class", ".jar", ".war", ".wasm", ".bin", ".dat", ".pak", ".pak0", ".bundle",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".tga", ".webp", ".psd",
    ".wav", ".mp3", ".ogg", ".flac", ".m4a", ".mp4", ".webm", ".mov",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".db", ".sqlite", ".sqlite3", ".zip", ".rar", ".7z", ".gz", ".tar", ".xz",
    ".map", ".lock", ".pdf", ".docx", ".xlsx", ".pptx",
}

# grep 单文件上限：超大文件（打包产物/日志）不读，避免一次扫描卡住几分钟
_GREP_MAX_FILE = 2 * 1024 * 1024


def _resolve(ws: Path, p: str, scope: str = "workspace") -> Path:
    """把工具路径解析成绝对路径。

    scope="workspace"（默认）：只允许工作区内，越界直接报错——这是本产品一直以来的安全边界。
    scope="full"：本轮会话被用户显式授予「全部权限」，允许访问工作区以外的路径
    （比如 ../test/cdp-test/run-extra35.mjs 这种项目目录外的文件）。

    绝对路径与 `..` 在 workspace 下也统一按 resolve 后的真实路径判定，
    防止 `a/../../etc` 这类绕路。系统级危险目录（盘符根、系统目录）在 full 下仍拦，
    避免一次误操作把整台机器写坏。"""
    ws_resolved = ws.resolve()
    if scope == "full":
        target = Path(p).expanduser()
        if not target.is_absolute():
            target = ws_resolved / target
        target = target.resolve()
        if _is_system_critical(target):
            raise ToolError(
                f"系统关键目录受保护，即使「全部权限」也不允许访问：{p}")
        return target
    target = (ws_resolved / p).resolve()
    if not target.is_relative_to(ws_resolved):
        raise ToolError(
            f"路径越界，禁止访问工作区外：{p}（本会话权限为「仅工作区」；"
            "需要读写项目外文件，请把该会话权限切到「全部权限」）")
    return target


# 系统关键目录：即使授予「全部权限」也永远不碰（盘符根 / Windows / Program Files / 用户主目录本身）。
# 只拦「这些目录本身或它们的直接上层」，正常项目子目录不受影响。
def _is_system_critical(target: Path) -> bool:
    raw = str(target).replace("\\", "/").rstrip("/")
    if not raw:
        return True
    # 盘符根：C: / D:
    if re.fullmatch(r"[A-Za-z]:", raw):
        return True
    # 去掉盘符前缀（D:/xxx → /xxx），统一按 POSIX 形式比对
    rel = re.sub(r"^[A-Za-z]:", "", raw).lower()
    if rel.startswith("/windows") or rel.startswith("/program files") \
            or rel.startswith("/programdata"):
        return True
    if rel.startswith("/program files (x86)"):
        return True
    # 用户主目录本身（不拦其下的普通子目录，避免误伤桌面/文档里的合法项目）
    import os as _os
    home = _os.path.expanduser("~").replace("\\", "/").rstrip("/").lower()
    if home and raw.lower() == home:
        return True
    return False


def _check_protected(rel: str) -> None:
    parts = Path(rel).parts
    for pref in PROTECTED_PREFIXES:
        if rel == pref or rel.startswith(pref + "/") or (pref in parts):
            raise ToolError(f"受保护目录，禁止操作：{pref}")


def _snapshot_before_write(t: ToolContext, rel: str, tool: str) -> None:
    """改动前留快照（供用户审阅后回滚）。快照失败绝不影响工具本身。"""
    try:
        from game_agent.orchestration.snapshots import remember
        remember(str(getattr(t.run_ctx, "run_id", "") or ""), t.workspace, rel,
                 tool=tool, agent=str(getattr(t, "role", "") or ""))
    except Exception:  # noqa: BLE001 - 审计失败不阻断写文件
        pass


def _restamp_after_write(t: ToolContext, key: str, path: Path) -> None:
    """落盘后把指纹换成「我刚写的这一版」。

    不刷新的话，同一 run 内自己上一次 edit 成功后 read_stamp 还是旧值，
    下一次 edit 必然撞 stale（2026-10-28 会话连撞 3 次，每次多烧一轮重读）。
    跨 run 的外部修改照旧拦截：read_stamp 是 run 级的，行为不变。
    """
    try:
        st = path.stat()
        t.read_stamp[key] = (st.st_mtime, st.st_size)
        t.read_set.add(key)
    except OSError:  # noqa: BLE001 - 刷不上指纹不影响写文件已成功
        pass


_IMG_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".aac", ".amr"}


async def read_file(args: dict, t: ToolContext):  # -> str | ToolResult
    path = _resolve(t.workspace, args["path"], t.permission_scope)
    if not path.exists():
        raise ToolError(f"文件不存在：{args['path']}（新建文件请用 write_file）")
    if path.is_dir():
        raise ToolError(f"这是目录：{args['path']}")
    # 图片按文本读只会吐乱码（实测：PNG 被当成 3759 行字符，模型只能猜画面）→ 直接回灌视觉
    if path.suffix.lower() in _IMG_SUFFIXES:
        from game_agent.agents.tools.service import view_image
        return await view_image({"path": args["path"]}, t)
    # 音频既读不出内容、也绝不该整包发进上下文（base64 会把请求体顶到 413）→ 只给路径与量法
    if path.suffix.lower() in _AUDIO_SUFFIXES:
        size = path.stat().st_size
        return (f"这是音频文件（{size // 1024}KB），不会进入模型上下文：按文本读只有乱码，"
                "整包 base64 发出去会把请求体顶爆。要核对它，用路径 + 客观指标："
                f'run_command 跑 ffprobe -hide_banner "{args["path"]}" 看时长/采样率，'
                "或跑一段脚本比文件大小；试听由用户在前端完成，"
                "需要用户确认时直接 report_to_user 说明路径。")
    # 文档容器（zip/二进制）按文本读也是乱码 → 直接指到解析工具
    if path.suffix.lower() in (".xlsx", ".docx", ".pdf", ".xls", ".doc", ".ppt", ".pptx"):
        fmt = {"xlsx": "xlsx", "docx": "docx", "pdf": "pdf"}.get(path.suffix.lower().lstrip("."))
        hint = (f"这是 Office/PDF 文档，read_file 读不出内容（会是乱码）。"
                f"请用 parse_document 解析：{{\"path\": \"{args['path']}\"}}"
                + (f"，pdf 可加 page 参数逐页看" if fmt == "pdf" else ""))
        if path.suffix.lower() in (".ppt", ".pptx"):
            hint = "不支持解析 PowerPoint 文档（.ppt/.pptx），请先导出为 PDF，再用 parse_document 读取"
        elif fmt is None:
            hint = (f"旧版 Office 格式（{path.suffix}）无法解析，需要用户先另存为 "
                    f".xlsx/.docx 后再用 parse_document 读取")
        return hint
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"读取失败：{e}")
    t.read_set.add(str(path))
    try:  # 记下"读的是哪一版"：写之前若文件被改过（别的会话/构建产出），要求重读
        st = path.stat()
        t.read_stamp[str(path)] = (st.st_mtime, st.st_size)
    except OSError:  # noqa: BLE001
        pass
    # 分页读取（offset/limit 按行）：大文件以前只能拿到前 16000 字符，
    # 想看第 600 行只能反复整读、反复被截断，最后误判「读不到 → 不能动手」。
    total_lines = text.count("\n") + 1
    raw_off = args.get("offset")
    raw_lim = args.get("limit")
    if raw_off is not None or raw_lim is not None:
        try:
            start = max(1, int(raw_off or 1))
            limit = int(raw_lim) if raw_lim else 400
        except (TypeError, ValueError):
            raise ToolError("offset/limit 必须是整数")
        limit = max(1, min(limit, 2000))
        lines = text.splitlines()
        chunk = lines[start - 1: start - 1 + limit]
        if not chunk:
            return f"(第 {start} 行超出文件末尾：共 {total_lines} 行)"
        head = f"[第 {start}-{start + len(chunk) - 1} 行 / 共 {total_lines} 行]"
        body = "\n".join(f"{start + i:>5}| {ln}" for i, ln in enumerate(chunk))
        tail = ""
        if start - 1 + len(chunk) < total_lines:
            tail = f"\n...[还有 {total_lines - (start - 1 + len(chunk))} 行，用 offset={start + len(chunk)} 继续读]"
        return f"{head}\n{body}{tail}"
    if len(text) > MAX_READ:
        return (text[:MAX_READ]
                + f"\n...[截断，共 {len(text)} 字符 / {total_lines} 行；"
                  f"用 offset+limit 按行读后面的内容，例如 offset={text[:MAX_READ].count(chr(10)) + 10}&limit=200]")
    return text


async def list_dir(args: dict, t: ToolContext) -> str:
    p = _resolve(t.workspace, args.get("path", "."), t.permission_scope)
    if not p.exists():
        raise ToolError(f"路径不存在：{args.get('path', '.')}")
    if p.is_file():
        return f"{args.get('path', '.')} 是文件"
    entries = sorted(os.listdir(p))
    return "\n".join(
        (e + "/") if (p / e).is_dir() else e for e in entries
    ) or "(空目录)"


def _glob_match(rel: str, pattern: str) -> bool:
    """把 glob 通配（含 `**`）匹配到相对路径，语义贴近 pathlib 的 glob。

    只对「已收集到内存的相对路径」做匹配，不触发任何磁盘遍历——这样大目录
    （node_modules 等）在遍历前就能被 `_SKIP_DIRS` 剪掉，而不是先 glob 全树再过滤。
    """
    import fnmatch
    pat = pattern.replace("\\", "/").strip()
    if pat.startswith("./"):
        pat = pat[2:]
    psegs = [s for s in pat.split("/") if s != ""]
    rsegs = [s for s in rel.replace("\\", "/").split("/") if s != ""]

    def walk(pi: int, ri: int) -> bool:
        if pi == len(psegs):
            return ri == len(rsegs)
        seg = psegs[pi]
        if seg == "**":
            if pi == len(psegs) - 1:  # 末尾 ** 命中剩余全部
                return True
            for k in range(ri, len(rsegs) + 1):
                if walk(pi + 1, k):
                    return True
            return False
        if ri >= len(rsegs):
            return False
        if fnmatch.fnmatchcase(rsegs[ri], seg):
            return walk(pi + 1, ri + 1)
        return False

    return walk(0, 0)


def _is_protected_rel(rel: str) -> bool:
    return any(rel == pp or rel.startswith(pp + "/") for pp in PROTECTED_PREFIXES)


def _rel_from(ws: Path, p: Path) -> str | None:
    try:
        return str(p.relative_to(ws)).replace("\\", "/")
    except ValueError:  # 工作区外（理论上 _resolve 已挡）
        return None


async def glob(args: dict, t: ToolContext) -> str:
    pattern = args.get("pattern", "*")

    def _glob_sync() -> list[str]:
        # 关键优化：先按 _SKIP_DIRS 剪枝遍历，再对相对路径做匹配。
        # 旧写法 `workspace.glob(pattern)` 会先老实走完 node_modules/dist/.ga-dev
        # 整棵树（实测近 2GB / 数万文件）才在结果里过滤噪音目录——项目一大就卡死。
        out: list[str] = []
        base = t.workspace
        if not base.exists():
            return out
        # 模式最前面的字面量目录能直接限定遍历起点（如 src/**.ts 只走 src）
        first = pattern.replace("\\", "/").split("/")[0]
        if first and first not in ("**", "*") and "*" not in first and "?" not in first:
            cand = base / first
            if cand.exists():
                base = cand if cand.is_dir() else cand.parent
        if base.is_file():
            rel = _rel_from(t.workspace, base)
            if rel is not None and _glob_match(rel, pattern) and not _is_protected_rel(rel):
                out.append(rel)
            return out
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in files:
                p = Path(dirpath) / name
                rel = _rel_from(t.workspace, p)
                if rel is None or _is_protected_rel(rel):
                    continue
                if _glob_match(rel, pattern):
                    out.append(rel)
        return out

    results = await asyncio.to_thread(_glob_sync)
    return "\n".join(sorted(results)) if results else "(无匹配)"


def _read_text_if_text(p: Path) -> list[str] | None:
    """文件是文本才返回行列表。二进制/超大文件返回 None。

    只靠后缀白名单不够：项目里 release/engine/*.dll 会让 grep 结果全是二进制垃圾，
    模型据此误判"找到了"，白烧好几轮工具调用。这里做一次 NUL 字节探测。

    性能：一次 open 读全量再判 NUL（此前先 stat + open 读头 4KB 判 NUL，
    再 open 第二次读全文——每个文件两次 IO；大目录里这是主要耗时来源）。
    """
    try:
        if p.stat().st_size > _GREP_MAX_FILE:
            return None
        with p.open("rb") as f:
            data = f.read()
        if b"\x00" in data:  # 含 NUL 即视作二进制
            return None
        return data.decode("utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001 - 权限/编码/IO 问题一律跳过
        return None


def _grep_sync(ws: Path, rx: re.Pattern, limit: int, root: Path | None = None,
               context: int = 0, include: str | None = None,
               scope: str = "workspace") -> str:
    """同步扫描（在线程池执行）：os.walk 剪枝噪音目录，避免遍历 node_modules。

    root：只在指定子目录/单文件里搜（用户报某个文件的行，别再全库扫一遍）。
    context：命中行上下各带 N 行（模型要「看这段代码」时不必再整读大文件）。
    include：只搜匹配该通配的文件名，如 \"*.ts\"（对应 grep --include）。
    """
    out: list[str] = []
    scanned = skipped = 0
    base = root or ws
    files_iter: list[Path] = []
    if base.is_file():
        files_iter = [base]
    else:
        for dirpath, dirs, files in os.walk(base):
            # 工作区根下的运行时/开发副产物（.ga/.ga-dev/.trace…）即使没列进 _SKIP_DIRS 也剪掉
            if Path(dirpath) == ws:
                dirs[:] = [d for d in dirs
                           if d not in _SKIP_DIRS and not d.startswith(_ROOT_NOISE_PREFIXES)]
            else:
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in files:
                files_iter.append(Path(dirpath) / name)
    incs = [x for x in (include or "").split("|") if x]

    # 先做便宜的路径级过滤（后缀 / include / 受保护），再并发读取沉重的文件内容
    candidates: list[tuple[Path, str]] = []
    for p in files_iter:
        if p.suffix.lower() in _BINARY_EXT:
            skipped += 1
            continue
        if incs and not any(p.match(x) for x in incs):
            continue
        rel = _rel_from(ws, p)
        if rel is None:
            # 全部权限下允许搜工作区外的 root：相对路径退回绝对路径参与输出
            if scope == "full":
                rel = str(p).replace("\\", "/")
            else:
                continue
        if _is_protected_rel(rel):
            continue
        candidates.append((p, rel))

    def _scan_lines(rel: str, lines: list[str]) -> bool:
        """扫描一个文件的行；返回 True 表示已到上限（可提前收工）。"""
        nonlocal scanned
        scanned += 1
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                if context > 0:
                    lo = max(1, i - context)
                    hi = min(len(lines), i + context)
                    out.append(f"{rel}:{i}: {line}")
                    for j in range(lo, hi + 1):
                        if j == i:
                            continue
                        out.append(f"{rel}:{j}-    {lines[j - 1]}")
                    out.append("--")
                else:
                    out.append(f"{rel}:{i}: {line}")
                if len(out) >= limit:
                    return True
        return False

    # 文件读取是唯一的重 IO：线程池并发预读一批，主线程按原顺序逐文件落结果，
    # 输出顺序与命中上限行为都不变，大项目下比纯串行逐文件读快一个量级。
    import concurrent.futures as _cf
    workers = min(16, (os.cpu_count() or 4) * 2)
    batch = 48
    with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(candidates), batch):
            chunk = candidates[start:start + batch]
            contents = list(pool.map(lambda pr: _read_text_if_text(pr[0]), chunk))
            for (_p, rel), lines in zip(chunk, contents):
                if lines is None:
                    skipped += 1
                    continue
                if _scan_lines(rel, lines):
                    return "\n".join(out) + (
                        f"\n...[已达到上限 {limit} 条，可缩小 path 或加更精确的 pattern]")
    if out:
        return "\n".join(out)
    return (f"(无匹配：已扫描 {scanned} 个文本文件，跳过 {skipped} 个二进制/超大文件；"
            f"node_modules、构建产物、游戏引擎生成目录等噪音目录已自动剪枝。"
            f"若确需在这些目录里搜，请把 path 指到具体子目录。"
            f"换个 pattern 或在 path 里指定子目录，别重复同样的搜索)")


async def grep(args: dict, t: ToolContext) -> str:
    pattern = args.get("pattern", "")
    if not pattern:
        raise ToolError("缺少 pattern")
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"正则错误：{e}")
    limit = args.get("limit", 50)
    try:
        limit = max(1, min(int(limit), 400))
    except (TypeError, ValueError):
        limit = 50
    try:
        ctx = max(0, min(int(args.get("context") or 0), 20))
    except (TypeError, ValueError):
        ctx = 0
    root = None
    if args.get("path"):
        root = _resolve(t.workspace, str(args["path"]), t.permission_scope)
        if not root.exists():
            raise ToolError(f"路径不存在：{args['path']}")
    # 真凶修复：此前同步 rglob+逐文件读取在事件循环线程执行，
    # 扫到大目录时整个服务冻结（表现为打印中断/切页卡住）——移入线程池
    include = str(args.get("include") or "").strip() or None
    return await asyncio.to_thread(_grep_sync, t.workspace, rx, limit, root, ctx, include,
                                   t.permission_scope)


async def write_file(args: dict, t: ToolContext) -> str:
    await t.run_ctx.fencing.assert_valid() if t.run_ctx.fencing else None
    if "path" not in args or not str(args.get("path") or "").strip():
        raise ToolError("缺少 path 参数（工作区内相对路径）")
    if "content" not in args:
        raise ToolError("缺少 content 参数（要写入的文件正文）")
    rel = args["path"]
    _check_protected(rel)
    content = args.get("content") or ""
    if not isinstance(content, str):
        content = str(content)
    mode = str(args.get("mode") or "write").strip().lower()
    if mode not in ("write", "append"):
        raise ToolError("mode 只能是 write（默认，整体写入/覆盖）或 append（追加）")
    path = _resolve(t.workspace, rel, t.permission_scope)
    key = str(path)
    existed = path.exists()
    if existed:
        size = path.stat().st_size
        # 整体覆盖已有内容：必须先读当前版本，否则等于闭眼把别人/别轮次的改动冲掉
        # （实测过：整段重写把用户的修复清了）。append 只往后加、不清空原内容，
        # 不需要先读——大文档分块写入正是靠 append 一段段续写。
        if mode == "write" and size > 0 and key not in t.read_set:
            raise ToolError(
                f"未读不许写：{rel} 已存在（{size} 字节）。先 read_file 看清当前内容再改"
                "（要整体重写也先读一遍，可以 offset/limit 只看开头）；"
                "若确认是新建文件，请用工作区里不存在的路径。")
        stamp = t.read_stamp.get(key)
        now = (path.stat().st_mtime, size)
        if mode == "write" and size > 0 and stamp and stamp != now:
            raise ToolError(
                f"文件在你读过之后已经被改过：{rel}（大小/时间变了，可能是别的会话或构建产出）。"
                "请重新 read_file 拿最新内容，再决定怎么改——不要拿旧内容覆盖。")
    prev_size = path.stat().st_size if existed else 0
    if prev_size + len(content.encode("utf-8")) > MAX_WRITE:
        raise ToolError("单文件写入超过 2MB 限制")
    path.parent.mkdir(parents=True, exist_ok=True)
    _snapshot_before_write(t, rel, "write_file")
    if mode == "append" and existed:
        with path.open("a", encoding="utf-8") as f:
            f.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    _restamp_after_write(t, key, path)
    total = path.stat().st_size
    if mode == "append" and existed:
        return f"已追加写入 {rel}（+{len(content)} 字符，现共 {total} 字节）"
    return f"已写入 {rel}（{'覆盖' if existed else '新建'} {len(content)} 字符）"


async def edit_file(args: dict, t: ToolContext) -> str:
    await t.run_ctx.fencing.assert_valid() if t.run_ctx.fencing else None
    rel = args["path"]
    _check_protected(rel)
    path = _resolve(t.workspace, rel, t.permission_scope)
    if not path.exists():
        raise ToolError(f"文件不存在：{rel}（新建请用 write_file）")
    key = str(path)
    if key not in t.read_set:
        raise ToolError(f"未读不许写：请先 read_file {rel}（要看你要替换的那几行，不用整读）")
    st_now = path.stat()
    stamp = t.read_stamp.get(key)
    if stamp and stamp != (st_now.st_mtime, st_now.st_size):
        raise ToolError(
            f"文件在你读过之后已经被改过：{rel}（大小/时间变了，可能是别的会话或构建产出）。"
            "请重新 read_file 确认现状再改。")
    old = args["old_string"]
    new = args.get("new_string", "")
    text = path.read_text(encoding="utf-8", errors="replace")
    cnt = text.count(old)
    if cnt == 0:
        raise ToolError("old_string 未命中，请确认内容一致")
    if cnt > 1:
        raise ToolError(f"old_string 命中 {cnt} 处，不唯一，请扩大上下文")
    text = text.replace(old, new, 1)
    _snapshot_before_write(t, rel, "edit_file")
    path.write_text(text, encoding="utf-8")
    _restamp_after_write(t, key, path)
    return f"已修改 {rel}（1 处替换）"


def _effective_whitelist(ws: Path) -> set[str]:
    """全局白名单 + 项目级扩展（工作区 .trace/allow_commands.txt，每行一个命令头）。
    供项目声明自己的服务重启/构建脚本等安全命令。"""
    allowed = set(COMMAND_WHITELIST)
    try:
        f = ws / ".trace" / "allow_commands.txt"
        if f.is_file():
            for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                raw = ln.strip()
                if not raw or raw.startswith("#"):
                    continue
                # 保留外层引号给 cmd_head：带空格的路径靠引号才能整体解析
                head = cmd_head(raw)
                if head:
                    allowed.add(head)
    except Exception:  # noqa: BLE001 - 读取失败只影响扩展，不影响基础白名单
        pass
    return allowed


def cmd_head(cmd: str) -> str:
    """取命令头（可执行文件名，去掉 .exe/.cmd/.bat），兼容引号路径与绝对路径。

    `"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --headless ...`
    旧写法 `cmd.strip('"').split()[0]` 会得到 `C:\\Program` → `program`，
    于是正常启动浏览器被误报「命令不在白名单：program」（用户截图复现）。
    """
    s = str(cmd or "").strip()
    if not s:
        return ""
    if s[0] in "\"'":
        q = s[0]
        end = s.find(q, 1)
        raw = s[1:end] if end != -1 else s[1:]
    else:
        raw = re.split(r"\s+", s, maxsplit=1)[0]
    raw = raw.strip().strip("\"'")
    name = Path(raw).name.lower()
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name or raw.lower()


# 模型最常混用的命令 → 本系统等价写法（白名单拒绝时直接给出，省掉一轮试探）
_WIN_EQUIV = {
    "ls": "dir", "ll": "dir", "cat": "type", "pwd": "echo %CD%",
    "grep": "用 grep 工具（shell 里没有 grep，findstr 参数完全不同）", "which": "where",
    "cp": "copy", "mv": "move", "rm": "del",
    "head": "more（或 node -e 读前 N 行）",
    "tail": "powershell -c \"Get-Content <文件> -Tail 20\"（或 node -e）",
    "sed": "node -e / powershell（cmd 没有 sed）",
    "awk": "node -e / powershell（cmd 没有 awk）",
    "touch": "type nul > <文件>", "sleep": "timeout /t N", "export": "set",
    "chmod": "（Windows 没有 chmod 概念）",
}
_POSIX_EQUIV = {
    "dir": "ls", "type": "cat", "where": "which", "copy": "cp", "move": "mv",
    "del": "rm", "erase": "rm", "findstr": "grep", "cls": "clear", "ren": "mv",
}
# 允许直接用 Unix 写法：Windows 上自动换成本系统等价命令（不再拒绝一轮）
# PowerShell 自带 cp/mv/rm/ls/cat 同名别名，而且认正斜杠路径，比 cmd 的 copy 更省事：
# 这类命令整体交给 powershell 跑；其余给 cmd 等价头。
# PowerShell 专有命令原样交给 powershell.exe 执行（cmd 里根本没有这些命令）。
# 实测：模型在中文项目里写 `Get-ChildItem -Path a,b` 连吃两次 exit=255，白烧两轮。
_WIN_PS_WRAP = {"cp", "mv", "rm", "ls", "cat", "ll", "touch", "pwd",
                "get-childitem", "gci", "get-content", "select-string", "sls",
                "get-item", "resolve-path"}
_WIN_TRANSLATE = {"which": "where", "clear": "cls"}
# 这些没有可靠的 Windows 对应写法（findstr 的语法和 grep 完全不同，硬换只会产出垃圾结果）：
# grep/rg 直接改走内置 grep 工具（不失败、不白烧轮次）；sed/awk 才需要模型换写法。
_SEARCH_TOOL_HEADS = {"grep", "rg", "egrep", "fgrep", "ack", "ag"}
_NO_EQUIV_HEADS = _SEARCH_TOOL_HEADS | {"sed", "awk"}

# ---------- 递归扫描类 shell 命令：单独限时（实测会卡住整条 run）----------
# 现场：`powershell -Command "Get-ChildItem -Recurse -File -Include *.json,*.py,*.png … | Sort-Object …"`
# 在工作区根上跑，几分钟不出结果，界面一直挂「执行中…」。TOOL_TIMEOUT_S（默认 300s）是给
# 构建/测试留的，扫描用不着这么久——扫这么久基本等于「范围写错了」。所以扫这类命令改用
# SEARCH_TIMEOUT_S（默认 60s）掐断，并在超时文案里直接给替代工具。
_SCAN_HEADS = {
    "get-childitem", "gci", "dir", "ls", "tree", "find", "findstr", "select-string",
    "sls", "where", "du", "fd", "rg", "grep", "robocopy", "xcopy",
}
# 这些命令本身就是递归的（不需要 -Recurse 也扫整棵子树）：tree /f、find . -name、du -sh、fd、rg
_RECURSE_BY_DEFAULT = {"find", "fd", "du", "tree", "rg"}
# 显式限定只看一层时不算扫描：find . -maxdepth 1 …、fd -d 1 …
_SHALLOW_FLAG = re.compile(
    r"(?<![\w-])(-maxdepth\s+1|-depth\s+1|--max-depth[= ]?1|--maxdepth\s+1|-d\s+1)(?![\w-])", re.I)
_RECURSE_MARK = re.compile(r"(?<![\w-])(-recurse|-r|/s|/f|/r|/e)(?![\w-])", re.I)


def _is_scan_command(cmd: str) -> bool:
    """判断是不是「递归扫目录 / 全库找文件」这类慢命令。

    只看两点：命令头（前 6 个 token，允许 powershell -Command "…" 这种包一层）是不是列举/查找类，
    以及这条命令是不是递归的（显式 -Recurse / -r / /s / /f / /r / /e / **，或 tree、find、du、fd、rg
    这种天生递归的）。单层列举（dir、ls、Get-ChildItem web）不算——那是快的。
    """
    tokens = [t.strip("\"'").lower() for t in re.split(r"\s+", cmd.strip().strip("\"'")) if t.strip()]
    head = tokens[:6]
    if not any(t in _SCAN_HEADS for t in head):
        return False
    if _SHALLOW_FLAG.search(cmd):
        return False
    if any(t in _RECURSE_BY_DEFAULT for t in head):
        return True
    return bool(_RECURSE_MARK.search(cmd)) or "**" in cmd


def _no_equiv_message(head: str, cmd: str) -> str:
    m0 = re.search(r'["\']([^"\']{1,80})["\']', cmd)
    pat = m0.group(1) if m0 else "<关键词>"
    incs = re.findall(r'--include[=\s]+["\']?([^\s"\']+)', cmd)
    call = (f'grep(pattern="{pat}", path="."'
            + (f', include="{incs[0]}"' if incs else "") + ", context=2)")
    return (f"别用 shell 的 {head}：cmd.exe 没有它，findstr 的语法也完全不同。直接照抄这个工具调用——\n"
            f"  {call}\n"
            "找文件用 glob(pattern=...)；看某几行用 read_file(path, offset, limit)；读整篇用 read_file(path)。"
            "shell 只留给构建/测试/起服务。")


def _search_args(cmd: str, t: "ToolContext") -> dict | None:
    """把 `grep -rn "关键词" src --include=*.ts` 解析成内置 grep 工具的参数。解析不出就返回 None。"""
    m0 = re.search(r'["\']([^"\']{1,200})["\']', cmd)
    if not m0:
        return None
    args: dict = {"pattern": m0.group(1), "context": 1, "limit": 60}
    incs = re.findall(r'--include[=\s]+["\']?([^\s"\']+)', cmd)
    if incs:
        args["include"] = "|".join(dict.fromkeys(incs))
    rest = (cmd[:m0.start()] + " " + cmd[m0.end():]).strip().strip('"')
    for tok in re.split(r"\s+", rest)[1:]:
        tok = tok.strip('"\'')
        if not tok or tok.startswith("-") or tok in ("|", "&&", ";"):
            continue
        if tok in (".", ".."):
            args["path"] = "."
            break
        if (t.workspace / tok).exists():
            args["path"] = tok
            break
    return args


def _dangerous(cmd: str) -> str | None:
    """只拦「后果不可逆」的动作（删盘/格式化/关机/远程执行/强推），与命令写法无关。"""
    low = cmd.lower()
    for pat, why in DANGEROUS_PATTERNS:
        if re.search(pat, low):
            return why
    return None


# 浏览器是「用户自己的窗口」，不是本任务的资源。
# 实测（2026-11-05 用户截图）：组长每次跑 CDP 自测前都先
#   taskkill /IM chrome.exe /F >nul 2>&1 & node target/_cdp_xxx.mjs ...
# 想「清场再起无头浏览器」，结果把用户正开着的循迹界面一起关掉（chrome.exe 是全局同名）。
# 这类命令不管加多少引号/大小写/别名都会命中下面这张表，直接拒绝；并给出正确做法：
# 清的是「自己起的那个调试实例」（限定 --remote-debugging-port / --user-data-dir），
# 而不是按进程名全杀。
_BROWSER_NAMES = r"(?:chrome|chromium|msedge|brave|firefox|iexplore)"
_GLOBAL_BROWSER_KILL = re.compile(
    rf"""(?ix)
    (?:
        # Windows：taskkill 按映像名(/IM) 或按筛选器(/FI IMAGENAME eq) 一次杀光同名进程
        \btaskkill\b[^\n&|;]*?
            (?: /im\s*["']?\s*{_BROWSER_NAMES}(?:\.exe)?\b
              | /fi\s+["']?[^\n&|;]*?image(?:name)?\s*(?:eq|=)\s*["']?\s*{_BROWSER_NAMES}(?:\.exe)?
            )
      # Unix：killall / pkill / kill $(pidof|pgrep ...)
      | \bkillall\b[^\n&|;]*\b{_BROWSER_NAMES}(?:\.exe)?\b
      | \bpkill\b[^\n&|;]*?\b{_BROWSER_NAMES}(?:\.exe)?\b
      | \bkill\b[^\n&|;]*\$\(\s*(?:pidof|pgrep)\b[^\n&|;]*\b{_BROWSER_NAMES}(?:\.exe)?\b
      # PowerShell：Stop-Process -Name / Get-Process <name> | Stop-Process
      | \bstop-process\b[^\n&|;]*?(?:-\s*name|/name)\s*["']?\s*{_BROWSER_NAMES}(?:\.exe)?\b
      | \bget-process\b[^\n]*?\b{_BROWSER_NAMES}(?:\.exe)?\b[^\n]*?\|\s*(?:stop-process|kill)\b
      # wmic：wmic process where name='chrome.exe' delete
      | \bwmic\b[^\n&|;]*?process[^\n&|;]*?name\s*=\s*["']?\s*{_BROWSER_NAMES}(?:\.exe)?["']?[^\n&|;]*?delete\b
    )
    """
)

_BROWSER_KILL_MSG = (
    "这条命令被拦：它会按「进程名」杀掉所有同名进程（taskkill /IM xxx /F、killall、pkill、"
    "Stop-Process -Name xxx），连用户自己正开着的浏览器/别的工作一起关掉。"
    "浏览器是用户的窗口，不属于本次任务的资源，不能这样清场。\n"
    "正确做法（二选一）：\n"
    "  · 只清你自己起的那个调试实例：先用 --remote-debugging-port=<端口> 或 "
    "--user-data-dir=<你的 profile 目录> 起无头浏览器，收尾时按「端口」找进程杀"
    "（如 netstat 找到 PID 再 taskkill /F /PID <pid>），不要按名字全杀；\n"
    "  · 更省事：用 screenshot_page 工具截图（它自己起独立 profile 的无头浏览器，跑完自动清理），"
    "根本不用手动起/杀 Chrome。"
)


def _browser_global_kill(cmd: str) -> bool:
    return bool(_GLOBAL_BROWSER_KILL.search(cmd or ""))


def _translate_head(cmd: str, head: str) -> str:
    """把模型常用的 Unix 命令头换成当前平台的等价写法（只换第一个词，其余原样）。
    实测反馈：组长习惯写 cp，被白名单拒掉后要靠 node -e 绕道，白烧好几轮。"""
    if PLATFORM != "windows":
        return cmd
    if head.lower() in _WIN_PS_WRAP:
        return 'powershell -NoProfile -Command "' + cmd.replace('"', '\\"') + '"'
    repl = _WIN_TRANSLATE.get(head)
    if not repl:
        return cmd
    return repl + cmd[len(head):]


def _autofix_shell(cmd: str) -> tuple[str, list[str], str | None, tuple[str, int] | None]:
    """把 bash 习惯静默改写成当前 shell 能跑的等价命令。

    实测：模型写 `grep -rl "x" . | head -50`、`... 2>/dev/null`，在 cmd 下不是「报错」而是
    「静默跑错/整条失败」，每次都要用户在界面上看到一条红叉。能改的就地改掉 + 把改了什么告诉模型，
    改不动的（grep/sed/awk 这种没有对应写法的）才拒绝，并直接指向本进程的现成工具。
    返回 (改写后的命令, 改写说明, 拒绝原因 or None, (head|tail, N) or None)。
    """
    if PLATFORM != "windows":
        return cmd, [], None, None

    body = re.sub(r'"[^"]*"|\'[^\']*\'', "", cmd)  # 去掉引号内容，别误伤 node -e "..."

    head = cmd_head(cmd)
    if head in _NO_EQUIV_HEADS:
        return cmd, [], _no_equiv_message(head, cmd), None
    # heredoc（python - <<'EOF' … EOF）：cmd 根本不支持，实测被后面的 `;`→`&&` 改写弄成
    # 一条语法正确的坏命令（exit=1、模型看不出为什么），再白烧一轮去写脚本文件。
    # 这里直接拒绝，并给出一步到位的替代写法。
    if re.search(r"<<-?\s*['\"]?[A-Za-z_][\w]*['\"]?\s*\r?\n", cmd):
        return cmd, [], (
            "cmd.exe 不支持 heredoc（<<EOF）。把脚本写到 .trace/tmp/<名字>.py 再用 run_command "
            "执行（python .trace/tmp/x.py），或改成 `python -c \"一行到底\"`（不要带换行）。"), None
    if re.search(r"\$\(|`[^`]*`", body):
        return cmd, [], (
            "cmd.exe 不支持 $(...) / 反引号命令替换：改用 node -e \"...\" 或 python -c \"...\" 一次算出来，"
            "或拆成两步走。"), None

    fixed, notes = cmd, []
    limit: tuple[str, int] | None = None
    m = re.search(r"\|\s*(head|tail)\b(?:\s+-n?\s*|\s+)(\d+)?\s*$", body)
    if m:
        limit = (m.group(1), int(m.group(2) or 10))
        fixed = fixed[:m.start()].rstrip()
        notes.append(f"去掉 `| {limit[0]}`，由工具截取{'前' if limit[0] == 'head' else '后'} {limit[1]} 行")
    # Git-Bash 路径 /c/Users/... → C:/Users/...（模型从 bash 习惯带过来的，cmd 不认）
    m = re.search(r'(?<![\w:.])/([A-Za-z])/(?=[^\s"\']|["\'])', body)
    if m:
        fixed = re.sub(r'(?<![\w:.])/([A-Za-z])/',
                       lambda mt: mt.group(1).upper() + ":/", fixed)
        notes.append("Git-Bash 路径 /c/... 改成 C:/...")
    if "/dev/null" in fixed:
        fixed = re.sub(r"2\s*>\s*/dev/null", "2>nul", fixed)
        fixed = fixed.replace("/dev/null", "nul")
        notes.append("丢弃输出改成 2>nul")
    if re.search(r";\s*\S", re.sub(r'"[^"]*"|\'[^\']*\'', "", fixed)):
        fixed = re.sub(r";\s*", " && ", fixed)
        notes.append("`;` 串联改成 &&")
    if re.search(r"\$\{?[A-Za-z_]", fixed):
        fixed = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", r"%\1%", fixed)
        notes.append("$VAR 改成 %VAR%")
    return fixed, notes, None, limit


def _whitelist_error(head: str, ws: Path) -> str:
    """白名单拒绝时给「可以立刻照做的下一步」，而不是让模型原地重试同一个错命令。"""
    equiv = (_WIN_EQUIV if PLATFORM == "windows" else _POSIX_EQUIV).get(head)
    tip = f"本系统等价写法：{equiv}。" if equiv else ""
    allowed = sorted(_effective_whitelist(ws))
    rows = [", ".join(allowed[i:i + 8]) for i in range(0, len(allowed), 8)]
    return (
        f"命令不在白名单：{head}。{tip}\n"
        f"常用写法（照抄）：{env_examples()}\n"
        f"可用命令头（{len(allowed)} 个）：\n  " + "\n  ".join(rows) + "\n"
        "清单外的命令一律拒绝；项目专属命令写进工作区 .trace/allow_commands.txt（每行一个命令头）。"
        "不要原样重试同一条被拒命令，改用上面的写法或 fs 工具。"
    )


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

_SKILL_SCOPE_LABELS = SKILL_SCOPE_LABELS


def _find_skill_file(name: str, t: ToolContext) -> tuple[Path, str] | None:
    """按优先级在多个技能根里找 <name>.md 或 <name>/SKILL.md。"""
    if not _SKILL_NAME_RE.match(name):
        return None
    roots: list[tuple[str, Path]] = list(t.skill_roots) if t.skill_roots else [
        ("project", t.workspace / ".trace" / "skills")]
    for scope, root in roots:
        try:
            for cand in (root / f"{name}.md", root / name / "SKILL.md"):
                if cand.is_file():
                    return cand, scope
        except OSError:
            continue
    return None


async def load_skill(args: dict, t: ToolContext) -> str:
    """按需加载技能正文（渐进式披露的第二步：清单只给名称+描述，正文在这里）。

    搜索范围：项目 .trace/skills → 全局技能目录 → 用户在技能面板添加的文件夹。
    """
    name = str(args.get("name", "")).strip()
    if not _SKILL_NAME_RE.match(name):
        raise ToolError("技能名只允许字母/数字/下划线/中划线（1-64 字符）")
    hit = _find_skill_file(name, t)
    if hit is None:
        raise ToolError(f"技能不存在：{name}（可用清单见上下文「可用技能」）")
    path, scope = hit
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ToolError(f"技能文件读取失败：{e}")
    if len(text) > 12000:
        text = text[:12000] + f"\n...[截断，共 {len(text)} 字符]"
    head = f"[技能 {name} · 来源：{_SKILL_SCOPE_LABELS.get(scope, scope)} · {path.name}]\n\n"
    # 配音类技能正文里写着 python 管线（cv_gen.py / gen-cv-indextts.py）——
    # 那是给人/给脚本用的老路。平台规则是「配音一律直接调用 generate_audio」，
    # 不先把这条压上去，模型读完技能就会照着文档自己去起服务、跑脚本（实测 2026-10-28）。
    if any(k in text for k in ("cv_gen", "gen-cv-indextts", "IndexTTS", "配音")):
        head += (
            "⚠️ 平台调用规则（优先于本技能正文里的命令行）：在 循迹（Trace） 里生成配音一律直接调用 "
            "**generate_audio** 工具——语音服务由它内部自动拉起，不要自己 "
            "start_service 起 IndexTTS2、不要 curl 127.0.0.1:8860、不要跑 cv_gen.py / "
            "gen-cv-indextts.py / cv-presets.py。需要新音色时给 generate_audio 传 "
            "design_desc（音色描述）+ preset_name（预设名），它会先造音色再合成。"
            "本技能里的 python 命令只用于用户明确要求「按之前 python 脚本的方式」时，"
            "或作为理解数据流/参数口径的参考，不是让你去执行的。\n\n")
    return head + text


# 一次 run 最多允许检索几次历史。实测 2026-10-28 run 27a8a2bf：模型丢了任务后
# 连搜 78 次（「出战」「操作失败」「11:23」「山门小径」…几乎全是无匹配），
# 每次结果都一样却继续换关键词碰运气。这个工具本身没有「每次调用必须不同」的
# 死循环特征，runtime 的三连相同调用检测抓不到，只能在这里硬限。
SEARCH_HISTORY_MAX_PER_RUN = 5


async def save_env_fact(args: dict, t: ToolContext) -> str:
    """把「试过才知道、下次还要用」的项目入口事实写进 .trace/env.json。

    为什么给它一个口子：登录账号、起服务命令、进哪个页面这类事实，平台没法从报错文案里
    sniff 出来（环境事实那层只认「命令不存在 / 不是 git 仓库」这种硬信号）。
    2026-10-28 那次会话光「怎么登进去」就烧了 6 分钟，下个 run 又得从头撞——
    一次确认就该一次写死。
    """
    key = str(args.get("key") or "").strip().lower().replace(" ", "_")
    value = str(args.get("value") or "").strip()
    if not key or not value:
        raise ToolError("key 和 value 都要填（例：key=test_account，value=测试号 admin / 密码 123456）")
    if not re.fullmatch(r"[a-z0-9_.-]{1,40}", key):
        raise ToolError("key 只能用字母/数字/下划线/点/连字符（≤40 字），例：test_account、login_entry")
    if len(value) > 300:
        raise ToolError("value 超过 300 字：入口事实要一两句能用；长篇步骤请写进 .trace/notes 经验（playbook）")
    if t.workspace is None:
        raise ToolError("本次任务没有工作区，没法记录环境事实")
    force = bool(args.get("force"))
    existing = envfacts.load(t.workspace).get(key)
    if existing is not None and str(existing) != value and not force:
        return (f"没覆盖：{key} 已经记着「{existing}」。"
                "确认它变了（比如换了端口/换了账号）就带 force=true 重写一次。")
    changed = envfacts.record(t.workspace, {key: value}, force=force)
    if not changed:
        return f"{key} 本来就是这个值，没重写（文件：{envfacts.ENV_FILE}）"
    return (f"已记入 {envfacts.ENV_FILE}：{key}={value}\n"
            "从下一个 run 起，这段会出现在上下文「环境事实与入口事实」里，直接照用、不用再试探。")

async def memory_read(args: dict, t: ToolContext) -> str:
    """项目记忆全文：按 8 位 id 或关键词取 1-3 条（上下文里注入的只是标题索引）。"""
    from game_agent.orchestration import project_memory as pm

    q = str(args.get("query") or "").strip()
    raw_ids = args.get("ids") or []
    ids = [str(x).strip() for x in raw_ids if str(x).strip()] if isinstance(raw_ids, list) else []
    if not q and not ids:
        raise ToolError("缺少 query 或 ids（索引里每条记忆前面有 8 位 id）")
    entries = await pm.read_entries(t.run_ctx.project_id, query=q, ids=ids, limit=3)
    if not entries:
        return ("没有匹配的项目记忆。换个关键词再试，或者先看上下文里的「项目记忆索引」确认有哪些条目；"
                "与本轮无关的记忆不要读。")
    blocks: list[str] = []
    for e in entries:
        tags = f" · {'/'.join(e['tags'])}" if e.get("tags") else ""
        blocks.append(f'【{e["id"][:8]}】{e["title"]}{tags}\n{e["body"][:1600]}')
    return "\n\n".join(blocks)


async def search_history(args: dict, t: ToolContext) -> str:
    """会话冷存储检索：只查本会话的**用户消息与助手最终回复**。

    工具调用轨迹（read_file/grep/run_command 及其结果）不写 messages 表，
    所以这里搜不到「我上一步改了什么」——需要现场信息只能重新 read/grep。
    """
    kw = str(args.get("keyword", "")).strip()
    if not kw:
        raise ToolError("缺少 keyword")
    state = t.agent_state.setdefault(
        "search_history", {"count": 0, "seen": {}})
    key = kw.lower()
    if key in state["seen"]:
        return (f"（「{kw}」本次运行已搜过，结果不会变。不要换关键词碰运气：本工具只能搜到"
                "用户消息和助手最终回复，工具调用轨迹不在检索范围。要文件内容请用 read_file/grep。）")
    if state["count"] >= SEARCH_HISTORY_MAX_PER_RUN:
        return (f"（本次运行检索历史已达上限 {SEARCH_HISTORY_MAX_PER_RUN} 次，已停用。"
                "会话里只有用户消息和助手最终回复，反复换关键词不会出现新内容。"
                "现在停止检索：直接按已有信息改代码，或用 ask_user 问用户要复现步骤。）")
    state["count"] += 1
    limit = min(int(args.get("limit", 15)), 30)
    from sqlalchemy import func, select
    from game_agent.db.models import Message
    from game_agent.db.session import SessionLocal
    sid = t.run_ctx.session_id
    if not sid:
        raise ToolError("当前 run 无会话上下文")
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Message).where(
                Message.session_id == _u(str(sid)),
                Message.content.like(f"%{kw}%"),
            ).order_by(Message.created_at.desc()).limit(limit)
        )).scalars().all()
        total = (await s.execute(
            select(func.count()).select_from(Message).where(
                Message.session_id == _u(str(sid)))
        )).scalar_one()
    if not rows:
        state["seen"][key] = 0
        return (f"（无匹配。本会话一共只有 {total} 条用户/助手消息，工具调用过程不计入检索。"
                f"本次已检索 {state['count']}/{SEARCH_HISTORY_MAX_PER_RUN} 次——"
                "不要换关键词重搜，直接 read_file/grep 看现场，或开始改代码。）")
    state["seen"][key] = len(rows)
    out = []
    for m in rows:
        who = getattr(m, "agent_key", None) or getattr(m, "author_type", None) or "?"
        out.append(f"[{who}] {str(m.created_at)[:16]}: {str(m.content)[:300]}")
    return (f"共 {len(rows)} 条（本会话共 {total} 条用户/助手消息；新→旧）：\n"
            + "\n---\n".join(out))


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    """超时后尽力杀干净子进程树（Windows 用 taskkill /T）。"""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _decode_shell_output(raw: bytes) -> str:
    """cmd.exe 的输出是**控制台代码页**编码（中文 Windows 上是 GBK），不是 UTF-8。

    实测（kpgame，全中文路径）：`dir` 列出 `阿汐-ur-yh-02` 回来是 `��ϫ-ur-yh-02`，
    模型读不出文件名，只能改用 list_dir 再查一遍——每个中文项目每次 run 都要多烧几轮。
    先按 UTF-8 严格解（node/python 都按 UTF-8 输出，env 里也强制了），失败再按 GB18030
    （GBK 超集）解 cmd 内建命令的结果。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


async def _run_shell_cmd(cmd: str, cwd: str, timeout_s: float, scan: bool = False) -> tuple[int, str]:
    """异步执行命令，**可被取消**（取消即杀掉整棵子进程树）。

    ⚠️ 不能用 asyncio.create_subprocess_shell：本进程在 Windows 上强制 Selector 事件循环
    （aiomysql 要求，见 main.py），而 SelectorEventLoop 不支持子进程，会抛
    NotImplementedError —— 表现正是「所有 run_command 一律 启动命令失败」。所以用
    subprocess.Popen（同步 API）+ 一个专门的读线程抽输出，事件循环侧用轮询等待。

    为什么不再用 `asyncio.to_thread(_run_shell_blocking, …)`：to_thread 是**不可取消**的。
    run 被取消（用户点停止 / 看门狗掐任务）时，CancelledError 投不进去，那个线程会一直
    阻塞在 proc.communicate 上，run 就成了僵尸（status 永远 running、界面「停止中」却
    还在转）——2026-11-10 实测 `npm build`/`pytest` 卡住就是这样。这里把等待点改成一个
    `await asyncio.sleep` 轮询，取消能立即生效，并顺手 `_kill_tree` 把子进程真正杀掉，
    杜绝孤儿 pytest/构建进程继续占资源。

    scan=True 表示这是递归扫描命令（时限更短）：超时文案换成「缩小范围 / 换内置工具」。

    并发控制：`_SHELL_SEM` 只做并发上限（不是串行锁），一条慢/挂的命令不会连坐别的 run。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    def _timeout_msg(partial: str) -> str:
        if scan:
            return (
                f"递归扫描 {int(timeout_s)}s 内没跑完，已终止（搜索时限 GA_SEARCH_TIMEOUT_S，"
                "和构建/测试用的 GA_TOOL_TIMEOUT_S 分开算）。整库扫一遍几乎总是范围写错了：\n"
                "  · 找文件 → glob(pattern=\"**/*.py\")，自动跳过 node_modules/.git/dist/.venv 等；\n"
                "  · 找内容 → grep(pattern=\"关键词\", path=\"子目录\")，先 list_dir 看上一步该进哪个目录；\n"
                "  · 确实要扫 shell，也必须限定路径（例：Get-ChildItem web -Recurse …），别从工作区根扫起；\n"
                "  · 想按修改时间挑文件，用 glob 拿到清单后 read_file 看几个明确路径，别对着全库排序。"
                + (f"\n[超时前的输出]\n{partial[-2000:]}" if partial else ""))
        return (
            f"命令 {int(timeout_s)}s 内没有结束，已被终止。常驻服务（web/dev server、watch、"
            "游戏进程）不要用 run_command 反复起：改用 start_service 后台启动 → service_logs 看日志 → "
            "screenshot_page 截图 → stop_service 收掉；如果是测试卡住，缩小范围（跑单个用例）或加超时参数。"
            + (f"\n[超时前的输出]\n{partial[-2000:]}" if partial else ""))

    async with _SHELL_SEM:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, creationflags=flags, close_fds=True,
        )
        buf = bytearray()

        def _reader() -> None:
            try:
                assert proc.stdout is not None
                for line in iter(proc.stdout.readline, b""):
                    buf.extend(line)
            except Exception:  # noqa: BLE001 - 读线程异常不影响主流程
                pass
            finally:
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except Exception:  # noqa: BLE001
                    pass

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        try:
            while proc.poll() is None:
                if loop.time() >= deadline:
                    _kill_tree(proc)
                    # 给读线程一点时间收尾已吐出的输出，再交还（partial 让模型有线索）
                    await asyncio.to_thread(reader.join, 5)
                    partial = _decode_shell_output(bytes(buf)).strip()
                    raise ToolError(_timeout_msg(partial))
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            # 关键：取消必须真正杀掉子进程，否则 run 僵尸 + 孤儿 pytest/build 继续跑
            _kill_tree(proc)
            try:
                await asyncio.to_thread(reader.join, 5)
            except Exception:  # noqa: BLE001
                pass
            raise
        # 进程已退出：读线程读到 EOF 后会自然结束，等它把尾部输出抽干净
        await asyncio.to_thread(reader.join, 5)
        return proc.returncode, _decode_shell_output(bytes(buf))


async def run_command(args: dict, t: ToolContext) -> str:
    await t.run_ctx.fencing.assert_valid() if t.run_ctx.fencing else None
    cmd = args.get("command", "").strip()
    if not cmd:
        raise ToolError("缺少 command")
    # ① 搜文本类（grep/rg…）：Windows 没有 grep，与其报错白烧一轮，直接把这条命令改成内置 grep 工具执行
    head0 = cmd_head(cmd)
    if PLATFORM == "windows" and head0 in _SEARCH_TOOL_HEADS:
        gargs = _search_args(cmd, t)
        if gargs:
            try:
                res = await grep(gargs, t)
            except ToolError as e:
                raise ToolError(f"已转成内置 grep 工具执行，但它失败了：{e}")
            return (f"[command rewritten] `{head0} ...` → grep 工具（cmd 里没有 grep/rg）\n"
                    f"参数：{gargs}\n{res}")
        raise ToolError(_no_equiv_message(head0, cmd))
    # ② 危险动作：与语法无关，只看后果（删盘/格式化/关机/远程执行/强推）
    danger = _dangerous(cmd)
    if danger:
        raise ToolError(
            f"这条命令被拦：{danger}。这类操作不可逆，不在自动执行范围内。"
            "如果确实需要，请让人来执行，或改用只影响本项目的写法（限定路径、先备份）。")
    # ②·二 按进程名全杀浏览器/解释器：会连用户自己的窗口一起关掉（实测踩过）。
    if _browser_global_kill(cmd):
        raise ToolError(_BROWSER_KILL_MSG)
    # ②·五 配音路由：生成配音一律直接调 generate_audio 工具，不许自己
    # 起 IndexTTS2 / curl 语音接口 / 跑 cv 脚本。2026-10-28 实测：用户说「配音重新生成」，
    # 模型 load 了项目技能后就照技能里的 python 管线自己 start_service(8860)、写探针脚本合成，
    # 全程没调配音工具。例外：用户明确说「按之前 python 脚本的方式」。
    from game_agent.agents.tools.audio_guard import (
        AUDIO_ROUTE_DENIED, audio_python_allowed, is_audio_script_command,
        is_audio_server_command, script_calls_audio)
    if not audio_python_allowed(t.run_ctx.user_request):
        if is_audio_server_command(cmd) or is_audio_script_command(cmd) \
                or script_calls_audio(cmd, t.workspace):
            raise ToolError(AUDIO_ROUTE_DENIED)
    # ③ bash 习惯就地改写成当前 shell 能跑的写法（/dev/null→2>nul、`;`→&&、`| head -N`→由工具截取…）
    cmd, fix_notes, reject, limit = _autofix_shell(cmd)
    if reject:
        raise ToolError(reject)
    # 用项目解释器替换 `python`（保证在正确的 venv 里跑测试）
    head = cmd_head(cmd)
    allowed = _effective_whitelist(t.workspace)
    if head not in allowed:
        # cp/ls/cat 这类 Unix 习惯：自动换成 copy/dir/type 再跑，而不是让模型原地重试
        fixed = _translate_head(cmd, head)
        if fixed != cmd:
            cmd = fixed
            head = cmd_head(cmd)
            fix_notes.append(f"命令头换成 {head}")
        # 默认 open 策略：不再按白名单拦（语法对不对让 shell 自己说），只有显式 GA_SHELL_POLICY=strict 才拦
        if head not in allowed and SHELL_POLICY == "strict":
            raise ToolError(_whitelist_error(head, t.workspace))
    # 非 git 仓库禁止跑 git：没有 .git 的工作区里 git 只会报错，白烧轮次
    if head == "git" and not (t.workspace / ".git").exists():
        raise ToolError(
            "此工作区不是 git 仓库（没有 .git 目录），不要用 git 命令；"
            "直接按任务读写文件即可，也不要再拿别的命令试探环境。")
    if head == "python" and t.python_bin:
        m = re.match(r'^("?)python\1(?=\s|$)', cmd, flags=re.I)
        if m:
            cmd = f'"{t.python_bin}"' + cmd[m.end():]
    # 递归扫描（Get-ChildItem -Recurse / dir /s / find …）单独限时：几分钟的整库扫描在界面上
    # 就是一条「执行中…」挂着不动，用户看到的就是卡住。搜索用 60s，构建/测试才用 300s。
    scan = _is_scan_command(cmd)
    budget = float(SEARCH_TIMEOUT_S if scan else TOOL_TIMEOUT_S)
    try:
        rc, text = await _run_shell_cmd(cmd, str(t.workspace), budget, scan)
    except ToolError:
        raise
    except asyncio.CancelledError:
        # 取消（用户点停止 / 看门狗）要一路向上抛，交给 orchestrator 落 cancelled 终态，
        # 绝不能在这里吞成「启动命令失败」，否则 run 被取消却报成工具错误。
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"启动命令失败：{type(e).__name__}: {e}")
    lines = text.splitlines()
    if limit and len(lines) > limit[1]:
        keep = lines[:limit[1]] if limit[0] == "head" else lines[-limit[1]:]
        text = "\n".join(keep) + f"\n...[已截取{limit[1]}行 / 共{len(lines)}行]"
    tail = text[-4000:] if len(text) > 4000 else text
    note = ("\n[已自动改写为当前系统可执行的写法：" + "；".join(fix_notes) + f"]\n实际执行：{cmd}"
            if fix_notes else "")
    if scan:
        note += (f"\n[递归扫描：超过 {int(budget)}s 会被终止；"
                 "下次优先用 glob（找文件）/ grep（找内容），它们本来就会跳过 node_modules/.git 等目录]")
    if rc != 0:
        # 命令失败时顺手记一条「本机现实」（cmd 里没 grep、这不是 git 仓库…），
        # 下个 run 的上下文里直接给出，不用再撞一遍。认不准的模式一律不记。
        fresh = envfacts.observe(cmd, text, rc, t.workspace)
        if fresh:
            note += ("\n[已记入环境事实 .trace/env.json：" +
                     "；".join(f"{k}={v}" for k, v in fresh.items()) +
                     "] 后续 run 会直接知道这一点，不必再试探。")
    return f"[exit={rc}]{note}\n{tail}"


async def delete_file(args: dict, t: ToolContext) -> str:
    """删除工作区内的单个文件（受保护路径与目录不支持）。"""
    await t.run_ctx.fencing.assert_valid() if t.run_ctx.fencing else None
    p = str(args.get("path", "")).strip()
    if not p:
        raise ToolError("缺少 path")
    target = _resolve(t.workspace, p, t.permission_scope)
    if not target.exists():
        raise ToolError(f"文件不存在：{p}")
    if target.is_dir():
        raise ToolError(f"只支持删除文件，不支持目录：{p}")
    try:
        rel = str(target.relative_to(t.workspace.resolve())).replace("\\", "/")
    except ValueError:
        # 全部权限下删除工作区外文件：相对路径取绝对路径（快照/审计仍能记下）
        rel = str(target).replace("\\", "/")
    for pref in PROTECTED_PREFIXES:
        if rel == pref or rel.startswith(pref.rstrip("/") + "/"):
            raise ToolError(f"受保护路径，禁止删除：{rel}")
    try:
        _snapshot_before_write(t, rel, "delete_file")
        target.unlink()
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"删除失败：{type(e).__name__}: {e}")
    return f"已删除：{rel}"
