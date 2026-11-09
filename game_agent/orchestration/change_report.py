"""变更报告（设计 §7.7）：工具层写前快照 + 测试结论 → 结构化报告。

改动清单来自 snapshots（平台不接管 git，工作区里有没有 .git 都不看）：
只报「这次 run 动过哪些文件、净增删多少行」。
"""
from __future__ import annotations

from pathlib import Path


def build_change_report(workspace: Path, *, run_id: str, run_type: str,
                        test_report: dict | None, has_placeholder: bool,
                        artifacts: list, model_map: dict | None = None) -> dict:
    from game_agent.agents.tools import is_scratch_path
    from game_agent.orchestration import snapshots
    try:
        changes = snapshots.list_changes(run_id, workspace)
    except Exception:  # noqa: BLE001 - 列不出改动不能把整个 run 带崩
        changes = {"files": [], "summary": {"count": 0, "added": 0, "deleted": 0}}
    # 过程文件（.trace/tmp/ 探针、argfile 生成脚本、ps1…）不算「改动的文件」。
    # 实测 run 9a9656bc：变更清单报 14 个文件，其中 9 个是 .trace/tmp/*，产品改动只有 5 个。
    # _landed_actions 早就用 is_scratch_path 过滤过，这里（用户可见的「改动的文件」卡片）
    # 却漏了，导致用户以为改了一大堆临时脚本。过滤后 summary 必须按剩下的重算。
    kept = [f for f in changes["files"] if not is_scratch_path(str(f.get("path") or ""))]
    files = [{"file": f["path"], "added": f["added"], "deleted": f["deleted"],
              "status": f["status"]} for f in kept]
    summary = {
        "count": len(files),
        "added": sum(f["added"] for f in files),
        "deleted": sum(f["deleted"] for f in files),
    }
    placeholders = [a for a in artifacts if getattr(a, "is_placeholder", False)]
    return {
        "run_type": run_type,
        "files": files,
        "summary": summary,
        "test_report": test_report,
        "has_placeholder": has_placeholder,
        "placeholder_count": len(placeholders),
        "placeholders": [
            {"slug": getattr(p, "slug", ""), "rel_path": getattr(p, "rel_path", "")}
            for p in placeholders
        ],
        "model_map": model_map or {},
    }
