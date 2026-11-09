"""环境事实画像 `.trace/env.json`：平台自动记录「这台机器/这个工作区跑命令的现实」。

为什么单独一层（2026-10-28 会话实测）：每个新 run 都要重新用命令去撞
「这里是 cmd 还是 bash」「grep 有没有」「是不是 git 仓库」，一轮一轮白烧（约 2 分钟）。
这些答案跟具体任务无关、跨 run 稳定，所以在命令失败时由平台顺手记一笔，
下个 run 直接把结论摆进上下文，别让模型再试探一遍。

两类来源：
- 平台自动采集：命令失败时 sniff 出的 shell / unavailable / git，起服务成功时记的
  service_start / service_url；
- 智能体自己确认的入口事实（测试账号、登录入口、验证码开关…）走 save_env_fact 工具——
  这些东西平台没法从报错文案里认出来（2026-10-28 那次光「怎么登进去」烧了 6 分钟，
  下个 run 还得从头撞）。

规则：
- 只有能确定的模式才记（错误文案里明确写着「不是内部或外部命令」这类），脏值宁可不记；
- 已有同键不覆盖（第一条事实最可信：后面撞的坑往往是任务特有的），
  要改已记的值必须显式 force=True；列表键（unavailable）做并集追加；
- 键数上限 40，超了按写入顺序丢最旧的。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from game_agent.logging_setup import get_logger

log = get_logger("orchestration.envfacts")

ENV_FILE = ".trace/env.json"
_MAX_KEYS = 40

# 「这条命令本机没有」的各 shell 原文案：命中谁就顺带知道当前 shell 是哪种
_CMD_NOT_FOUND = ("不是内部或外部命令", "is not recognized as an internal")
_PS_NOT_FOUND = ("The term '", "is not recognized as the name of a cmdlet")
_POSIX_NOT_FOUND = ("command not found",)
_QUOTED_RE = re.compile(r"[\"'“‘]([A-Za-z0-9_.\\/-]{1,60})[\"'”’]")
_NO_GIT_RE = re.compile(r"not a git repository", re.I)


def _clean(v: Any) -> Any:
    return v.strip() if isinstance(v, str) else v


def _bad_word(text: str) -> str:
    """从错误文案里取出「不被认识的那个命令名」。"""
    m = _QUOTED_RE.search(text)
    return _clean(m.group(1)).lower() if m else ""


def sniff(cmd: str, out: str, rc: int) -> dict[str, Any]:
    """纯函数：从「命令 + 合并输出 + 退出码」里抠出可复用的环境事实。

    认不准就返回空 dict——绝不把「这次任务自己的报错」升级成机器事实。
    """
    text = out or ""
    head = re.split(r"\s+", str(cmd or "").strip().strip('"\''))[0].strip('"\'').lower()
    facts: dict[str, Any] = {}
    word = _bad_word(text)
    if any(k in text for k in _CMD_NOT_FOUND):
        shell = "cmd"
    elif any(k.lower() in text.lower() for k in _PS_NOT_FOUND):
        shell = "powershell"
    elif any(k in text for k in _POSIX_NOT_FOUND):
        shell = "sh"
    else:
        shell = ""
    if shell:
        facts["shell"] = shell
        if word:
            facts["unavailable"] = [word]
    if _NO_GIT_RE.search(text) or (head == "git" and rc == 128):
        facts["git"] = False
    return facts


def load(workspace: Path | str) -> dict[str, Any]:
    p = Path(workspace) / ENV_FILE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(workspace: Path | str, facts: dict[str, Any], force: bool = False) -> bool:
    """合并写入（同键不覆盖、列表键并集）。没有新东西就不落盘、不动 mtime。

    force=True 才允许改写已有的标量值：入口事实会过期（换端口、换测试账号），但只有
    明确要求订正时才覆盖——平台自己的采集路径一律不传，免得把一次任务特有的值当成新现实。
    """
    clean = {k: _clean(v) for k, v in (facts or {}).items() if k and v not in (None, "", [])}
    if not clean:
        return False
    ws = Path(workspace)
    data = load(ws)
    changed = False
    for k, v in clean.items():
        if k in data:
            if isinstance(data[k], list) and isinstance(v, list):
                added = [x for x in v if x not in data[k]]
                if added:
                    data[k] = (data[k] + added)[:40]
                    changed = True
            elif force and data[k] != v:
                data[k] = v
                changed = True
            continue
        data[k] = v
        changed = True
    if not changed:
        return False
    for stale in list(data.keys())[:max(0, len(data) - _MAX_KEYS)]:
        data.pop(stale, None)
    try:
        d = ws / Path(ENV_FILE).parent
        d.mkdir(parents=True, exist_ok=True)
        tmp = ws / f"{ENV_FILE}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ws / ENV_FILE)
    except OSError as e:
        log.warning("环境事实写入失败：%s", e)
        return False
    return True


def observe(cmd: str, out: str, rc: int, workspace: Path | str) -> dict[str, Any]:
    """run_command 收尾调用：认出事实就落盘，返回本次新记的键（给日志/测试看）。"""
    try:
        facts = sniff(cmd, out, rc)
        if not facts or not record(workspace, facts):
            return {}
        return facts
    except Exception:  # noqa: BLE001 - 记环境事实绝不能影响命令本身的结果
        log.debug("环境事实采集异常", exc_info=True)
        return {}


def prompt_block(workspace: Path | str) -> str:
    """注入上下文的「环境事实」块；没有记录就返回空串。"""
    data = load(workspace)
    if not data:
        return ""
    lines = []
    for k, v in data.items():
        if isinstance(v, list):
            v = "、".join(str(x) for x in v)
        lines.append(f"- {k}：{v}")
    if not lines:
        return ""
    return ("## 环境事实与入口事实（本平台/此前 run 已经确认过，直接用，别再逐条试探）\n"
            + "\n".join(lines) + "\n"
            "「已记录为不可用」的写法不要再用；起服务/登录这类入口事实如果已给出，"
            "直接照它做，不要重新翻代码找账号、也不要换个端口再试一遍。\n"
            "这些事实之外仍有不确定的，直接试一次、按报错换写法，不要连着试同一类写法；\n"
            "试出「下次还要用」的新事实（测试账号、启动命令、进哪个页面），"
            "用 save_env_fact 记一条，别让下个项目 run 再撞一遍。\n\n")
