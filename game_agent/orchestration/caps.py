"""运行能力开关：自动化测试 / 联网 / 出图配音。

原先前端「运行选项」里有这三个勾选，现在这块 UI 已移除——用哪个工具完全由模型按任务判断。
保留本模块是为了：老客户端/API 仍可显式传 caps，传了就按它裁剪。
缺省（不传）一律全开：工具不裁剪，只留下「你自己判断要不要跑」的引导（caps_hint）。
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
    """角色全量白名单 → 本轮实际可用的工具名（只按用户手动的能力开关裁剪）。

    analyze / 只读诊断不裁工具：用哪个工具由模型自己判断（诊断时也可能要跑命令验证）。
    运行中改「仅分析 / 能力开关」时（runtime 的 _apply_settings）会在这里重算一遍。
    """
    del run_type  # 保留参数：历史调用方/测试仍按位置传，工具不再随轮次类型裁剪
    return caps_tool_filter(list(role_whitelist), normalize_caps(caps))


def caps_hint(caps: dict[str, bool]) -> str:
    """能力开关写进本轮提示：关掉的要说清「不做什么、怎么收尾」。"""
    parts: list[str] = []
    if caps.get("test", True):
        parts.append("【自动化测试已开启】起服务做运行时验证是**重手段，默认不做**："
                     "改完先跑 run_command 的语法/构建/单测级静态验证，过了就汇报；"
                     "只有用户明确要求看效果，或同一个问题多轮修改仍未解决时，"
                     "才 start_service + screenshot_page 实测。"
                     "若起服务换了几种方式仍起不来，就**停止测试**，把起不来的原因、已试过的方法、风险点写进汇报，不要无限重试。")
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
