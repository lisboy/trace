"""工具注册表 + 分发器（设计 §4.3）。

白名单在 AgentRuntime 处硬校验；这里是实现与 schema 的集中定义。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from game_agent.agents.tools import ToolContext, ToolError, ToolResult
from game_agent.agents.tools.native import (
    delete_file, edit_file, glob, grep, list_dir, load_skill, memory_read, read_file,
    run_command, save_env_fact, search_history, write_file,
)
from game_agent.agents.tools.collaboration import (
    generate_audio, generate_image as generate_image_batch,
    generate_video as generate_video_batch, report_to_user,
)
from game_agent.agents.tools.documents import parse_document
from game_agent.agents.tools.service import (
    screenshot_page, service_logs, start_service, stop_service, view_image,
    view_images,
)
from game_agent.agents.tools.specialized import (
    design_voice, edit_image, generate_music, generate_sfx, list_voice_presets,
    save_character_sheet, tts, upsert_voice_profile,
)
from game_agent.agents.tools.plan import update_plan
from game_agent.agents.tools.question import AskUserStop, ask_user
from game_agent.agents.tools.spawn import spawn_agents
from game_agent.agents.tools.web import web_fetch, web_search
from game_agent.llm.base import ToolSpec

TOOL_IMPLS: dict[str, Callable] = {
    # 原生
    "read_file": read_file, "write_file": write_file, "edit_file": edit_file,
    "delete_file": delete_file,
    "list_dir": list_dir, "glob": glob, "grep": grep, "run_command": run_command,
    "load_skill": load_skill, "search_history": search_history,
    "memory_read": memory_read,
    "save_env_fact": save_env_fact,
    # 文档解析（xlsx/docx/pdf）
    "parse_document": parse_document,
    # 联网（只读）
    "web_search": web_search, "web_fetch": web_fetch,
    # 服务启停与视觉验收
    "start_service": start_service, "stop_service": stop_service,
    "service_logs": service_logs, "screenshot_page": screenshot_page,
    "view_image": view_image,
    "view_images": view_images,
    # 协作与汇报
    "report_to_user": report_to_user,
    # 子agent 并行：把互不依赖的调研/核对拆成多个只读子agent并发执行
    "spawn_agents": spawn_agents,
    # 媒体直连：出图（批量版，支持 items）/ 出音频（voice/sfx/music + 音色设计）/ 出视频
    "generate_image": generate_image_batch, "generate_audio": generate_audio,
    "generate_video": generate_video_batch,
    "list_voice_presets": list_voice_presets,
    # 计划机制（助手维护待办清单）
    "update_plan": update_plan,
    "ask_user": ask_user,
    # 图像辅助（出图本体 generate_image 在上面直连注册）
    "edit_image": edit_image,
    "save_character_sheet": save_character_sheet,
    # 配音
    "tts": tts, "generate_sfx": generate_sfx, "generate_music": generate_music,
    "upsert_voice_profile": upsert_voice_profile,
    "design_voice": design_voice,
}

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "read_file": {"description": "读取工作区文件内容（写前必须先读）。大文件用 offset+limit 按行分页读，"
                                  "被截断时结果里会提示下一段从第几行读", "parameters": {
        "type": "object", "properties": {
            "path": {**_STR, "description": "工作区内相对路径"},
            "offset": {**_INT, "description": "从第几行开始（1 基），配合 limit 分页"},
            "limit": {**_INT, "description": "读多少行（默认 400，最大 2000）"}},
        "required": ["path"]}},
    "write_file": {"description": (
        "写入/新建文件（新文件可直接写；改已有文件需先 read_file）。"
        "大文档一次写完容易被输出长度上限截断参数，可拆成多块：第一块用默认 mode=write，"
        "后续每块带同一 path 且 mode=append 追加到同一文件。"), "parameters": {
        "type": "object", "properties": {
            "path": {**_STR, "description": "工作区内相对路径"},
            "content": {**_STR, "description": "要写入的文件正文"},
            "mode": {**_STR, "description": "write=整体写入/覆盖（默认）；append=追加到文件末尾（分块续写大文档时用）"}},
        "required": ["path", "content"]}},
    "edit_file": {"description": "精确替换文件中的一段文本（old_string 必须唯一命中）。"
                                    "old_string 从 read_file/grep 结果里照抄真实原文，不要凭印象重写；"
                                    "长段落只取要改的行 ±2 行，改多少贴多少。"
                                    "只差空白（缩进/行尾空格/全角空格）时会自动对齐；"
                                    "未命中会回传文件里最接近的段落，照它重试即可", "parameters": {
        "type": "object", "properties": {"path": _STR, "old_string": _STR, "new_string": _STR},
        "required": ["path", "old_string", "new_string"]}},
    "delete_file": {"description": "删除工作区内的一个文件（删文件就用本工具，不要用 run_command 想办法删；"
                                    "不支持目录，受保护路径如 .venv/.git 拒绝）", "parameters": {
        "type": "object", "properties": {"path": _STR}, "required": ["path"]}},
    "list_dir": {"description": "列出目录内容", "parameters": {
        "type": "object", "properties": {"path": _STR}}},
    "parse_document": {"description": "解析工作区里的 xlsx / docx / pdf 为文本（xlsx 所有列转 TSV 并标行号，"
                                       "docx 按正文段落/表格原顺序，pdf 每次只提取一页、默认第 1 页）。"
                                       "用 offset/limit 按字符分页，按末尾提示继续；不要用 read_file 读这些二进制文件", "parameters": {
        "type": "object", "properties": {
            "path": {**_STR, "description": "工作区内相对路径"},
            "sheet": {**_INT, "minimum": 1, "default": 1,
                      "description": "xlsx：第几个工作表（1 基），全部工作表名列在所选文本开头"},
            "page": {**_INT, "minimum": 1, "default": 1,
                     "description": "pdf：只提取第几页（1 基），不填只读第 1 页；换页时重置 offset=1"},
            "max_rows": {**_INT, "minimum": 1, "maximum": 2000, "default": 200,
                         "description": "xlsx：选取多少行（默认 200，上限 2000）；先用 offset 读完所选行的全部字符"},
            "start_row": {**_INT, "minimum": 1, "default": 1,
                          "description": "xlsx：选区起始行（1 基）；更换 sheet/start_row/max_rows 时重置 offset=1"},
            "offset": {**_INT, "minimum": 1, "default": 1,
                       "description": "所有格式：所选文本的 1 基 Unicode 字符位置（按码点，不是字节），含格式标签、Tab 和换行，"
                                      "不含分页头尾；继续时保持 path/sheet/start_row/max_rows/page 不变"},
            "limit": {**_INT, "minimum": 1, "maximum": 6000, "default": 6000,
                      "description": "所有格式：每次返回的所选文本字符数（按码点，默认/上限 6000），加分页头尾仍小于 8000 字符"}},
        "required": ["path"]}},
    "glob": {"description": "按通配符搜索文件（自动跳过 node_modules、构建产物、游戏引擎生成目录等噪音目录）",
             "parameters": {"type": "object", "properties": {"pattern": _STR}, "required": ["pattern"]}},
    "grep": {"description": "按正则搜索文件内容（带行号）。大项目建议用 path 限子目录、include 限后缀（如 *.py），"
                            "并善用 context 带上命中行上下若干行，一次看清一段代码，不必整读大文件；"
                            "node_modules/构建产物/引擎生成目录会自动剪枝", "parameters": {
        "type": "object", "properties": {
            "pattern": {**_STR, "description": "正则表达式"},
            "path": {**_STR, "description": "限定搜索范围（子目录或单个文件），不填=全工作区"},
            "context": {**_INT, "description": "命中行上下各带几行（0-20）"},
            "limit": {**_INT, "description": "最多返回多少条命中（默认 50，最大 400）"}},
        "required": ["pattern"]}},
    "run_command": {"description": "在工作区运行 shell 命令并等它跑完（编译/单测/lint 用它；"
                                    "要起常驻服务请用 start_service，不要用本工具挂服务）。"
                                    "默认策略只拦危险动作（删盘/格式化/关机/远程执行/强推），"
                                    "其余命令照常执行；bash 习惯会自动改写成 cmd 写法。"
                                    "⚠️ 不要用它递归扫全库（Get-ChildItem -Recurse / dir /s / find）："
                                    "超时比普通命令短得多（默认 60s），会被直接终止；"
                                    "找文件用 glob、找内容用 grep", "parameters": {
        "type": "object", "properties": {"command": _STR}, "required": ["command"]}},
    "start_service": {"description": (
        "后台启动服务（不阻塞，自动等端口就绪），返回 pid、url、日志路径。"
        "⚠️ 起服务验收是**重手段，默认不要主动用**：改完跑编译/构建/单测等静态验证即可汇报；"
        "只有用户明确要求看效果（起服务看看/截图给我/跑起来），或同一个问题多轮修改仍未解决时，"
        "才起服务 + screenshot_page 实测。"
        "默认策略与 run_command 一致（只拦危险动作），带空格的引号路径也能直接起；"
        "起停命令优先看 .trace/allow_commands.txt。验收完 stop_service 收掉。"), "parameters": {
        "type": "object", "properties": {
            "command": {**_STR, "description": "启动命令，例：python -m uvicorn game_agent.main:app --port 8000"},
            "port": {**_INT, "description": "服务监听端口（给了就等端口可连再返回）"},
            "ready_path": {**_STR, "description": "就绪后要访问的路径，默认 /"},
            "wait_s": {**_INT, "description": "最长等待秒数，默认 25"}},
        "required": ["command"]}},
    "service_logs": {"description": "查看本次任务后台服务日志尾部（服务起不来/报错时用它定位）", "parameters": {
        "type": "object", "properties": {
            "pid": {**_INT, "description": "不填=看本次任务所有服务"},
            "lines": {**_INT, "description": "看最后多少行，默认 60"}}}},
    "stop_service": {"description": "停止后台服务（不填 pid = 收掉本次任务全部）", "parameters": {
        "type": "object", "properties": {"pid": _INT}}},
    "screenshot_page": {"description": (
        "无头浏览器截取页面截图，截图会作为图片直接进入你的视野——你必须真的看图判断效果对不对。"
        "⚠️ 和 start_service 一样默认不主动用：静态验证能判断的改动不要截图验收；"
        "只在用户明确要求看效果、或同一问题多轮修改仍没解决时，才 start_service 起服务 → screenshot_page(url)。"), "parameters": {
        "type": "object", "properties": {
            "url": {**_STR, "description": "要截的地址，例：http://127.0.0.1:8000/runs"},
            "width": {**_INT, "description": "视口宽，默认 1440"},
            "height": {**_INT, "description": "视口高，默认 900（长页面可调大）"},
            "wait_ms": {**_INT, "description": "截图前等待渲染毫秒数，默认 1200"},
            "out": {**_STR, "description": "可选：保存到工作区的相对路径，默认 .trace/screenshots/"}},
        "required": ["url"]}},
    "view_image": {"description": (
        "看磁盘上已有的图片（自己截的图 / cdp 脚本出的截图 / 生成的图 / 用户放的参考图），"
        "图片会作为视觉输入直接进入你的视野。要看本地截图就用它，不要去读 base64 或用 screenshot_page "
        "开 file://（本地文件不走浏览器）。"
        "一次看多张美术图请用 view_images 合成一张预览图，不要逐张或并行 view_image。"
        "看图并回复后不再重复携带像素，需要再看时重新调用本工具。"), "parameters": {
        "type": "object", "properties": {
            "path": {**_STR, "description": "图片路径，相对工作区即可，例：target/shots/manor-check.png"}},
        "required": ["path"]}},
    "view_images": {"description": (
        "查看美术素材时，先从生成结果路径中选出最多9张，用本工具按3列网格合成一张压缩预览图。"
        "只发送这张合图，不逐张发送原图；格号对应返回的文件清单，原文件不改。"
        "超过9张分批查看；只有检查选中图片的细节时才单独用 view_image。"), "parameters": {
        "type": "object", "properties": {
            "paths": {"type": "array", "description": "已挑选的美术图片路径，按左到右、上到下排列",
                      "items": _STR, "minItems": 1, "maxItems": 9}},
        "required": ["paths"]}},
    "load_skill": {"description": "加载项目技能（.trace/skills/<name>.md，可用清单见上下文「可用技能」）", "parameters": {
        "type": "object", "properties": {"name": _STR}, "required": ["name"]}},
    "search_history": {"description": (
        "按关键词检索本会话的历史消息。⚠️ 只含**用户消息与助手最终回复**，"
        "不含工具调用轨迹（read/grep/run_command 的结果搜不到）。"
        "同一关键词只会返回一次结果；一次任务最多用 5 次。"
        "丢失上下文时最多查 1-2 次确认任务原话，然后直接 read_file/grep 看现场。"), "parameters": {
        "type": "object", "properties": {"keyword": _STR, "limit": _INT}, "required": ["keyword"]}},
    "memory_read": {"description": (
        "读取项目记忆正文。上下文里注入的只是「项目记忆索引」（标题+一句概括+8 位 id），"
        "动手前判断哪些条目与本轮相关，再用本工具取全文：一次最多 3 条，"
        "query 传关键词、或 ids 传索引里的 8 位 id（可多个）。与本轮无关的记忆不要读。"), "parameters": {
        "type": "object", "properties": {
            "query": {**_STR, "description": "关键词（命中标题/标签/正文）"},
            "ids": {"type": "array", "items": _STR,
                    "description": "索引里的 8 位 id，如 [\"3f9a1c2b\"]"}}}},
    "save_env_fact": {"description": (
        "把「这次试过才知道、以后每个任务都要用」的项目入口事实记进 .trace/env.json，"
        "从下一个任务起它会出现在上下文「环境事实与入口事实」里，不用再撞一遍。"
        "该记的：怎么起这个项目的服务（命令+端口）、访问哪个地址、用哪个测试账号登录、"
        "进哪个页面点哪里；已确认「本机没有」的命令由系统自动记，不用你管。"
        "一条一个键（test_account / start_command / login_entry 这种），value 一两句写完；"
        "长步骤写成 .trace/notes 经验。同键默认不覆盖，值真的变了带 force=true。"), "parameters": {
        "type": "object", "properties": {
            "key": {**_STR, "description": "事实名，小写字母/数字/下划线，≤40 字"},
            "value": {**_STR, "description": "事实内容（一两句，≤300 字）"},
            "force": {**_BOOL, "description": "已有一条要改它时才传 true"}},
        "required": ["key", "value"]}},
    "web_search": {"description": "联网搜索（需要查外部资料时用：报错查因、库文档、方案对比、行业资料）。"
                                   "返回标题+链接+摘要；要读某条全文再用 web_fetch(url)", "parameters": {
        "type": "object", "properties": {
            "query": {**_STR, "description": "搜索关键词（可中英文）"},
            "max_results": {**_INT, "description": "结果条数（默认 6，最多 10）"}},
        "required": ["query"]}},
    "web_fetch": {"description": "抓取网页并转为纯文本正文（跟进搜索结果、读在线文档/文章）。"
                                 "纯 JS 渲染或二进制页面会如实说明无法提取", "parameters": {
        "type": "object", "properties": {
            "url": {**_STR, "description": "完整 http(s) 地址"},
            "max_chars": {**_INT, "description": "正文字符上限（默认 6000，最大 20000）"}},
        "required": ["url"]}},
    "generate_image": {"description": (
        "直接生成真实图片（用户要图/立绘/卡面/头像/场景/图标/UI 素材时调用）。"
        "**要出多张（≥2）时用一次本工具 + items 或 count**：工具内部会串行一张一张出完，"
        "再把全部路径一次回报给你，界面上实时显示进度。不要为多张图反复单独调用，"
        "也不要先扫代码/文件再决定。"
        "prompt 写全主体+场景+构图+风格+光照；size 如 1024x1024 / 512x256；"
        "**高分辨率也支持**：可直接要 2360x1640（≈3.9MP，8G 显存约 5 分钟）等 2K 级尺寸；"
        "超过约 4MP（如 4K 3840x2160）会自动「基准分辨率采样 + 放大」两段式出图避免爆显存（更快但不增细节）；"
        "category 建议按用途（ui/character/illustration/scene）；"
        "需要透明底传 transparent=true；有参考图传 reference（工作区相对路径），"
        "reference_mode=style 只取画风（默认）或 img2img 以参考图重绘。"
        "**img2img 的重绘度由你按这次要改多少自己定 reference_denoise**（0~1，越大改动越多、"
        "越不保留原图）：只想小改（改颜色/加配饰/微调表情）用 0.3~0.45；要换背景/换动作/换场景但保住脸和主体"
        "用 0.5~0.65；只借参考图起个手、主体可明显变化用 0.75~0.9。不传则用服务默认（偏低≈0.42，"
        "基本保留原图，强改背景之类会改不动）。"
        "**项目已在素材库绑定了生图预设时，尺寸/LoRA/画风会被项目预设强制覆盖**，"
        "你传的 size 不生效；回报里会注明所用的预设名，不要声称用了别的尺寸。"
        "negative_prompt 传逆提示词（要避免的元素）；不传用服务默认负面词。"
        "**多视角/八方画像**：要一个人物的所有方向参考图（正面/左右侧/背后/各45°/俯视/仰视）时，"
        "传 views=[...]（视角 key：front/right/back/left/front_right_45/back_right_45/back_left_45/"
        "front_left_45/top/bottom，或 \"all\" 表示标准8方位），工具会按视角逐张串行出图，"
        "同一人物同一画风、只变观察方向，并自动给每个视角加文件名后缀。views 与 count 二选一。"
        "items=[{slug,prompt,size,category,transparent,reference,views,negative_prompt}] 一次最多 24 项；"
        "count=N 表示同一套参数出 N 张（slug 自动加序号）。"), "parameters": {
        "type": "object", "properties": {
            "prompt": _STR, "slug": _STR, "size": _STR, "category": _STR,
            "transparent": _BOOL, "reference": _STR,
            "reference_mode": {**_STR, "description": "style（默认）| img2img"},
            "reference_denoise": {**_STR, "description": "img2img 重绘度 0~1：0.3~0.45 小改(保构图)、0.5~0.65 换背景/改动作(保脸与主体)、0.75~0.9 大改；不传用服务默认(≈0.42，强改背景会改不动)"},
            "negative_prompt": {**_STR, "description": "逆向提示词：要避免的元素（如 多余手指、文字、水印、低清）；不传用服务默认"},
            "views": {**_STR, "description": "多视角/八方画像：视角 key 逗号分隔（front,left,back…）或 all=标准8方位；给了就逐张出各视角，忽略 count"},
            "count": _INT,
            "items": {"type": "array", "description": "要出的图清单，顺序即出图顺序",
                      "items": {"type": "object", "properties": {
                          "slug": _STR, "prompt": _STR, "size": _STR,
                          "category": _STR, "transparent": _BOOL,
                          "reference": _STR, "reference_mode": _STR,
                          "reference_denoise": _STR, "negative_prompt": _STR,
                          "views": _STR, "note": _STR}}}},
        "required": ["prompt"]}},
    "generate_video": {"description": (
        "直接生成真实视频（动态素材/过场动画/镜头片段/角色动作演示时调用）。"
        "走本地 ComfyUI（Wan2.2 TI2V 5B，原生 24fps 出片、自动补到 60fps 顺滑，8G 可跑，单条约 2–5 分钟）。"
        "prompt 写全主体+动作+镜头运动+场景+风格+光照；不要指望文字清晰（视频模型不擅长写字）。"
        "**漫剧/角色镜头要稳不要猛**：动作幅度宁小勿大（眨眼、轻微转头、衣袂飘动即可），"
        "prompt 里点明「保持原图画风/二次元插画风、背景延续原图」，别让模型把 2D 立绘画成 3D 塑料手办；"
        "单段控制在 3–6 秒，长镜头拆成多段短片段分别生成再拼接，能显著减少脸部扭曲与画面漂移。"
        "**有角色参考图一定要传 reference**（工作区相对路径）→ 以该图为**首帧**做图生视频，"
        "按目标宽高等比缩放（不裁脸、透明底自动铺浅灰底防黑背景），是锁住人物外貌最有效的手段；不传 → 文生视频。"
        "size 如 832x480；duration_s 秒数（帧数按 4n+1 收敛）；fps 默认 24。"
        "**要更长、更连贯的单镜头用接龙**：传 segments（段数，每段默认 1 秒）——逐段以上一段"
        "**最后一帧**为首帧续写，串成任意长且动作/画面连贯的长片（比一次生成超长片段更稳、不爆显存）；"
        "接龙时各段共用同一条 prompt（描述整段的连续动作），segments 即总秒数，最多 30 段。"
        "category 建议 cutscene/loop/banner 等。"
        "**要出多条时用一次本工具 + items 或 count**（单条 2–5 分钟，最多 6 条），"
        "工具内部串行一条一条出完再一次性回报全部路径，界面实时显示进度；不要为多条反复单独调用。"
        "items=[{slug,prompt,size,category,reference,duration_s,frames,segments}] 一次最多 6 项。"), "parameters": {
        "type": "object", "properties": {
            "prompt": _STR, "slug": _STR, "size": _STR, "category": _STR,
            "reference": {**_STR, "description": "首帧参考图（工作区相对路径），给了就是图生视频；角色镜头强烈建议传"},
            "duration_s": {**_STR, "description": "时长（秒），会按帧率换算帧数并按 4n+1 收敛；漫剧建议 3–6 秒"},
            "frames": {**_INT, "description": "直接指定帧数（4n+1 如 17/33/49/81），优先于 duration_s"},
            "segments": {**_INT, "description": "接龙段数（每段约1秒）：>0 时逐段接上一段尾帧出连贯长片，等于总秒数，最多30"},
            "segment_seconds": {"type": "number", "description": "接龙每段秒数（默认1，帧数按4n+1取整）"},
            "fps": {**_INT, "description": "帧率（默认 24，Wan2.2 原生 24fps）"},
            "count": _INT,
            "items": {"type": "array", "description": "要出的视频清单，顺序即出片顺序",
                      "items": {"type": "object", "properties": {
                          "slug": _STR, "prompt": _STR, "size": _STR, "category": _STR,
                          "reference": _STR, "duration_s": _STR, "frames": _INT,
                          "segments": _INT, "note": _STR}}}},
        "required": ["prompt"]}},
    "generate_audio": {"description": (
        "直接生成真实音频（配音/CV/台词/语音/音效/BGM）。"
        "**任何配音生成都必须用本工具，这是唯一允许的配音入口**："
        "不许自己 start_service 起 IndexTTS2、"
        "不许跑 cv_gen.py / gen-cv-indextts.py / cv-presets.py 或自写脚本调语音接口。"
        "例外：只有用户明确说「按之前 python 脚本的方式生成」才可走老管线。"
        "kind=voice|sfx|music（默认 voice）：voice 必填 text（要念的台词）；"
        "sfx/music 必填 prompt（声音描述，如「清脆的按钮点击声」「温柔的主城背景音乐」），"
        "不需要 text。sfx/music 无外部供应商时由内置合成器产出真实可听音频（provider=synth），"
        "不再是无声音的静音占位。"
        "voice 传音色预设名（IndexTTS2 服务端已创建的预设，如「缘·温小令」）："
        "每个角色用它自己的预设实现一人一嗓，不传则用音频档位默认音色；"
        "远程 openai_tts 档位则传内置音色名。"
        "**用户要新音色（不要旧预设/换声线/按文字描述造音色）时**：传 design_desc=音色描述"
        "（如「妩媚、诱人、勾魂的九尾狐少女，气声多、语速偏慢」）+ preset_name=新预设名，"
        "工具会先造音色存预设再合成，不需要你先起语音服务或跑脚本。"
        "批量时 design_desc/preset_name 只写在顶层一次即可（整批共用这个新音色），"
        "不要在 items 每一条里重复写——每写一次都会多造一遍音色、极慢。"
        "**要出多条（≥2）时用一次本工具 + items 或 count**：工具内部会串行一条一条合成完"
        "（界面上实时显示第几条/真实还是占位），再把全部路径一次回报给你。"
        "不要为多条台词反复单独调用。items=[{slug,text,voice,kind,note}] 一次最多 24 项；"
        "count=N 表示同一套参数出 N 条（slug 自动加序号）。"
        "out 可以给具体文件名（web/audio/cv/me/enter.mp3），也可以给目录（以 / 结尾，"
        "如 web/audio/cv/me/）——目录形式会按每条 slug 自动落成独立文件，不会互相覆盖。"
        "**情绪（IndexTTS2）**：只给音色参考音频时模型会用平静语气，台词一多就"
        "「三句一个调」。想有感情就用：emo_text=情绪描述（如「压抑着怒气的低吼」）；"
        "或 emo_vector=8 维权重（高兴,愤怒,悲伤,害怕,厌恶,忧郁,惊讶,平静）；"
        "或 emo_audio=一段带目标情绪的工作区干声路径。三种给其一即可（emo_weight 控强度 0~1）。"
        "**语速与语言（IndexTTS-2.5）**：speed=语速 0.5~2.0（默认 1.0；>1 更慢更从容，"
        "<1 更快更急促，用于角色语速差异/强调重点）；lang=语言 zh/en/ja/es/ar"
        "（默认 auto 由服务端按台词自动判定）。需要读慢一点/快一点时直接传 speed，"
        "不用在台词里加省略号硬撑停顿。"), "parameters": {
        "type": "object", "properties": {
            "text": {**_STR, "description": "要念的台词（kind=voice 时必填；sfx/music 不需要）"},
            "prompt": {**_STR, "description": "声音描述（kind=sfx/music 时必填，如「清脆的按钮点击声」）"},
            "voice": _STR, "kind": _STR, "slug": _STR, "count": _INT, "note": _STR,
            "duration_s": {**_INT, "description": "BGM 时长（秒，默认 30，仅 kind=music）"},
            "out": {**_STR, "description": "工作区相对输出路径（可选）：给文件名如 web/audio/cv/ur-yx-01/enter.mp3；给目录（以 / 结尾）如 web/audio/cv/me/ 时按每条 slug 自动命名"},
            "design_desc": {**_STR, "description": "新音色文字描述（要造新音色时用）"},
            "preset_name": {**_STR, "description": "新音色保存成哪个预设名"},
            "design_say": {**_STR, "description": "音色设计试音句（可省，默认用 text）"},
            "design_n": {**_INT, "description": "音色设计候选条数（1-5，默认 1）"},
            "emo_mode": {**_STR, "description": "情绪模式 0=同参考(平) 1=情感音频 2=八维向量 3=文本描述；一般不用填，给下面素材会自动判定"},
            "emo_text": {**_STR, "description": "文本描述情绪（如「愤怒地质问」「温柔地哄」）"},
            "emo_weight": {**_STR, "description": "情绪强度 0~1（默认 0.65）"},
            "emo_vector": {"type": "array", "description": "8 维情感权重，顺序：高兴,愤怒,悲伤,害怕,厌恶,忧郁,惊讶,平静（每项 0~1，总和≤0.8 效果最好）",
                           "items": {"type": "number"}},
            "emo_audio": {**_STR, "description": "情感参考音频（工作区相对路径，一段带目标情绪的干声）"},
            "speed": {**_STR, "description": "语速 0.5~2.0（默认 1.0；>1 慢 <1 快，IndexTTS-2.5 生效）"},
            "lang": {**_STR, "description": "语言 zh/en/ja/es/ar（默认 auto 按台词自动判定，2.5 生效）"},
            "items": {"type": "array", "description": "要合成的音频清单，顺序即合成顺序",
                      "items": {"type": "object", "properties": {
                          "slug": _STR, "text": _STR, "voice": _STR, "kind": _STR,
                          "out": _STR, "design_desc": _STR, "preset_name": _STR,
                          "emo_mode": _STR, "emo_text": _STR, "emo_vector":
                              {"type": "array", "items": {"type": "number"}},
                          "speed": _STR, "lang": _STR,
                          "note": _STR}}}},
        # 不能把 text 设成 required：sfx/music 只传 prompt 就该放行（voice 缺 text
        # 由 generate_audio/_audio_jobs 在业务层报错）。宿主层会先按 schema 校验，
        # 之前这里留着 required=["text"]，导致 kind=sfx 的调用一律被「缺 text」拦下。
        "required": []}},
    "report_to_user": {"description": (
        "向用户汇报最终结果（收尾总结）。**这是收尾动作**：调用后本轮执行即结束，"
        "所以只在活确实做完、要交付时调用；待办清单还没做完就别调它"
        "（会被当成中途进度、在界面上标成「进度」后打回继续做）。"
        "必须写清「原因 / 说明 / 风险点」，不能只罗列改了哪些文件。"
        "建议按六段写：①目标（为什么做）②改动（文件→做了什么、为什么这样改）"
        "③原因与说明（根因/方案取舍/假设前提）④风险点与影响（副作用、边界、未覆盖场景，必写）"
        "⑤验证证据（编译/单测/截图结论；哪些没验证要如实说）⑥回滚与后续（出问题怎么退、还没做完的项）。"
        "最后一句结论说清能不能用、还要用户做什么。"
        "另外：活跑得久、想中途让用户知道到哪一步了，也可以调本工具发一两句口语化的进度"
        "（不必套上面六段格式、也不用凑齐各段），界面会把它标成「进度」；说不说、说几次自己判断，"
        "没有新进展就别硬发。真正的交付结论是全部做完后的最后一次汇报。"), "parameters": {
        "type": "object", "properties": {"report": _STR}, "required": ["report"]}},
    "spawn_agents": {"description": (
        "并行派发只读子agent 调研/核对，汇总结论返回。"
        "适用：互不依赖的多条查询/核对（并行查 N 个文件的同一类点、并行查多个资料源、"
        "并行核对多份文档），一次性发出去、并行跑完再汇总，比主agent 一条条串行查快；"
        "「多个问题同时处理」正是本工具的用途——只要这些子任务都是只读调研，就该并行派。"
        "子agent 只读（read/grep/glob/web/memory/view），不写文件不跑命令；"
        "items 每项 {name(子任务名), question(要核对的具体问题), note(可选)}，"
        "一次最多 6 项、至少 2 项（2 条以上互不依赖的核对值得并行派；只有 1 条自己读更快）。"
        "真正要改代码/生成素材/跑命令的事不要派给子agent——它们只能查和回答，"
        "改代码必须主agent 自己做（拿到子agent 的核对结论后自己 edit_file 落地）。"),
        "parameters": {
            "type": "object", "properties": {
                "items": {"type": "array", "description": "子任务清单，每项一个独立问题",
                          "items": {"type": "object", "properties": {
                              "name": {**_STR, "description": "子任务名（≤40字）"},
                              "question": {**_STR, "description": "要核对的具体问题（给足路径/关键词/判断标准）"},
                              "note": {**_STR, "description": "可选：补充约束（如只看某几行）"},
                          }, "required": ["question"]}},
            }, "required": ["items"]}},
    "update_plan": {"description": (
        "维护当前任务的待办清单（计划机制，用户界面实时显示勾选进度）。"
        "问题较复杂（≥3 步 / 多文件 / 多角色协作）时，开工前先列出 3-8 项待办（全部 pending，"
        "第一项可置 in_progress）；每完成一步立刻把对应项改成 completed 后再次调用本工具"
        "（传完整清单，整体覆盖）；简单问题不必调用。"), "parameters": {
        "type": "object", "properties": {
            "items": {"type": "array", "description": "完整待办清单，顺序即展示顺序（整体覆盖）",
                      "items": {"type": "object", "properties": {
                          "content": {**_STR, "description": "一项可独立验证的具体动作"},
                          "status": {"type": "string", "enum": ["pending", "in_progress", "completed"],
                                     "description": "pending=未开始 / in_progress=进行中 / completed=已完成"},
                          "note": {**_STR, "description": "可选：证据/文件路径/结论（简短）"},
                      }, "required": ["content"]}},
            "summary": {**_STR, "description": "可选：一句话说明本次更新（如「已完成第 2 步」）"},
        }, "required": ["items"]}},
    "ask_user": {"description": (
        "遇到你无法自行决定的事（业务口径不确定、两种方案各有取舍、缺关键信息、"
        "会影响用户预期/钱/数据的决策）时，向用户提问并等待回答——**不要自己拍板**。"
        "调用后本次执行会在原地暂停等用户回答（同一个任务继续，不结束本轮、不丢待办清单），"
        "用户在前端点选项或输入答案后你就能拿到答案接着往下做。"
        "问题要具体、可回答；选项之间要互斥且各自讲清后果。能自己查证/按既有约定处理的，不要问。"),
        "parameters": {
            "type": "object", "properties": {
                "question": {**_STR, "description": "要确认的问题（一句话说清要你拍板什么）"},
                "options": {"type": "array", "description": "2-5 个互斥选项（推荐给出，用户点一下即可）",
                            "items": {"type": "object", "properties": {
                                "label": {**_STR, "description": "选项结论（如「允许 → 方案B改SQL」）"},
                                "detail": {**_STR, "description": "可选：该选项的后果/影响范围"},
                            }, "required": ["label"]}},
                "allow_text": {**_BOOL, "description": "是否允许用户自由输入答案（默认 true）"},
            }, "required": ["question"]}},
    "edit_image": {"description": "改图（占位模式下不可用，请用 generate_image 重画）",
                   "parameters": {"type": "object", "properties": {"slug": _STR}}},
    "save_character_sheet": {"description": "保存角色多视图设定（JSON）", "parameters": {
        "type": "object", "properties": {"slug": _STR}}},
    "list_voice_presets": {"description": (
        "列出语音服务里已保存的音色预设名（给 generate_audio 的 voice 参数用）。"
        "不确定有哪些音色、或用户说「用之前的音色」时先调这个，"
        "不要用 dir 去翻语音服务安装目录。"), "parameters": {
        "type": "object", "properties": {}}},
    "tts": {"description": "文本转语音（generate_audio 的底层单条实现，优先用 generate_audio）。"
                           "voice 传音色预设名（IndexTTS2 已创建的预设，一人一嗓），"
                           "不传用档位默认；out 可指定工作区相对输出路径"
                           "（如 web/audio/cv/ur-yx-01/enter.mp3，会自动转 24k 单声道 mp3）；"
                           "情绪可用 emo_text / emo_vector / emo_audio 三种之一；"
                           "speed=语速 0.5~2.0（>1 慢 <1 快，2.5 生效）；lang=zh/en/ja/es/ar（默认 auto）；"
                           "无供应商走占位静音", "parameters": {
        "type": "object", "properties": {"text": _STR, "voice": _STR, "slug": _STR,
                                         "out": {**_STR, "description": "工作区相对输出路径（可选）"},
                                         "emo_mode": _STR, "emo_text": _STR,
                                         "emo_weight": _STR,
                                         "emo_vector": {"type": "array", "items": {"type": "number"}},
                                         "emo_audio": _STR,
                                         "speed": _STR, "lang": _STR}}},
    "generate_sfx": {"description": "生成音效（内置合成器出真实可听波形，provider=synth；prompt 写声音描述）",
                     "parameters": {"type": "object", "properties": {
                         "slug": _STR, "prompt": _STR, "out": _STR, "placeholder": _BOOL}}},
    "generate_music": {"description": "生成 BGM（内置合成器出真实可听循环片段，provider=synth；prompt 写风格描述）",
                       "parameters": {"type": "object", "properties": {
                           "slug": _STR, "prompt": _STR, "duration_s": _INT, "out": _STR,
                           "placeholder": _BOOL}}},
    "upsert_voice_profile": {"description": "保存音色档案", "parameters": {
        "type": "object", "properties": {"slug": _STR}}},
    "design_voice": {"description": (
        "按文字描述造一个新音色（VoxCPM2 音色设计），并保存成 IndexTTS2 预设。"
        "一般用 generate_audio(design_desc=..., preset_name=...) 即可；本工具是底层实现。"
        "造好后用 tts(voice=预设名) 合成台词。语音服务由工具内部自动拉起，不要自己起服务。"),
        "parameters": {"type": "object", "properties": {
            "desc": {**_STR, "description": "音色文字描述（年龄/气质/音色/语速/气声等）"},
            "name": {**_STR, "description": "保存的预设名（一人一嗓，如「涂山月见·媚」）"},
            "say": {**_STR, "description": "试音句（可选）"},
            "n": {**_INT, "description": "候选条数 1-5，默认 1"}},
            "required": ["desc", "name"]}},
}

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(name=n, description=d["description"], parameters=d["parameters"])
    for n, d in TOOL_SCHEMAS.items()
]

# 只读查询工具挂在描述上的「一次问全」提示。实测某会话 84 次 LLM 往返里 57 次只发一个
# 工具调用，把「文件叫什么 → 在哪目录 → 哪一行」串成三次串行等待；运行循环本来就会
# 并行执行同一批调用（见 runtime），是模型不知道可以这么干。
# 挂在 schema 描述上而不是只写系统提示词：系统提示会被上下文压缩挤掉，工具描述不会。
_QUERY_BATCH_HINT = ("（与别的只读查询互不依赖时，在同一次回复里一起调用这几个工具"
                     "（read_file / grep / glob / list_dir），系统会并行执行、一次性把全部结果给你；"
                     "只有后一步的参数要等前一步结果时才分开调用。）")
_QUERY_BATCH_TOOLS = ("read_file", "grep", "glob", "list_dir")


def build_tool_specs(whitelist: list[str],
                     extra_specs: dict[str, dict] | None = None) -> list[ToolSpec]:
    """白名单 → ToolSpec 列表。extra_specs 为 MCP 等动态工具（name → {description, parameters}），
    优先于静态注册表。"""
    out: list[ToolSpec] = []
    extra = extra_specs or {}
    seen: set[str] = set()
    for name in whitelist:
        if name in extra:
            meta = extra[name]
            out.append(ToolSpec(name=name, description=meta.get("description", ""),
                                parameters=meta.get("parameters") or {"type": "object", "properties": {}}))
            seen.add(name)
    for spec in TOOL_SPECS:
        if spec.name in whitelist and spec.name not in seen:
            if spec.name in _QUERY_BATCH_TOOLS:
                spec = ToolSpec(name=spec.name, description=spec.description + _QUERY_BATCH_HINT,
                                parameters=spec.parameters)
            out.append(spec)
    return out


def _truncate(s: str, n: int = 8000) -> str:
    return s if len(s) <= n else s[:n] + f"\n...[截断，共 {len(s)} 字符]"


async def execute_tool(name: str, args: dict, t: ToolContext) -> "str | ToolResult":
    t.call_seq += 1
    if name.startswith("mcp_"):
        # MCP 动态工具：mcp_<server>_<tool>，由项目 MCP 配置（界面/文件）声明
        from game_agent.agents.tools.mcp_client import get_mcp_manager
        mgr = await get_mcp_manager(t.run_ctx.project_id, t.workspace)
        if mgr is None:
            return "工具错误：未配置 MCP 服务器（配置页或 .trace/mcp.json）"
        try:
            return _truncate(await mgr.call(name, args or {}))
        except Exception as e:  # noqa: BLE001
            return f"MCP 调用异常：{type(e).__name__}: {e}"
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return f"工具不存在：{name}"
    try:
        result = await impl(args or {}, t)
        if isinstance(result, ToolResult):  # 带图（截图回灌视觉）
            return ToolResult(text=_truncate(result.text), images=result.images)
        return _truncate(str(result))
    except ToolError as e:
        return f"工具错误：{e}"
    except AskUserStop:
        # 抛给用户的问题：必须冒泡到 orchestrator（否则会被当成工具错误吞掉，
        # 模型会以为提问失败然后自己拍板——正是这次要修的行为）
        raise
    except Exception as e:  # noqa: BLE001
        return f"工具执行异常：{type(e).__name__}: {e}"


async def execute_tool_with_timeout(name: str, args: dict, t: ToolContext,
                                    timeout_s: float | None = None) -> "str | ToolResult":
    """在 execute_tool 外面再套一层「无进展超时」兜底（不是总时长上限）。

    为什么改成「无进展」而不是「总时长」：批量出图/配音是**串行**一张张出，单张可能
    1-2 分钟，十几张正常也要十几分钟。以前这里是固定总时长（默认 900s）一刀切，结果
    一批 11 张、每张都正常出图，到第 900s 被整批判超时中止——用户看到的就是「明明
    一张张在正确出图，却断了」。正确的判据是**单张/单条是否超时**：工具内部每出一张
    都会 t.progress_seq += 1（见 collaboration._image_batch/_audio_batch），这里每 2s
    轮询一次，只要 progress_seq 在变就说明在正常推进，计时重来；连续 timeout_s 秒
    没有任何进展才判定真卡死并中止。

    对不发进展的工具（run_command 等），progress_seq 不变，等价于「总时长 timeout_s」，
    仍是原来的挂死兜底行为——只有真正毫无音讯的挂起才会命中。
    """
    if not timeout_s or timeout_s <= 0:
        return await execute_tool(name, args, t)

    # 轮询粒度：正常取 2s；超时值很小时（测试/极短兜底）按 1/4 缩小，保证能观察到进展。
    step = 2.0 if timeout_s >= 8 else max(0.02, timeout_s / 4.0)
    start_seq = t.progress_seq
    idle = 0.0
    task = asyncio.create_task(execute_tool(name, args, t))
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=step)
            if task in done:
                return task.result()
            if t.progress_seq != start_seq:
                # 有进展（出了一张/一条）：重置计时，从此刻重新算「多久没动静」
                start_seq = t.progress_seq
                idle = 0.0
                continue
            idle += step
            if idle >= timeout_s:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 取消产生的异常无所谓
                    pass
                return (f"工具 {name} 连续 {timeout_s:g} 秒没有任何进展，已强制中止。"
                        "通常是外部命令/接口挂死或网络卡住——换一种做法，或如实说明卡在哪一步。")
    finally:
        if not task.done():
            # 关键：外层被取消（用户点停止 / run 看门狗掐任务）时，这里必须把内层工具任务
            # 真正 cancel **并 await 它结束**。只 cancel 不 await 的话，内层 run_command 的
            # `except CancelledError: _kill_tree(proc)` 还没执行到，事件循环就转向收尾——
            # 子进程（pytest / npm build）会变成孤儿继续跑，甚至把还在用的文件锁住。
            # 用 wait_for 包一层：万一某个工具吞掉了 CancelledError，也不能把收尾卡死。
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 取消产生的异常无所谓
                pass
