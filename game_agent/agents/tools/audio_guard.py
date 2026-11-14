"""配音路由硬规则：生成配音一律直接调用 generate_audio 工具。

2026-10-28 实测事故（run 865be9fd）：
用户只说「涂山月见的配音重新生成……」——模型先读了项目技能 shiyuan-cv-pipeline，
技能正文写的是 `python tools/gen-cv-indextts.py --preset ...` 那条老管线，
于是它自己去 curl 127.0.0.1:8860、自己 start_service 起 IndexTTS2、
准备用 python 脚本直接合成，全程没有调用 generate_audio。

平台规则（用户明确要求）：
- 默认：配音/音色设计一律 generate_audio，语音服务由工具内部拉起；
- 例外：只有用户**明确要求**「按之前 python 脚本的方式 / 跑 gen-cv-indextts.py / 直接调 8860 接口」
  才允许自己起服务、跑脚本。
"""
from __future__ import annotations

import re
from pathlib import Path

# 只有这些解释器/壳出现在命令里，才认为命令「在执行脚本」——
# copy/type/move 这类文件操作即使带着 .py 路径也不是在跑脚本
# （实测误杀：`copy "<平台源码>/collaboration.py" .trace/tmp/` 因为源文件
#  文档字符串里写了「IndexTTS2」被当成配音探针拦下，2026-10-28）
_EXEC_VERB_RE = re.compile(
    r"\b(pythonw?(?:\d+(?:\.\d+)*)?|pypy(?:3)?|node|deno|bun|powershell|pwsh)\b", re.I)


# 语音服务历史默认端口（8860=IndexTTS2 曾用、7860=IndexTTS2 原始默认）。现在端口可 >10000
# 并会动态改绑，守卫不能只认死一个端口：这里动态并入「当前配置/实际端口」，同时保留这两个
# 历史默认，保证 .env 换端口后既拦得住新端口、也拦得住模型照旧 curl 老默认端口。
_LEGACY_TTS_PORTS = {8860, 7860}


def _tts_ports() -> set[int]:
    """当前需要拦的语音服务端口：配置端口 + 已改绑的实际端口 + 历史默认。"""
    ports = set(_LEGACY_TTS_PORTS)
    try:
        from game_agent.assets import tts as _tts
        for raw in (_tts.TTS_BASE_URL, _tts.actual_base()):
            try:
                p = _tts.urlparse((raw or "").strip()).port
            except Exception:  # noqa: BLE001
                p = None
            if p:
                ports.add(int(p))
    except Exception:  # noqa: BLE001 - 模块导入失败也不影响守卫
        pass
    return ports


def audio_python_allowed(text: str) -> bool:
    """用户是否明确要求走 python 脚本/直接调接口的老路（或明确要求运维语音服务）。"""
    t = (text or "").lower()
    # 1) 点名了老脚本 —— 最明确的信号
    if any(x in t for x in ("gen-cv-indextts", "cv-presets.py", "cv_presets.py",
                            "cv_gen.py", "cv-gen.py")):
        return True
    # 2) 明确说「用 python 跑/生成/脚本」
    if "python" in t and any(x in t for x in ("生成", "跑", "脚本", "方式", "直接", "执行")):
        return True
    # 3) 明确是在运维/排查语音服务本身（重启/启动/停止/排查 语音服务）
    svc_words = ("语音服务", "tts", "indextts", "index-tts") + tuple(
        str(p) for p in _tts_ports())
    if any(x in t for x in ("重启", "启动", "关闭", "停掉", "停止", "排查", "调试")) and any(
            x in t for x in svc_words):
        return True
    # 4) 明确说直接调接口
    if "直接调" in t and ("接口" in t or "api" in t):
        return True
    # 5) 「按之前/原来的方式/脚本/管线」——但要先排除否定句（「不要用之前的方式」）
    negated = any(x in t for x in ("不要", "别", "不用", "无需", "不再"))
    if not negated and any(x in t for x in ("之前", "以前", "原来", "老")) and any(
            x in t for x in ("方式", "脚本", "管线")):
        return True
    return False


def _norm(cmd: str) -> str:
    return (cmd or "").lower().replace("\\", "/")


_LAUNCH_RE = re.compile(
    r"\b(start|pythonw?(?:\d+(?:\.\d+)*)?|pypy|uvicorn|nohup|npm|yarn|node|deno|bun"
    r"|conda|activate)\b|\.py\b|\.bat\b|\.cmd\b|\.ps1\b|\.exe\b|\.venv",
    re.I)


def is_audio_server_command(cmd: str, port: int = 0) -> bool:
    """这条命令是不是在启动本地语音服务（IndexTTS2 等）。

    收口（实测误杀 2026-10-28）：`dir /b .../indextts2_v3/presets/*` 只是模型想
    看看有哪些音色预设名好传给 generate_audio，不是起服务——只有命令里带
    「启动/执行」信号（start、python、.py、uvicorn、conda activate…）才算。
    """
    if int(port or 0) in _tts_ports():
        return True
    c = _norm(cmd)
    if not any(k in c for k in ("indextts2", "index-tts", "index_tts", "index tts",
                                "tts_server", "tts-server")):
        return False
    return bool(_LAUNCH_RE.search(c))


def is_audio_script_command(cmd: str) -> bool:
    """这条命令是不是在绕过配音角色、自己调语音服务/跑配音脚本。"""
    c = _norm(cmd)
    if any(k in c for k in ("gen-cv-indextts", "cv-presets.py", "cv_presets.py",
                            "cv_gen.py", "cv-gen.py")):
        return True
    if any(re.search(rf":{p}(?:\D|$)", c) for p in _tts_ports()):
        return True
    if any(k in c for k in ("/api/v1/voice-design", "/api/v1/synthesize",
                            "/api/v1/tts", "voice-design/presets")):
        return True
    return False


AUDIO_ROUTE_DENIED = (
    "这条命令被拦：配音生成一律直接调用 **generate_audio** 工具，"
    "语音服务由它内部自动拉起/管理。不要自己 start_service 起 IndexTTS2、"
    "不要 curl 语音服务端口（如 127.0.0.1:8860）、不要跑 cv_gen.py / gen-cv-indextts.py / "
    "cv-presets.py，也不要写自定义脚本去调语音接口。\n"
    "正确做法：generate_audio(text=台词, voice=音色预设名, kind=\"voice\", slug=...)。\n"
    "需要新音色（用户说「不要之前的预设」）时，generate_audio 传 "
    "design_desc=音色文字描述、preset_name=预设名，它会先造音色再合成，不用你起服务。\n"
    "只有用户明确说「按之前 python 脚本的方式生成」时才允许本命令；"
    "用户没这么说，就按上面的工具路径做。"
)


def script_calls_audio(cmd: str, workspace) -> bool:
    """命令如果是「执行工作区里的脚本」，检查脚本内容是否在调语音接口。

    实测绕行手法：模型被拦住 curl 之后，会写一个 target/_xxx.py 探针脚本，
    里面 requests.post('http://127.0.0.1:8860/...')，再 `python target/_xxx.py`。
    只看命令行字符串抓不到，必须看被执行的脚本内容。

    三个收口（防误杀）：
    1. 只对真的在执行脚本的命令做内容检查（python/node/powershell…，见 _EXEC_VERB_RE）；
       copy/type 等文件操作直接放行；
    2. 只检查**工作区内**的脚本——探针脚本一定写在工作区里；平台源码、项目外的
       文件即使正文提到 IndexTTS2 字样，也不是在调语音接口；
    3. 只检查**真正被执行的那个脚本**，不是命令里出现的任意 .py：
       `pytest tests/test_audio_routing.py` 只是把测试文件当参数，测试正文提到
       IndexTTS2 不等于它在调语音接口（2026-10-30 实测误杀）。
    """
    text = str(cmd or "")
    low = text.lower()
    direct_script = low.rstrip().endswith((".bat", ".cmd", ".ps1"))
    if not (_EXEC_VERB_RE.search(low) or direct_script):
        return False

    script_tok = _executed_script_token(text, direct_script)
    if not script_tok:
        return False
    try:
        ws_root = Path(workspace).resolve()
    except OSError:
        ws_root = Path(workspace)
    p = Path(script_tok)
    if not p.is_absolute():
        p = ws_root / script_tok
    try:
        rp = p.resolve()
        # 工作区外的文件不扫（平台源码/系统文件里提到关键词不是调用）
        if ws_root not in rp.parents:
            return False
        if not rp.is_file() or rp.stat().st_size > 2 * 1024 * 1024:
            return False
        body = rp.read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        return False
    low_body = body.lower()
    port_hit = any(re.search(rf":{p}(?:\D|$)", low_body) for p in _tts_ports())
    return (port_hit or "voice-design" in low_body or "/api/v1/tts" in low_body
            or "indextts" in low_body or "gen-cv-indextts" in low_body)


_SCRIPT_SUFFIX_RE = re.compile(r"\.(?:py|mjs|js|ps1|bat|cmd)$", re.I)


def _executed_script_token(cmd: str, direct_script: bool) -> str | None:
    """取「真正被执行的脚本」：解释器后的第一个非选项参数，或整条命令本身是脚本。

    `python target/_probe.py --x` → target/_probe.py
    `python -m pytest tests/test_x.py` → None（-m 后面是模块/参数，不是被执行脚本）
    `node tools/build.mjs` → tools/build.mjs
    `start.bat` → start.bat
    """
    s = str(cmd or "").strip()
    if direct_script:
        tok = s.split()[0].strip('"\'') if s else ""
        return tok if _SCRIPT_SUFFIX_RE.search(tok) else None
    # 找到解释器词，取它后面第一个不以上划线/横线开头的参数
    m = _EXEC_VERB_RE.search(s)
    if not m:
        return None
    rest = s[m.end():].strip()
    for tok in re.findall(r'"[^"]*"|\'[^\']*\'|[^\s]+', rest):
        tok = tok.strip('"\'')
        if not tok or tok.startswith(("-", "/")):
            # `-m pytest` / `-c "..."` / `--flag`：不是脚本文件
            if tok == "-m":
                return None
            continue
        return tok if _SCRIPT_SUFFIX_RE.search(tok) else None
    return None

