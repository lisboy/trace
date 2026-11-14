"""素材驱动注册表（设计 §7.9：schema 驱动表单，前端零改动）。

新增供应商 = 加一个文件 + 一个 @register_driver，前端自动渲染表单。
本实现内置占位生成；真实供应商驱动在此注册即可被 Artist 调用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Field:
    name: str
    label: str
    type: str = "text"          # text|secret|int|select|bool
    required: bool = False
    default: Any = None
    options: list[str] = field(default_factory=list)
    min: int | None = None
    max: int | None = None


@dataclass
class DriverSpec:
    key: str
    kind: str                   # image|voice|sfx|music
    label: str
    docs_url: str = ""
    fields: list[Field] = field(default_factory=list)


_REGISTRY: dict[tuple[str, str], DriverSpec] = {}


def register_driver(key: str, kind: str, label: str, docs_url: str = "",
                    fields: list[Field] | None = None) -> Callable:
    def deco(cls):  # type: ignore[no-untyped-def]
        _REGISTRY[(kind, key)] = DriverSpec(key=key, kind=kind, label=label,
                                           docs_url=docs_url, fields=fields or [])
        cls._driver_spec = _REGISTRY[(kind, key)]
        return cls
    return deco


def get_driver_specs() -> list[dict]:
    return [
        {
            "key": s.key, "kind": s.kind, "label": s.label, "docs_url": s.docs_url,
            "fields": [
                {"name": f.name, "label": f.label, "type": f.type,
                 "required": f.required, "default": f.default,
                 "options": f.options, "min": f.min, "max": f.max}
                for f in s.fields
            ],
        }
        for s in _REGISTRY.values()
    ]


# ---------------- 内置驱动（配置口，不预置 key）----------------

@register_driver(
    "gemini_image", "image", "Google Gemini（图像）", "https://ai.google.dev/",
    fields=[
        Field("api_key", "API Key", type="secret", required=True),
        Field("base_url", "Base URL", type="text", default="https://generativelanguage.googleapis.com"),
        Field("model", "模型", type="select", default="gemini-2.5-flash-image",
              options=["gemini-2.5-flash-image", "imagen-4.0-generate-001"]),
        Field("max_images", "单次上限", type="int", default=4, min=1, max=8),
    ],
)
class GeminiImageDriver:  # noqa: D101
    pass


@register_driver(
    "openai_images", "image", "OpenAI 图像", "https://platform.openai.com/",
    fields=[
        Field("api_key", "API Key", type="secret", required=True),
        Field("model", "模型", type="select", default="gpt-image-1",
              options=["gpt-image-1", "dall-e-3"]),
    ],
)
class OpenAIImageDriver:  # noqa: D101
    pass


@register_driver(
    "seedream", "image", "火山方舟 Seedream", "https://www.volcengine.com/",
    fields=[
        Field("api_key", "API Key", type="secret", required=True),
        Field("model", "模型", type="select", default="seedream-3.0",
              options=["seedream-3.0", "seedream-2.0"]),
    ],
)
class SeedreamDriver:  # noqa: D101
    pass


@register_driver(
    "edge_tts", "voice", "Edge TTS（免费，无需 key）", "https://github.com/rany2/edge-tts",
    fields=[Field("voice", "音色", type="select", default="zh-CN-XiaoxiaoNeural",
                  options=["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "en-US-AriaNeural"])],
)
class EdgeTtsDriver:  # noqa: D101
    pass


@register_driver(
    "local_library", "sfx", "本地 CC0 音效库（免费）", "",
    fields=[Field("library_path", "库路径", type="text", default="assets/audio/sfx")],
)
class LocalSfxLibrary:  # noqa: D101
    pass


@register_driver(
    "indextts2", "voice", "IndexTTS2 本地部署（零样本克隆，可克隆自定音色）",
    "docs/语音接入.md",
    fields=[
        Field("base_url", "服务地址", type="text", default="http://127.0.0.1:10189"),
        Field("preset", "音色预设名", type="text", default="",
              required=False),
        Field("reference_audio", "参考音频路径（无预设时用，3~10 秒干声）", type="text"),
        Field("emo_mode", "情绪控制", type="select", default="0",
              options=["0", "1", "2", "3"]),  # 0=同参考 1=情感音频 2=八维向量 3=文本描述
        Field("emo_text", "情绪描述（emo_mode=3 时生效，如「愤怒地质问」）", type="text"),
        Field("emo_weight", "情绪强度 0~1", type="text", default="0.65"),
        Field("emo_vector", "八维情感（高兴,愤怒,悲伤,害怕,厌恶,忧郁,惊讶,平静；逗号分隔，emo_mode=2 时生效）",
              type="text"),
        Field("emo_audio", "情感参考音频路径（一段带目标情绪的干声，emo_mode=1 时生效）", type="text"),
        Field("speed", "语速 0.5~2.0（>1 慢 <1 快；IndexTTS-2.5 才生效）", type="text"),
        Field("lang", "语言 auto/zh/en/ja/es/ar（默认 auto；IndexTTS-2.5 才生效）", type="text", default="auto"),
        Field("format", "输出格式", type="select", default="wav", options=["wav"]),
    ],
)
class IndexTTS2Driver:  # noqa: D101
    """本机 IndexTTS2（8G 卡可跑）。比 openai_tts 强的地方是能克隆自定音色：
    给一段 3~10 秒参考音频（或 IndexTTS2 里的预设名），它按那个音色念。
    单卡串行推理，一句约 20~60 秒，超时见 .env TTS_TIMEOUT_S。"""


@register_driver(
    "minimax_music", "music", "MiniMax 音乐", "https://www.minimax.io/",    fields=[Field("api_key", "API Key", type="secret", required=True),
            Field("model", "模型", type="text", default="music-01")],
)
class MinimaxMusicDriver:  # noqa: D101
    pass


@register_driver(
    "external_http", "image", "外置素材源（HTTP 拉取图片/音频/图标）", "",
    fields=[
        Field("base_url", "Base URL", type="text", required=True),
        Field("api_style", "接口风格", type="select", default="json",
              options=["json", "template"]),
        Field("url_template", "URL 模板（template 模式，含 {query}/{n}）", type="text"),
        Field("list_path", "列表路径（json 模式可选）", type="text"),
        Field("auth_header", "鉴权请求头名", type="text"),
        Field("auth_value", "鉴权请求头值", type="secret"),
    ],
)
class ExternalHttpSource:  # noqa: D101
    """通用外置素材源：任何返回 {items:[{name,url}]} / 数组 JSON 的开放平台均可接入。"""
    pass
