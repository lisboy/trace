"""运行能力开关：自动化测试 / 联网 / 出图配音。

前端「运行选项」里的勾选，默认全开（每个新会话都是开的）。
关闭时做两件事：
1. 工具层硬裁剪（caps_tool_filter）——不靠 prompt 自觉；
2. 本轮提示里写清楚（caps_hint）——说明不做什么、怎么收尾。
"""
from __future__ import annotations

CAP_TEST_TOOLS = {"start_service", "stop_service", "service_logs", "screenshot_page"}
CAP_WEB_TOOLS = {"web_search", "web_fetch"}
CAP_MEDIA_TOOLS = {
    "generate_image", "edit_image", "save_character_sheet", "generate_audio",
    "generate_video", "list_voice_presets", "tts", "design_voice", "generate_sfx",
    "generate_music", "upsert_voice_profile",
}
CAP_DEFAULTS = {"test": True, "web": True, "media": True}


def normalize_caps(raw: object) -> dict[str, bool]:
    """把前端传来的 {test/web/media} 归一化；缺省一律 True（老会话/老客户端默认全开）。"""
    caps = dict(CAP_DEFAULTS)
    if isinstance(raw, dict):
        for k in caps:
            if k in raw:
                caps[k] = bool(raw[k])
    return caps


def caps_tool_filter(whitelist: list[str], caps: dict[str, bool]) -> list[str]:
    drop: set[str] = set()
    if not caps.get("test", True):
        drop |= CAP_TEST_TOOLS
    if not caps.get("web", True):
        drop |= CAP_WEB_TOOLS
    if not caps.get("media", True):
        drop |= CAP_MEDIA_TOOLS
    return [w for w in whitelist if w not in drop] if drop else whitelist


def effective_whitelist(role_whitelist: list[str], run_type: str,
                        caps: object) -> list[str]:
    """角色全量白名单 → 本轮实际可用的工具名（analyze 剔写/生成；caps 关掉再剔）。

    注意：运行中把「仅分析」打开/关闭时（runtime 的 _apply_settings），这里会重新算一遍；
    历史中已调用过的工具不会因白名单变化而消失。
    """
    wl = list(role_whitelist)
    if run_type == "analyze":
        # WRITE_TOOLS 定义在 orchestrator（避免循环导入，这里延迟取）
        from game_agent.orchestration.orchestrator import WRITE_TOOLS
        wl = [w for w in wl if w not in WRITE_TOOLS]
    return caps_tool_filter(wl, normalize_caps(caps))


def caps_hint(caps: dict[str, bool]) -> str:
    """能力开关写进本轮提示：关掉的要说清「不做什么、怎么收尾」。"""
    parts: list[str] = []
    if caps.get("test", True):
        parts.append("【自动化测试已开启】改完要实际验收：能起服务就起，用截图/接口/单测看结果再汇报；"
                     "验证不过就继续修，别报「已完成」。")
    else:
        parts.append("【本轮关闭了自动化测试】不要 start_service / service_logs / screenshot_page，"
                     "也不要跑 CDP、接口或端到端测试。改完做语法/构建级静态检查即可收尾，"
                     "汇报时明确写「未做运行时验证」。")
    if not caps.get("web", True):
        parts.append("【本轮关闭了联网】不要 web_search / web_fetch；缺外部资料就基于现有代码判断，"
                     "并把不确定的地方写进汇报。")
    if not caps.get("media", True):
        parts.append("【本轮关闭了出图与配音】不要调用 generate_image / edit_image / generate_audio / tts "
                     "等媒体工具；任务需要素材时先说明「需要开启出图/配音」再继续。")
    return "\n".join(parts)
