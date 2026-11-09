"""过程临时文件清理：run 收尾时扫掉「只为当轮判断服务」的东西。

两个机制，缺一不可：

1) 登记制（精确）：工具落的临时文件（screenshot_page 的 shot-*.png、
   start_service 的 svc-*.log）按 run_id 登记，run 收尾只删自己那一份。
   为什么不能只靠「整目录清空」：GA_SAME_PROJECT_PARALLEL 默认开，同项目
   可以两个 run 并行改同一个工作区，整目录清会删掉别人正在用的文件。

2) 整目录清（自愈）：全工作区只有 .trace/tmp/、.trace/screenshots/、
   .trace/previews/ 这三个平台专属暂存区（组长提示词里也明确要求过程产物写
   .trace/tmp/）。当同项目没有别的 run 在跑时，收尾把这三个目录的内容一并清掉——
   这样进程被杀、用户中途取消留下的残留也能被下一个 run 带走，不需要人工打扫。

边界：只清这两个目录的**内容**，目录本身留着（工具下次还要用）；
交付物（assets/**、src/**、.trace/skills/**、.trace/art_bible.md 等）一律不碰。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from game_agent.logging_setup import get_logger

log = get_logger("orchestration.tempfiles")

# 平台专属暂存区：screenshot_page 落 .trace/screenshots/shot-*.png，
# start_service 落 .trace/tmp/services/svc-*.log，组长按约定把过程脚本写 .trace/tmp/，
# view_images 合成的联系表落 .trace/previews/sheet-*.jpg（只给前端出缩略图用）。
SCRATCH_DIRS = (".trace/tmp", ".trace/screenshots", ".trace/previews")

# run_id -> 本 run 用工具落下的临时文件绝对路径
_TEMP: dict[str, list[str]] = {}


def register(run_id: str, path: Path | str) -> None:
    """登记一个只服务于本轮判断的临时文件（截图、服务日志）。登记失败不影响工具。"""
    rid = str(run_id or "")
    if not rid:
        return
    try:
        p = str(Path(path).resolve())
    except OSError:
        return
    bucket = _TEMP.setdefault(rid, [])
    if p not in bucket:
        bucket.append(p)


def _rm(target: Path) -> bool:
    """删一个条目（目录递归删）；Windows 上文件还被占着会失败，返回 False 让调用方跳过。"""
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        return True
    except OSError as e:
        log.warning("临时文件删不掉（跳过）：%s：%s", target, e)
        return False


def sweep_run(run_id: str, workspace: Path | str, *, full: bool = True) -> list[str]:
    """run 收尾清理：删本 run 登记的文件；full=True 时连带清空两个暂存目录。

    full 由调用方决定——同项目还有别的 run 在跑时必须传 False，
    否则会删掉并行 run 正在用的过程文件。
    """
    removed: list[str] = []
    ws: Path | None = None
    try:
        ws = Path(workspace).resolve()
    except OSError:
        ws = None

    def _rel(p: Path) -> str:
        if ws is None:
            return str(p)
        try:
            return str(p.relative_to(ws)).replace("\\", "/")
        except ValueError:
            return str(p)

    # 1) 本 run 登记的（不管并行，自己拉的屎自己擦）
    for raw in _TEMP.pop(str(run_id), []):
        p = Path(raw)
        if p.exists() and _rm(p):
            removed.append(_rel(p))

    # 2) 独占时整目录自愈：把更早 run / 崩溃残留的过程文件一起带走
    if full and ws is not None:
        for rel in SCRATCH_DIRS:
            root = ws / rel
            try:
                if root.is_symlink() or not root.is_dir():
                    continue  # 目录本身被换成软链 → 不敢顺着删到工作区外
                for child in sorted(root.iterdir()):
                    if _rm(child):
                        name = f"{rel}/{child.name}"
                        if name not in removed:
                            removed.append(name)
            except OSError as e:
                log.warning("暂存目录扫不动（跳过）：%s：%s", rel, e)

    if removed:
        log.info("收尾清理过程临时文件 %d 项：%s",
                 len(removed), ", ".join(removed[:20]))
    return removed
