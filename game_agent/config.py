"""全局配置：从环境变量读取，提供 lite / server 双模式所需的所有开关与路径。

设计约束：业务代码只依赖 5 个抽象接口，本模块只决定「用哪套实现」与「路径/密钥」。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    if getattr(sys, "frozen", False):
        # 冻结成 exe 后没有 .py 文件，load_dotenv() 默认从当前工作目录找 .env；
        # 明确指向 exe 同目录的 .env，双击启动（工作目录未必是 exe 目录）也能读到配置。
        load_dotenv(Path(sys.executable).parent / ".env")
    else:
        load_dotenv()
except Exception:  # pragma: no cover - 没有 python-dotenv 也能跑（环境变量已注入）
    pass

ENV = os.environ


def _str(key: str, default: str = "") -> str:
    v = ENV.get(key)
    return v if v not in (None, "") else default


def _bool(key: str, default: bool = False) -> bool:
    v = ENV.get(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    v = ENV.get(key)
    if v in (None, ""):
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    v = ENV.get(key)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except ValueError:
        return default


# ---------- 模式 ----------
MODE: str = _str("GA_MODE", "lite").lower()
assert MODE in ("lite", "server"), f"GA_MODE 非法: {MODE}"

# ---------- 数据目录 / 路径 ----------
_DATA_DIR_RAW = _str("GA_DATA_DIR", str(Path.home() / ".trace"))
DATA_DIR = Path(_DATA_DIR_RAW).expanduser().resolve()
PROJECTS_ROOT = Path(
    _str("PROJECTS_ROOT", str(DATA_DIR / "projects"))
).expanduser().resolve()
# 聊天附图（用户贴的原图）单独存这里，不落在项目工作区里：
#   ① 工作区常被快照回滚 / 切换分支 / 换机器 / 清缓存整个重来，埋在里面的附图会跟着消失；
#   ② 用户在自己仓库里 git clean/checkout 也会把 .trace/uploads 当未跟踪文件删掉。
# 放到 DATA_DIR 下（与 db/日志同级、随安装目录一起走），附图就能和工作区生命周期解耦。
# 旧数据仍在各工作区的 .trace/uploads/（读取时按项目回退、启动时迁移到新目录）。
CHAT_UPLOADS_ROOT = Path(
    _str("GA_CHAT_UPLOADS_ROOT", str(DATA_DIR / "chat_uploads"))
).expanduser().resolve()
LOG_DIR = DATA_DIR / "logs"
TMP_DIR = DATA_DIR / "tmp"

# ---------- 密钥 ----------
SECRET_KEY = _str("GA_SECRET_KEY", "")
if not SECRET_KEY and MODE == "lite":
    # lite 离线可接受一个确定性开发密钥；server 模式必须由用户设置
    SECRET_KEY = "dev-insecure-secret-change-me-in-server"

# ---------- 数据库 / Redis ----------
DATABASE_URL = _str("DATABASE_URL", "")
REDIS_URL = _str("REDIS_URL", "")

if not DATABASE_URL:
    if MODE == "lite":
        DB_PATH = DATA_DIR / "game_agent.db"
        DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
    else:
        raise RuntimeError(
            "server 模式必须设置 DATABASE_URL，"
            "公用部署推荐 MySQL：mysql+aiomysql://user:pass@host:3306/game_agent?charset=utf8mb4"
        )

# ---------- LLM ----------
# 只有「真实大模型」一种运行方式：有配置好的供应商档位就走档位，没有就走下面的
# 全局兜底（.env）。不存在 mock/离线演示模式——"能不能聊"只取决于有没有配供应商。
OPENAI_API_KEY = _str("OPENAI_API_KEY")
OPENAI_BASE_URL = _str("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = _str("GA_LLM_MODEL", "gpt-4o-mini")
# 调用失败重试（429/5xx/超时/网络错误）：指数退避，遵循 Retry-After
LLM_MAX_ATTEMPTS = 5                             # 单次对话请求的最大尝试次数（含首次），达标即放弃
# 额外重试次数（总尝试 = 1 + N），默认 4 → 共 5 次；环境变量调大也会被 LLM_MAX_ATTEMPTS 夹住
LLM_RETRIES = max(0, min(_int("GA_LLM_RETRIES", LLM_MAX_ATTEMPTS - 1), LLM_MAX_ATTEMPTS - 1))
LLM_CALL_TIMEOUT_S = _int("GA_LLM_CALL_TIMEOUT_S", 360)  # 单次 LLM 调用硬上限（防网关挂起卡死 run）
LLM_RETRY_BASE_S = float(_str("GA_LLM_RETRY_BASE_S", "1.5") or 1.5)
# 历史压缩用的「摘要调用」单独给一套更短的上限：它不是正文产出，卡住时用户看到的是
# 「压缩/汇报时一直不动」。默认 120s / 2 次（最坏 ~2×120s + 退避），失败就退化为要点节选，
# 绝不让摘要器按正文的 5×360s 预算把整个 run 拖死。
LLM_SUMMARY_TIMEOUT_S = _int("GA_LLM_SUMMARY_TIMEOUT_S", 120)
LLM_SUMMARY_MAX_ATTEMPTS = max(1, min(_int("GA_LLM_SUMMARY_MAX_ATTEMPTS", 2), LLM_MAX_ATTEMPTS))
# 整次压缩（可能分好几块串行摘要）的总墙钟预算：超了就停下，用已有摘要 + 要点节选兜底。
# 否则 4 块 × 每块多次重试会把「压缩」这一步本身拖成好几分钟的假死。
LLM_SUMMARY_CYCLE_BUDGET_S = _int("GA_LLM_SUMMARY_CYCLE_BUDGET_S", 240)
# 中转站挂在 Cloudflare 后面时，httpx 默认 UA（python-httpx/x.y）会被 WAF 判成机器人，
# /models 与 /chat/completions 一律回 HTTP 403 + 「Just a moment...」HTML ——
# 表现成「Key 无效/额度不足」，其实是端点根本没把请求转给上游。带浏览器 UA 即 200。
# 置空 = 用 httpx 默认 UA（自建端点如 Ollama 无此问题，但默认带也无害）。
LLM_USER_AGENT = _str("GA_LLM_USER_AGENT",
                      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# 上下文现场（llm_transcripts）保留天数：每轮存一份完整请求，长 run 一轮就上百 KB，
# 不清库迟早把 MySQL 撑爆。0 = 不保留（等于关掉现场记录，只剩计数统计）。
LLM_TRANSCRIPT_KEEP_DAYS = _int("GA_LLM_TRANSCRIPT_KEEP_DAYS", 14)

# ---------- 联网搜索（web_search / web_fetch 工具） ----------
# bing = 无 key 抓结果页（国内可达，默认）；ddg = DuckDuckGo HTML 版（部分网络不可达）；
# tavily = 需要 GA_TAVILY_KEY，质量最好。
SEARCH_PROVIDER = _str("GA_SEARCH_PROVIDER", "bing").lower()
TAVILY_API_KEY = _str("GA_TAVILY_KEY")
WEB_SEARCH_TIMEOUT_S = _int("GA_WEB_SEARCH_TIMEOUT_S", 15)

_ROLE_MODELS_RAW = _str("GA_ROLE_MODELS", "")
ROLE_MODELS: dict[str, str] = {}
for pair in _ROLE_MODELS_RAW.split(","):
    if ":" in pair:
        k, v = pair.split(":", 1)
        k = k.strip()
        # 旧配置里的键名（planner=会话模型）映射到 v3 的 main
        if k == "planner":
            k = "main"
        ROLE_MODELS[k] = v.strip()

# ---------- 认证 ----------
AUTH_MODE = _str("GA_AUTH_MODE", "both").lower()
assert AUTH_MODE in ("password", "oidc", "both")
PLACEHOLDER_ENABLED = _bool("PLACEHOLDER_ENABLED", True)

# ---------- 图像生成（ComfyUI 本地） ----------
COMFYUI_BASE_URL = _str("COMFYUI_BASE_URL")           # 空 = 未启用，图像走占位
COMFYUI_CHECKPOINT = _str("COMFYUI_CHECKPOINT")       # 留空自动选第一个可用模型
# text_encoders 里选哪个文本编码器（DiT 三件套）。留空自动：优先 8b（qwen_image_2.1 的
# 4096 维对应 Qwen3-VL-8B）。必须钉死——「取第一个含 qwen 的文件」会误选给 MiniMax-H3/
# ClipProj 用的 qwen3vl_4b（2560 维），与扩散模型 4096 维不匹配，KSampler 直接报
# "normalized_shape=[4096] ... got [1,N,2560]"（2026-11-05 踩坑）。
COMFYUI_CLIP = _str("COMFYUI_CLIP")
# 反向提示词：出图默认负面词（工作台「恢复默认负面词」与 .env 未覆盖时都用它）。
# 采用带权重的分组写法：画质/解剖/手部/脸部按重要程度加权重（1.2~1.4），
# 其余为常规排除项（文字、水印、过曝、NSFW 等）。可用 .env COMFYUI_NEGATIVE_PROMPT 覆盖。
COMFYUI_NEGATIVE_PROMPT = _str(
    "COMFYUI_NEGATIVE_PROMPT",
    "(worst quality, low quality, normal quality:1.4), "
    "(bad anatomy, deformed, mutated, disfigured:1.3), "
    "(bad hands, missing fingers, extra fingers, fused fingers, poorly drawn hands, "
    "malformed limbs, extra limbs, missing limbs:1.4), "
    "(distorted face, deformed face, asymmetrical eyes:1.2), "
    "lowres, blurry, jpeg artifacts, text, watermark, signature, username, error, ugly, "
    "bad proportions, unnatural pose, overexposed, washed out, over-saturated, nsfw",
)
# 自定义 API 格式工作流 JSON（ComfyUI 界面 Save (API Format) 导出），空 = 内置 SD 默认工作流
COMFYUI_WORKFLOW_PATH = _str("COMFYUI_WORKFLOW_PATH")
# 美术派发/素材重画走哪条工作流：auto=按模型探测（checkpoints 里有 ckpt 就优先经典 SD），
# checkpoint=强制经典 SD，diffusion=强制 DiT 三件套（Qwen/Anima）。非法值按 auto 处理。
# 实测：本机 checkpoints 目录躺着 6.9G 的 SDXL，auto 永远选不到 DiT，要全批走 Qwen 只能显式指定。
COMFYUI_WORKFLOW_MODE = _str("COMFYUI_WORKFLOW_MODE", "auto")
# 默认风格 LoRA（models/loras 下的相对路径，空 = 不挂）。用于把出图拉向某个统一画风，
# 支持子目录（如 qwen21_style/xxx.safetensors）。强度 0~1.5，Qwen-Image 实测 0.7~0.9 合适。
COMFYUI_LORA = _str("COMFYUI_LORA")
COMFYUI_LORA_STRENGTH = _float("COMFYUI_LORA_STRENGTH", 0.8)
# LoRA 安装目录（工作台「上传 LoRA」写到这）：空 = 从 COMFYUI_LAUNCH_CMD 里的 main.py 反推
# <ComfyUI>/models/loras；仍解析不到就禁止上传（列表照常从 ComfyUI /object_info 读）。
COMFYUI_LORAS_DIR = _str("COMFYUI_LORAS_DIR")
COMFYUI_IMG2IMG_DENOISE = _float("COMFYUI_IMG2IMG_DENOISE", 0.42)
# ---------- 高分辨率出图（大图 / 超原生分辨率） ----------
# ComfyUI 本身不限分辨率，真正的门槛是 8G 显存：SDXL/Qwen 原生画幅只有 ~1MP，
# 直接按 3~4MP 出图能出（实测 2360x1640≈3.87MP、qwen_image_2.1 int8 + LoRA、20 步、
# tiled 解码，8G 卡约 5 分钟成功），但再往上（4K≈8MP）会 OOM 或把 GPU 拖死。
# 策略：目标像素 ≤ 此阈值 → 直接按目标分辨率采样（全画质）；超过 → 先按阈值内
# 的基准分辨率采样，再 Lanczos 放大到目标尺寸（快但不增细节）。
COMFYUI_DIRECT_MAX_MP = _float("COMFYUI_DIRECT_MAX_MP", 4.0)
# 大图 VAE 解码用分块（tiled）而非一次性解码，显著降低解码阶段的显存峰值（8G 必开）。
COMFYUI_VAE_TILE = _int("COMFYUI_VAE_TILE", 512)
COMFYUI_VAE_TILE_OVERLAP = _int("COMFYUI_VAE_TILE_OVERLAP", 64)
# 需要分块解码 / 触发基准采样的像素阈值（百万像素）。低于此值仍走原样一次性解码，行为不变。
COMFYUI_TILED_MP = _float("COMFYUI_TILED_MP", 1.6)
# 单张出图等待上限（含排队/加载模型）。1792x1024 + LoRA 实测 220~300s，
# 300s 卡在尾巴上会随机判超时（批量里一张图连超 3 次烧 15 分钟），留足余量。
COMFYUI_TIMEOUT_S = _int("COMFYUI_TIMEOUT_S", 600)
# ComfyUI「启动」等待上限（不是出图）：首次/久未运行（冷启动）要扫模型、解包前端、
# JIT 编译内核，几分钟很常见；固定 180s 常在尾巴判死 → 整批图回退占位。
# 冷启动给 COMFYUI_BOOT_TIMEOUT_S；热启动用较短值（内部取 min(此值,240)）。
COMFYUI_BOOT_TIMEOUT_S = _int("COMFYUI_BOOT_TIMEOUT_S", 600)
# 启动期间「多久没有新日志算异常」——只用于记录告警，不打断等待（加载模型本来就长时间无输出）
COMFYUI_BOOT_STALL_S = _int("COMFYUI_BOOT_STALL_S", 180)
# 启动硬上限：进程还活着时最多等这么久，超过一定判失败（防真正卡死的进程被无限等）
COMFYUI_BOOT_HARD_CAP_S = _int("COMFYUI_BOOT_HARD_CAP_S", 900)
# ComfyUI 未运行时自动启动：启动命令（exe 路径），空 = 探测常见安装位置；GA_COMFYUI_AUTOSTART=false 关闭
COMFYUI_LAUNCH_CMD = _str("COMFYUI_LAUNCH_CMD")
COMFYUI_AUTOSTART = _str("GA_COMFYUI_AUTOSTART", "true").lower() not in ("false", "0", "no")

# ---------- 视频生成（ComfyUI 本地 Wan2.2 TI2V 5B） ----------
# 8GB 单卡只能跑 5B（14B/28B 权重放不下），这里固定一套 Wan2.2 TI2V 5B 三件套工作流。
# 模型名对应 ComfyUI models/ 下的相对文件名；换模型改这里即可。
COMFYUI_VIDEO_UNET = _str("COMFYUI_VIDEO_UNET", "wan2.2_ti2v_5B_fp16.safetensors")
COMFYUI_VIDEO_CLIP = _str("COMFYUI_VIDEO_CLIP", "umt5_xxl_fp8_e4m3fn_scaled.safetensors")
COMFYUI_VIDEO_VAE = _str("COMFYUI_VIDEO_VAE", "wan2.2_vae.safetensors")
# 默认出片尺寸/帧数/帧率：8GB 显存用 832x480 + 49 帧（≈2s@24fps）最稳；调大先小步试。
# ⚠️ Wan2.2 TI2V 5B 是**原生 24fps / 720P** 的模型（官方模板 121 帧按 24fps 播放）；
#    以前按 16fps 出片会让动作发顿，再靠插值硬补到 60 会放大伪影——现在按原生 24fps 出。
#    帧数仍必须是 4n+1（17/33/49/81…）。
COMFYUI_VIDEO_WIDTH = _int("COMFYUI_VIDEO_WIDTH", 832)
COMFYUI_VIDEO_HEIGHT = _int("COMFYUI_VIDEO_HEIGHT", 480)
COMFYUI_VIDEO_LENGTH = _int("COMFYUI_VIDEO_LENGTH", 49)
COMFYUI_VIDEO_FPS = _int("COMFYUI_VIDEO_FPS", 24)
# 采样：uni_pc + simple。cfg 取 4.0（Wan 默认 5.0 偏高，白色/浅色人物在漫剧里容易过曝发白脸）；
# 降一点更稳、更少过曝，仍能守住提示词。steps 保持 20 兼顾出片质量与 8G 速度。
COMFYUI_VIDEO_STEPS = _int("COMFYUI_VIDEO_STEPS", 20)
COMFYUI_VIDEO_CFG = _float("COMFYUI_VIDEO_CFG", 3.0)
# 视频专用反向提示词（与出图的分开，避免把「3d/景深/虚化」写进出图，出图有时确实要 3D/景深）。
# 重点：① 防变脸、防割裂/闪烁；② 防把 2D 立绘画成 3D 塑料手办；③ 防背景被脑补成深色/墨绿/模糊光斑。
COMFYUI_VIDEO_NEGATIVE_PROMPT = _str(
    "COMFYUI_VIDEO_NEGATIVE_PROMPT",
    "text, watermark, lowres, worst quality, low quality, blurry, jpeg artifacts, "
    "overexposed, blown out highlights, overbright, washed out, white face, glowing face, "
    "distorted face, deformed face, inconsistent character face, mutated face, "
    "extra limbs, bad anatomy, frame flicker, flickering, color flicker, "
    "temporal inconsistency, warp, tearing, screen tearing, glitch, "
    "morphing, sudden large motion, chaotic motion, camera shake, "
    "3d, 3d render, cgi, cg render, plastic, doll, figurine, toy, oversharpened, "
    "depth of field, bokeh, blurred background, dark background, green screen, color shift, "
    "背景突变, 静帧, 画面割裂, 人脸扭曲, 变脸, 五官错位, 闪白, 过曝, 闪烁, 撕裂, "
    "三维, 塑料感, 人偶, 手办, 深色背景",
)
# i2v 参考图透明底合成色：透明底 PNG 在 ComfyUI LoadImage 会丢 alpha 变纯黑（已实测），
# 后端会先合成到此颜色再上传。取中性浅灰既能和白裙拉开边界，又不会像纯白那样把浅色人物烧白。
COMFYUI_VIDEO_REF_BG = _str("COMFYUI_VIDEO_REF_BG", "#ebebe7")
# 出片后补帧：Wan 原生 24fps → 用 ffmpeg 运动补偿补到 60fps 顺滑播放（纯 CPU、不占 GPU）。
# 失败自动保留原片、不阻断出片。想关掉把 COMFYUI_VIDEO_SMOOTH=false。
COMFYUI_VIDEO_SMOOTH = _bool("COMFYUI_VIDEO_SMOOTH", True)
COMFYUI_VIDEO_SMOOTH_FPS = _int("COMFYUI_VIDEO_SMOOTH_FPS", 60)
# 时间域降噪（防漫剧画面割裂/闪烁）：在补帧前用 ffmpeg hqdn3d 做很轻的时间+空间降噪。
# 强度 0=关；越大越平滑但会把漫画锐利线条糊掉，建议 0.15–0.25 档（这里用 light 预设）。
COMFYUI_VIDEO_TEMPORAL_DENOISE = _bool("COMFYUI_VIDEO_TEMPORAL_DENOISE", True)
# 单条视频等待上限（含排队/冷启动加载模型）。5B 在 3060Ti 上一条约 2–5 分钟，留足余量。
COMFYUI_VIDEO_TIMEOUT_S = _int("COMFYUI_VIDEO_TIMEOUT_S", 1800)

BOOTSTRAP_TOKEN = _str("GA_BOOTSTRAP_TOKEN", "")
# 首启后是否自动造示例 owner（demo@local / demo1234）与示例团队/项目。
# 源码与团队部署默认关闭（走 /setup 引导 token）；Windows 单机打包版在 env.template 里
# 显式设 GA_SEED_DEMO=1，开箱即可登录，避免预填一个不存在的账号导致「邮箱或密码错误」。
SEED_DEMO = _bool("GA_SEED_DEMO", False)

# ---------- 语音合成（IndexTTS2 本地服务，配音 / 语音工作台 / 对话朗读） ----------
# 空 = 未启用：配音工具回退占位静音 wav，语音工作台显示"未连接"
TTS_BASE_URL = _str("TTS_BASE_URL")
# 默认音色：优先用预设名（IndexTTS2 服务端预设），否则用参考音频文件路径
TTS_PRESET = _str("TTS_PRESET")
TTS_REF_AUDIO = _str("TTS_REF_AUDIO")
# 情绪：0=同参考 1=情感音频 2=八维向量 3=情感描述文本
#   只给音色参考音频而不指定情绪时，模型会用平静语气（听感"三句一个调"的根因）
TTS_EMO_MODE = _str("TTS_EMO_MODE", "0")
TTS_EMO_TEXT = _str("TTS_EMO_TEXT")
TTS_EMO_WEIGHT = float(_str("TTS_EMO_WEIGHT", "0.65") or 0.65)
# 八维情感权重（高兴,愤怒,悲伤,害怕,厌恶,忧郁,惊讶,平静），逗号分隔；配置即用 mode=2
TTS_EMO_VECTOR = _str("TTS_EMO_VECTOR")
# 情感参考音频文件路径（一段带目标情绪的干声；配置即用 mode=1）
TTS_EMO_AUDIO = _str("TTS_EMO_AUDIO")
# 单次合成等待上限：8G 单卡串行，首次含 30~60 秒模型加载，别设太小
TTS_TIMEOUT_S = _int("TTS_TIMEOUT_S", 600)
# 是否在界面暴露「音色设计」（文字描述造音色，需 IndexTTS2 侧装好 VoxCPM2）
TTS_VOICE_DESIGN = _str("GA_TTS_VOICE_DESIGN", "true").lower() not in ("false", "0", "no")
# 语音服务未运行时自动拉起（合成/音色设计前检查，未就绪则等待）
TTS_AUTOSTART = _str("GA_TTS_AUTOSTART", "true").lower() not in ("false", "0", "no")
# IndexTTS2 分发目录：含 runtime\python.exe 与 server.py
TTS_HOME = _str("TTS_HOME", r"D:\AI\IndexTTS2.0_v3")
# 自定义启动命令（留空自动用 TTS_HOME\runtime\python.exe -u server.py --port <端口>）
TTS_LAUNCH_CMD = _str("TTS_LAUNCH_CMD")

# ---------- GPU 互斥（本地生图 ComfyUI ↔ 本地语音 IndexTTS2 共用一块显卡） ----------
# 一张卡扛不住两个模型同时驻留：出图前先停语音服务、出音前先停生图服务，
# 等对方进程真正退出（端口释放）再拉起自己。同一进程内全局串行，并发生成会排队。
GPU_EXCLUSIVE = _str("GA_GPU_EXCLUSIVE", "true").lower() not in ("false", "0", "no")
# 停掉对方服务后，最多等多少秒让显存真正释放（等端口关闭）
GPU_STOP_TIMEOUT_S = _int("GA_GPU_STOP_TIMEOUT_S", 45)

# ---------- 服务 ----------
HOST = _str("GA_HOST", "127.0.0.1")
# 默认端口故意避开 Windows 的「保留端口段」（excludedportrange）：启用 Hyper-V / WSL /
# Docker 后 winnat 会把成段端口留给自己，段内 bind 直接 WinError 10013，且这段范围每次
# 重启都可能漂。8765 恰好常落在保留段里（实测本机 8751-9350 全被占），所以默认换成
# 15000（远在动态端口 1024-15000 的上沿之外、也不在保留段）。被占用时 main 还会自动顺延。
PORT = _int("GA_PORT", 18888)
# 调试实例：另起端口连同一套数据库/数据目录做前端/接口联调时打开。
# 打开后本进程不写 pid 文件、不做启动僵尸清理、不起 worker 池（只提供 API + 静态页），
# 避免把主实例正在执行的 run 误标成「服务重启中断」，也避免两个进程抢同一个队列。
DEBUG_INSTANCE = _bool("GA_DEBUG_INSTANCE", False)

# ---------- 运行约束（设计 §6 / §7）----------
# 一次任务跑多久、调多少次工具都没有上限。这条只是「判死线」：某个 run 连续这么久
# 一个事件都没落（LLM 往返/工具调用/批量进度都会落事件），说明 worker 已经死了或
# 永久挂在外部调用上，界面不该一直挂「运行中」。30 分钟远大于任何单个正常步骤
# （单次 LLM 360s / 单张出图 300s / 单条 TTS 600s / 单条命令 300s）。
RUN_STALE_S = _int("GA_RUN_STALE_S", 1800)
# 用户点了「停止」（runs.cancel_requested=1）后，最多再等这么久还没落终态，就由看门狗
# 强制把 run 落成 cancelled。正常的取消是秒级的（取消看门狗 task.cancel() 会立刻掀翻
# LLM 流 / 现在的 run_command）。但如果卡在一个**不可取消**的 await 上（历史 bug：to_thread
# 跑同步子进程），cancel 掀不翻，run 就会永远停在 running、界面「停止中」一直转圈。
# 这条是最后一道兜底：置位取消后超过这么久仍未结束 = 已经僵死，强制收尸成 cancelled。
CANCEL_GRACE_S = _int("GA_CANCEL_GRACE_S", 60)
# 并行执行的 run 数 = 进程内 worker 数（每个 worker 一次跑一条 run）。单机 lite 模式是
# 单进程多协程：几条 run 靠 asyncio 并行 + 各自线程里的阻塞调用跑，不需要起多个进程。
# 2026-10-30 用户要求「多个会话不需要排队，可以同时开多个对话」：默认从 3 提到 64，
# 单机使用相当于不设人为上限（真正瓶颈是 CPU/GPU/内存与外部端点配额）。
MAX_CONCURRENT_RUNS = _int("GA_MAX_CONCURRENT_RUNS", 64)  # 并行执行的 run 数（跨会话）
# 单次工具调用的「无进展」硬超时（秒）。注意语义是**连续这么久没有任何进展**才算卡死，
# 不是总时长上限：批量出图/配音每出一张(条)会推进 t.progress_seq，只要在出就不断重置计时。
# 这样「11 张各 1-2 分钟、正常一张张出」的批量不会被整批误杀（实测事故：总时长 900s
# 一刀切，把正在正确出图的批量判超时）。工具内部另有自己的单张时限（命令 300s / 出图 600s /
# TTS 600s / LLM 360s），这里是第三层兜底：某条外部调用（下载、第三方 SDK、半死连接）
# 卡住不返回时把控制权还给模型。默认略高于单张最长时限（出图/TTS 600s），只有真正
# 「毫无音讯」的挂起才会命中。设为 0 关闭。
TOOL_HARD_TIMEOUT_S = _int("GA_TOOL_HARD_TIMEOUT_S", 900)
# 同一轮里模型一次最多发多少个工具调用。模型偶尔会把整段清单拆成几十个并行调用，
# 一轮塞爆线程池/外部连接数；这里做个上限保护（超出的调用直接判失败并提示分批）。
MAX_TOOL_CALLS_PER_ROUND = _int("GA_MAX_TOOL_CALLS_PER_ROUND", 12)
# 同项目允许多个 run 并行改工作区（改动审阅/回滚走写前快照，互不依赖）；关掉则同项目串行
SAME_PROJECT_PARALLEL = _bool("GA_SAME_PROJECT_PARALLEL", True)
LOCK_TTL_S = _int("GA_LOCK_TTL_S", 120)              # 写锁 TTL
LOCK_WAIT_S = _int("GA_LOCK_WAIT_S", 1800)           # 锁等待上限（30 分钟）
TOOL_TIMEOUT_S = _int("GA_TOOL_TIMEOUT_S", 300)
# 一条需求「清单没做完」时，系统最多自动续跑几轮（0 = 关闭自动续跑，回到手动点「继续」）。
# 每轮 = 几十次 LLM 往返，所以有上限：做不了的东西不能靠无限重试烧额度。
AUTO_CONTINUE_MAX = _int("GA_AUTO_CONTINUE_MAX", 2)
# 搜索/递归扫描类命令的单独时限（默认 60s，会被 TOOL_TIMEOUT_S 夹住）。
# 实测：`Get-ChildItem -Recurse -File -Include *.py,*.png …` 这种整库扫描在十万级文件的目录上
# 能跑几分钟，界面上一直挂「执行中…」，用户只能看着卡住。构建/测试需要 300s，扫描不需要：
# 扫这么久通常说明扫的范围不对，早点掐断 + 引导去用会剪枝的内置 grep/glob/list_dir 更划算。
SEARCH_TIMEOUT_S = max(10, min(_int("GA_SEARCH_TIMEOUT_S", 60), TOOL_TIMEOUT_S))
CMD_IDLE_TIMEOUT_S = _int("GA_CMD_IDLE_TIMEOUT_S", 120)
MAX_TEST_ROUNDS = _int("GA_MAX_TEST_ROUNDS", 3)      # 测试回环上限
EVENT_MAXLEN = _int("GA_EVENT_MAXLEN", 5000)
BRANCH_TTL_DAYS = _int("GA_BRANCH_TTL_DAYS", 30)

# 写工具受限目录（设计 §7.3.1）
PROTECTED_PREFIXES = (".venv", "venv", "__pycache__", ".git", ".trace/events")

# ---------- 运行命令白名单（按操作系统给） ----------
# 实测问题：模型经常凭直觉写「另一套系统」的命令（Windows 上写 ls/cat/pwd/2>/dev/null/`;` 串联，
# 或 Linux 上写 dir/where），每撞一次白名单就白烧一轮，还会反复重试同一个错命令。
# 所以：① 白名单本身按平台给；② 把「当前是什么系统 / shell / 该怎么写」同时注入系统提示与报错文案，
# 让它在第一条命令之前就知道该用哪套写法，撞墙时也能立刻自纠。
PLATFORM = "windows" if sys.platform == "win32" else ("macos" if sys.platform == "darwin" else "linux")
SHELL_NAME = "cmd.exe" if PLATFORM == "windows" else "/bin/sh"

# 两边都该有的：解释器 / 包管理 / 版本控制 / 回显 / 媒体转换（游戏项目要压视频、抽帧、转音频）
_COMMON_COMMANDS = [
    "python", "python3", "py", "pytest", "compileall", "ruff", "pip",
    "git", "npm", "npx", "node", "echo", "ffmpeg", "ffprobe",
]
# Windows（cmd.exe）——含常用只读查看/比较类内置命令（fc、comp、tree、attrib、ipconfig…），
# 这些以前全不在名单里，模型一猜就是「命令不在白名单：fc」，白烧一轮。
_WIN_COMMANDS = [
    "cd", "chdir", "pushd", "popd", "dir", "tree", "type", "more", "sort",
    "find", "findstr", "fc", "comp", "attrib", "where", "set", "cls", "chcp", "ver",
    "copy", "move", "ren", "del", "erase", "mkdir", "md", "rmdir", "rd",
    "xcopy", "robocopy", "expand", "subst",
    "start", "tasklist", "taskkill", "netstat", "ipconfig", "ping",
    "whoami", "hostname", "systeminfo", "driverquery",
    "powershell", "pwsh", "cmd", "curl", "tar",
]
# POSIX（/bin/sh）
_POSIX_COMMANDS = [
    "cd", "pwd", "ls", "tree", "cat", "head", "tail", "grep", "sed", "awk", "cut",
    "sort", "uniq", "wc", "diff", "file", "du", "df", "tee", "xxd", "od",
    "cp", "mv", "rm", "mkdir", "rmdir", "touch", "chmod", "ln", "stat", "basename", "dirname",
    "find", "xargs", "which", "env", "export", "bash", "sh", "true", "sleep", "date",
    "ps", "kill", "lsof", "curl", "wget", "tar", "unzip", "zip", "gzip", "gunzip",
]

# 含文件搬运/查找类：组长经常要把截图、构建产物搬到静态目录里再验证（cp/copy/move/mkdir…），
# 以前这类命令一律被拦（实测反馈：「命令不在白名单：cp」），逼它绕道 node -e 写脚本。
COMMAND_WHITELIST = list(dict.fromkeys(
    _COMMON_COMMANDS + (_WIN_COMMANDS if PLATFORM == "windows" else _POSIX_COMMANDS)
))

# ---------- 命令策略：默认 open（只拦真正的危险动作），不再按白名单卡语法 ----------
# 用户反馈（多次）：「为什么这么多白名单限制？不是语法对不对就行了吗？」
# 结论：语法交给 shell 自己判断，白名单那层只制造摩擦（每遇一条新命令就白烧一轮）。
# 现在的分工：
#   ① 危险动作 → 拦（不可逆的删盘/格式化/关机/远程执行/强推）；
#   ② bash 习惯 → 自动改写成当前 shell 的写法（/dev/null→2>nul、`;`→&&、`| head -N`→工具截取…）；
#   ③ 搜文本类（grep/rg）→ 直接改走内置 grep 工具，不再让命令失败；
#   ④ 其他一律放行，跑不对时由命令自己的报错说话。
# 需要回到严格白名单：GA_SHELL_POLICY=strict
SHELL_POLICY = (os.getenv("GA_SHELL_POLICY", "open") or "open").strip().lower()
if SHELL_POLICY not in ("open", "strict"):
    SHELL_POLICY = "open"

# 只拦这些：会毁掉机器/仓库/远端，且基本不可能出现在正常开发流程里
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\bformat\s+[a-z]:", "格式化磁盘"),
    (r"\b(rd|rmdir)\s+/s\s+/q\s+\"?[a-z]:(\\|\"|$)", "递归删除整个磁盘"),
    (r"\bdel\b[^\n]*\b[a-z]:\\\s*(\*|$)", "删除盘符根目录"),
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~/?|\"?[a-z]:\\?\*?\"?)\s*$", "递归删除根目录/家目录"),
    (r"\bdiskpart\b|\bbcdedit\b|\bbootrec\b|\bcipher\s+/w\b", "改分区/引导记录"),
    (r"\breg\s+(delete|add)\s+hklm", "改注册表 HKLM"),
    (r"\bnet\s+(user|localgroup)\b[^\n]*\s/add\b", "新增系统账号/提权"),
    (r"\b(shutdown|Restart-Computer|Stop-Computer)\b", "关机/重启"),
    (r"(curl|wget|iwr|Invoke-WebRequest)[^\n|]*\|\s*(bash|sh|iex|Invoke-Expression)", "下载即执行"),
    (r"powershell[^\n]*\s-(enc|encodedcommand)\b", "编码过的远程脚本"),
    (r"\bgit\s+push\b[^\n]*--force", "强推远端"),
    (r"\b(npm|pnpm|yarn)\s+publish\b", "发版到公共仓库"),
    (r"\bmkfs(\.\w+)?\b|\bdd\s+if=", "抹盘"),
]


def env_shell_hint() -> str:
    """当前系统的 shell 与命令写法（注入系统提示 / 白名单报错文案），避免模型凭直觉写另一套系统的命令。"""
    if PLATFORM == "windows":
        return (
            "Windows / cmd.exe。进目录 cd /d <目录>；"
            "列目录 dir、看文件 type <文件>、找程序 where <名字>、搬运 copy/move/del/mkdir；"
            "多条命令用 && 连接；丢弃输出写 2>nul。"
            "bash 习惯会被自动改写成 cmd 写法（/dev/null→2>nul、`;`→&&、$VAR→%VAR%、/c/x→C:/x、"
            "`| head -N`/`| tail -N`→去掉管道由工具截取），cp/mv/rm/ls/cat/pwd 会交给 powershell、"
            "which 会换成 where；但 grep/rg/sed/awk 没有可靠对应——搜文本一律用 grep 工具。"
        )
    return (
        f"{PLATFORM} / {SHELL_NAME}：进目录 cd、列目录 ls、看文件 cat、搜索 grep、翻尾部 tail；"
        "搬运 cp/mv/rm/mkdir、找程序 which；多条命令用 && 或 `;`，丢弃输出用 2>/dev/null。"
    )



def env_examples() -> str:
    """「这类事该怎么写」的常用命令范例——一开始就给，别让模型一边猜一边撞白名单。"""
    if PLATFORM == "windows":
        return (
            "列目录 dir /b · 看文件 type <f> · 看大文件尾部 powershell Get-Content <f> -Tail 20 · "
            "搜文本用 grep 工具（shell 里没有 grep，findstr 参数完全不同，别写）· "
            "比较两个文件 fc /b <a> <b>（更省事的是 read_file 两个文件）· "
            "查端口占用 netstat -ano | findstr :18888 · 查进程 tasklist | findstr node · "
            "杀进程 taskkill /F /PID <pid> · 压视频/转音频 ffmpeg -i in.mp4 out.webm"
        )
    return (
        "列目录 ls -la · 看文件 cat <f> · 看尾部 tail -n 20 <f> · 搜文本 grep -rn \"关键词\" . · "
        "比较文件 diff <a> <b> · 查端口 lsof -i :18888 · 查进程 ps aux | grep node · "
        "杀进程 kill -9 <pid> · 压视频/转音频 ffmpeg -i in.mp4 out.webm"
    )


def env_brief(cwd: str | None = None) -> str:
    """系统提示最前面的「运行环境」块。

    实测：模型不看环境就下命令（Windows 上写 ls/cat/`;`/2>/dev/null，Linux 上写 dir/where），
    被白名单拦一次就烧一轮，被「静默失效」坑一次更贵。所以把环境放在**上下文最前面**，
    而不是靠第一条命令去试探。
    """
    lines = [
        "## 运行环境（第一条命令之前先读这段，不要靠试错猜环境）",
        f"- 操作系统: {PLATFORM} · shell: {SHELL_NAME}"
        + (f" · 工作目录: {cwd}" if cwd else ""),
        "- **先想工具、再想 shell（90% 的探查根本不用 shell）**：搜文本用 grep 工具"
        "（pattern/path/include/context）、找文件用 glob、看某几行用 read_file(offset,limit)、"
        "读整篇用 read_file、找改动范围用 grep。**不要写 shell 的 grep/rg/findstr**"
        "（cmd 没有 grep，findstr 参数也完全不同，写了必被拦）；也不要 cat/head/tail 翻文件。"
        "shell 只留给：构建、跑测试、起/停服务、装依赖。",
        f"- 命令写法: {env_shell_hint()}",
        f"- 常用写法（直接照抄）: {env_examples()}",
        f"- 命令策略: {('只拦危险动作（删盘/格式化/关机/远程执行/强推），其余照常执行' if SHELL_POLICY == 'open' else '严格白名单（GA_SHELL_POLICY=strict）')}"
        f"；bash 习惯会自动改写成 cmd 写法；grep/rg 自动转成 grep 工具执行。",
        "- 常见可用写法: " + ", ".join(COMMAND_WHITELIST[:24]) + " …"
        "（清单外的命令也能跑，只是没在这里列出来）",
        "- 不确定怎么写时：能用 fs 工具（read_file/write_file/edit_file/glob/grep）就别绕 shell；"
        "非得跑就用 powershell -NoProfile -Command \"...\"、node -e \"...\"、python -c \"...\"。",
        "",
        "## 编辑规则（工具层强制，违反会被直接拦）",
        "- 改**已存在**的文件：先 read_file 拿到最新内容，再用 edit_file 精确替换（首选）或 write_file 整体重写；"
        "大文件用 offset/limit 分段读，只看要动的那几行也行。新建文件（路径不存在）和空文件不需要先读。",
        "- 你读过之后文件若被改动过（别的会话、构建产出、脚本生成），写入会被要求重新 read_file——照做，不要绕。",
        "- 整体重写会冲掉别人的改动：不确定就 edit_file 改局部；写完用 read_file 复查一眼。",
    ]
    return "\n".join(lines)


def ensure_dirs() -> None:
    for d in (DATA_DIR, PROJECTS_ROOT, LOG_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def default_python_bin() -> str:
    """项目用的默认 Python 解释器路径。

    普通源码运行时就是当前解释器（sys.executable）。但在 PyInstaller 打包出来的
    单文件 exe 里，sys.executable 指向 Trace.exe 本身——拿它去改写用户项目里的
    `python xxx.py` 命令会变成 `"Trace.exe" xxx.py`，必然跑错。所以冻结运行时
    返回空串（下游按 None 处理，命令保持原样走 PATH 里的 python）。
    """
    if getattr(sys, "frozen", False):
        return ""
    return sys.executable


def is_sqlite() -> bool:
    return DATABASE_URL.startswith("sqlite")
