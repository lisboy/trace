"""请求体字节预算（413 防线）的回归测试。

为什么单独一组：token 预算管不住图片 —— 一张图在 token 口径里只值 900，
真实字节却是 MB 级。实测（llm_transcripts，2026-10-29）用户附图单张最大 4.72MB
base64、一次 run 314 轮里 256 轮都带着图，前置网关直接回 413，整轮白跑、run 判失败。
所以「按字节裁图」和「回灌只活一轮」必须有测试钉住，否则下次改上下文又会退回原样。
"""
from __future__ import annotations

import asyncio
import base64
import io

import pytest

from game_agent.agents.tools import ToolContext
from game_agent.agents.tools.service import view_images
from game_agent.agents.tools.native import read_file
from game_agent.llm.base import LlmPayloadTooLarge
from game_agent.llm.context_window import (
    MAX_IMAGES_PER_REQUEST, MEDIA_BYTES_BUDGET, MEDIA_DROPPED_NOTE, VIEWBACK_MARK,
    compress_messages, expire_viewback, fit_media, media_stats,
)
from game_agent.llm.openai_compat import _classify_http_error

# 一张「假装很大」的图：内容无所谓，字节数才是被测对象
BIG = "data:image/png;base64," + "A" * (3 * 1024 * 1024)
SMALL = "data:image/jpeg;base64," + "B" * 100_000


def _shot(url: str, n: int = 1) -> dict:
    """一条 runtime 风格的「截图回灌」user 消息。"""
    return {"role": "user", "content": [
        {"type": "text", "text": f"{VIEWBACK_MARK}（以上截图已附在本条消息中）"},
        *[{"type": "image_url", "image_url": {"url": url}} for _ in range(n)],
    ]}


def _text(text: str) -> dict:
    return {"role": "user", "content": text}


# ---------- 回灌：只留最新一张，且它跨模型回复仍在 ----------

def test_only_the_newest_viewback_keeps_pixels() -> None:
    msgs = [_text("需求"), _shot(SMALL), _text("助手回合"), _shot(SMALL)]
    out, dropped = expire_viewback(msgs)
    assert dropped == 1
    assert media_stats(out)["images"] == 1
    # 被过期的是**更早**那条，最近这条必须还看得见像素
    assert any(p.get("type") == "image_url" for p in out[3]["content"])
    old_text = " ".join(str(p.get("text") or "") for p in out[1]["content"])
    assert MEDIA_DROPPED_NOTE in old_text
    assert not any(p.get("type") == "image_url" for p in out[1]["content"])


def test_single_viewback_untouched() -> None:
    msgs = [_text("需求"), _shot(SMALL)]
    out, dropped = expire_viewback(msgs)
    assert dropped == 0 and out is msgs


def test_user_attachment_is_not_viewback() -> None:
    """用户自己贴的参考图不带回灌标记：它要跟着整个 run，不能被过期规则吃掉。"""
    anchor = {"role": "user", "content": [
        {"type": "text", "text": "（用户随本条需求附上 1 张图片，已作为视觉输入提供）"},
        {"type": "image_url", "image_url": {"url": SMALL}}]}
    out, dropped = expire_viewback([anchor, _shot(SMALL)])
    assert dropped == 0
    assert media_stats(out)["images"] == 2


# ---------- 字节预算 ----------

def test_fit_media_caps_image_count() -> None:
    msgs = [_text("需求"), _shot("data:image/jpeg;base64,AAAA", n=MAX_IMAGES_PER_REQUEST + 3)]
    out, dropped = fit_media(msgs)
    assert media_stats(out)["images"] == MAX_IMAGES_PER_REQUEST
    assert dropped == 3


def test_fit_media_caps_bytes_and_keeps_newest() -> None:
    # 每张小图 100KB，预算 400KB → 从最新往前装，装不下的（最旧的）被省略
    msgs = [_text("需求")] + [_shot(SMALL) for _ in range(40)]
    out, dropped = fit_media(msgs, max_bytes=400_000)
    st = media_stats(out)
    assert st["bytes"] <= 400_000
    assert st["images"] == 3 and dropped == 37
    assert any(p.get("type") == "image_url" for p in out[-1]["content"])


def test_single_oversized_image_is_dropped_with_a_path_hint() -> None:
    """历史里已经存下的 4.7MB 附图：整张摘掉，但必须留下「怎么再打开」的话。"""
    msgs = [_text("需求"), _shot(BIG)]
    out, dropped = fit_media(msgs, max_bytes=MEDIA_BYTES_BUDGET)
    assert dropped == 1
    assert media_stats(out)["images"] == 0
    text = " ".join(str(p.get("text") or "") for p in out[1]["content"])
    assert "view_image" in text


def test_within_budget_is_a_noop() -> None:
    msgs = [_text("需求"), _shot(SMALL)]
    out, dropped = fit_media(msgs)
    assert dropped == 0 and out is msgs


def test_compress_messages_applies_media_budget_even_when_tokens_fit() -> None:
    """token 远未超预算也要裁图 —— 否则等于没有这道闸（413 就是这么来的）。"""
    msgs = [_text("系统"), _text("需求")] + [_shot(SMALL) for _ in range(30)]
    rep: dict = {}
    out = asyncio.run(compress_messages(msgs, 128_000, None, rep, tools=None))
    assert rep.get("compressed") is False  # token 侧确实不需要压缩
    assert media_stats(out)["bytes"] <= MEDIA_BYTES_BUDGET
    assert rep.get("media_expired", 0) + rep.get("media_dropped", 0) > 0


# ---------- 附图入库即压缩 ----------

def _noisy_png_data_url(w: int = 2000, h: int = 2000) -> str:
    """噪声图：压不出冗余，PNG 原样大 —— 用来复现用户贴 4MB 截图的场景。"""
    import os

    from PIL import Image
    buf = io.BytesIO()
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_attachment_is_reencoded_before_it_enters_context() -> None:
    from game_agent.api.routers.sessions import MAX_IMAGE_STORED_CHARS, _shrink_image_data_url
    from PIL import Image

    url = _noisy_png_data_url()
    assert len(url) > 3_000_000, "构造的样本本身要够大，否则测不出压缩"
    out = _shrink_image_data_url(url)
    assert out.startswith("data:image/jpeg;base64,")
    assert len(out) <= MAX_IMAGE_STORED_CHARS
    img = Image.open(io.BytesIO(base64.b64decode(out.split(",", 1)[1])))
    assert max(img.size) <= 1280, "长边要收到视觉够用的尺寸"


def test_attachment_original_is_kept_on_disk(tmp_path, monkeypatch) -> None:
    """压缩只针对「进上下文的那一版」：原图必须留在稳定目录，不然素材就毁了。

    2026-11-05 起原图不再落在工作区 ``.trace/uploads/``（工作区换机器就丢），
    改存 DATA_DIR 下的 chat_uploads（见 core/chat_images.py），标记为
    ``chat-uploads/<team>/<file>``。这里把根目录指到 tmp_path 隔离真实数据。
    """
    from game_agent.core import chat_images
    from game_agent.api.routers.sessions import _save_image_original

    monkeypatch.setattr(chat_images, "CHAT_UPLOADS_ROOT", tmp_path / "chat_uploads")
    url = _noisy_png_data_url(300, 300)
    raw = base64.b64decode(url.split(",", 1)[1])
    rel = _save_image_original("t1", 0, url)
    assert rel.startswith("chat-uploads/t1/") and rel.endswith(".png")
    on_disk = chat_images.resolve_ref(rel, tmp_path, "t1")
    assert on_disk is not None and on_disk.is_file()
    assert on_disk.read_bytes() == raw, "落盘的是原始字节，不是 lossy 版"


def test_non_image_data_url_is_left_alone() -> None:
    from game_agent.api.routers.sessions import _shrink_image_data_url

    assert _shrink_image_data_url("data:text/plain;base64,AAA=") == "data:text/plain;base64,AAA="


# ---------- 413 归类 ----------

def test_413_is_payload_too_large_not_generic_error() -> None:
    err = _classify_http_error(413, "<html>413 Request Entity Too Large</html>")
    assert isinstance(err, LlmPayloadTooLarge)
    assert isinstance(_classify_http_error(414, "x"), LlmPayloadTooLarge)


# ---------- view_images：多张拼一张 ----------

def _png(path, size=(600, 900), color=(200, 30, 30)) -> None:
    from PIL import Image
    Image.new("RGBA", size, color + (255,)).save(path)


@pytest.fixture()
def ws(tmp_path):
    for i in range(3):
        _png(tmp_path / f"a{i}.png", color=(30 * i, 90, 200 - 30 * i))
    return tmp_path


def test_view_images_returns_one_contact_sheet(ws) -> None:
    t = ToolContext(run_ctx=None, workspace=ws, role="main", task_id="t")
    res = asyncio.run(view_images({"paths": [f"a{i}.png" for i in range(3)]}, t))
    assert len(res.images) == 1, "三张图必须只回灌一张"
    assert res.images[0].startswith("data:image/jpeg;base64,")
    # 每格对应哪个文件必须写清楚，否则模型不知道自己在看哪张
    for i in range(3):
        assert f"第{i + 1}格=a{i}.png" in res.text
    sheet_kb = (len(res.images[0]) - 23) * 3 / 4 / 1024
    assert sheet_kb < 400, f"联系表不该比单张原图还大（{sheet_kb:.0f}KB）"


def test_view_images_caps_at_nine(ws) -> None:
    t = ToolContext(run_ctx=None, workspace=ws, role="main", task_id="t")
    paths = [f"a{i}.png" for i in range(3)] * 4
    with pytest.raises(Exception) as e:
        asyncio.run(view_images({"paths": paths}, t))
    assert "9" in str(e.value)


def test_view_images_skips_missing_but_keeps_the_rest(ws) -> None:
    t = ToolContext(run_ctx=None, workspace=ws, role="main", task_id="t")
    res = asyncio.run(view_images({"paths": ["a0.png", "nope.png"]}, t))
    assert len(res.images) == 1
    assert "nope.png" in res.text and "不存在" in res.text


# ---------- 音频绝不进上下文 ----------

def test_read_file_refuses_audio_bytes(ws) -> None:
    (ws / "line.wav").write_bytes(b"RIFF" + b"\0" * 5000)
    t = ToolContext(run_ctx=None, workspace=ws, role="main", task_id="t")
    out = asyncio.run(read_file({"path": "line.wav"}, t))
    text = out if isinstance(out, str) else getattr(out, "text", "")
    assert "不会进入模型上下文" in text
    assert "ffprobe" in text
    assert "RIFF" not in text, "音频字节不能出现在返回里"


def test_viewback_survives_model_reply() -> None:
    """最新一张回灌图不能因为「模型回了一轮」就消失：模型看图那一轮的判断只写进了
    reasoning（不进历史），图再没了就只能重新打开重判。只有更新的回灌图才让它过期。"""
    msgs = [_text("看美术"), _shot(SMALL), {"role": "assistant", "content": "配色正确"}]
    out, dropped = expire_viewback(msgs)
    assert dropped == 0 and media_stats(out)["images"] == 1
    # 来了新的一张，旧的那条才过期
    out2, dropped2 = expire_viewback(out + [_shot(SMALL)])
    assert dropped2 == 1 and media_stats(out2)["images"] == 1


def test_nine_noisy_images_produce_one_bounded_sheet(tmp_path) -> None:
    from PIL import Image
    from game_agent.llm.context_window import MAX_SHEET_DATA_URL_CHARS

    paths = []
    for i in range(9):
        path = tmp_path / f"art-{i}.png"
        raw = base64.b64decode(_noisy_png_data_url(600, 900).split(",", 1)[1])
        path.write_bytes(raw)
        paths.append(path.name)
    before = [(tmp_path / p).read_bytes() for p in paths]
    t = ToolContext(run_ctx=None, workspace=tmp_path, role="main", task_id="t")
    res = asyncio.run(view_images({"paths": paths}, t))
    assert len(res.images) == 1
    assert len(res.images[0]) <= MAX_SHEET_DATA_URL_CHARS
    sheet = Image.open(io.BytesIO(base64.b64decode(res.images[0].split(",", 1)[1])))
    assert sheet.width == sheet.height and max(sheet.size) <= 1400
    for i, path in enumerate(paths):
        assert f"第{i + 1}格={path}" in res.text
        assert (tmp_path / path).read_bytes() == before[i]


@pytest.mark.parametrize("paths", [[], "a0.png", [None], [""], {"path": "a0.png"}])
def test_view_images_validates_paths(ws, paths) -> None:
    from game_agent.agents.tools import ToolError

    t = ToolContext(run_ctx=None, workspace=ws, role="main", task_id="t")
    with pytest.raises(ToolError):
        asyncio.run(view_images({"paths": paths}, t))


@pytest.mark.parametrize("stream", [False, True])
def test_contact_sheet_pixels_are_sent_over_http(ws, monkeypatch, stream) -> None:
    import json
    import httpx
    from game_agent.llm.base import Ctx
    from game_agent.llm.openai_compat import OpenAICompatibleLLM

    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        if stream:
            chunk = {"choices": [{"delta": {"content": "已查看合图"}}]}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "已查看合图"}}]})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))

    async def flow():
        t = ToolContext(run_ctx=None, workspace=ws, role="main", task_id="t")
        result = await view_images({"paths": [f"a{i}.png" for i in range(3)]}, t)
        llm = OpenAICompatibleLLM("main", vision=True, api_key="test", base_url="https://llm.invalid/v1")
        msgs = [_text(result.text), _shot(result.images[0])]
        delta = (lambda rd, td: None) if stream else None
        response = await llm.chat(msgs, [], Ctx(), on_delta=delta)
        assert response.text == "已查看合图"
        msgs.append({"role": "assistant", "content": response.text})
        await llm.chat(msgs, [], Ctx(), on_delta=delta)
        return result.images[0]

    image = asyncio.run(flow())
    assert len(sent) == 2
    first = [p for m in sent[0]["messages"] if isinstance(m["content"], list)
             for p in m["content"] if p.get("type") == "image_url"]
    assert len(first) == 1 and first[0]["image_url"]["url"] == image
    # 最新一张回灌图跨一轮仍在（模型下一轮不必重新打开同一批图；换图时才过期）
    assert media_stats(sent[1]["messages"])["images"] == 1


@pytest.mark.parametrize("stream", [False, True])
def test_payload_error_retries_a_smaller_request_once(monkeypatch, stream) -> None:
    import json
    import httpx
    from game_agent.llm.base import Ctx
    from game_agent.llm.openai_compat import OpenAICompatibleLLM

    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(413, text="request too large")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw))
    llm = OpenAICompatibleLLM("main", vision=True, api_key="test", base_url="https://llm.invalid/v1")
    with pytest.raises(LlmPayloadTooLarge):
        asyncio.run(llm.chat([_text("需求"), _shot(SMALL)], [], Ctx(),
                             on_delta=(lambda rd, td: None) if stream else None))
    assert len(sent) == 2
    assert media_stats(sent[1]["messages"])["bytes"] < media_stats(sent[0]["messages"])["bytes"]


def test_saved_original_names_do_not_collide(tmp_path, monkeypatch) -> None:
    from game_agent.core import chat_images
    from game_agent.api.routers.sessions import _save_image_original

    monkeypatch.setattr(chat_images, "CHAT_UPLOADS_ROOT", tmp_path / "chat_uploads")
    url = _noisy_png_data_url(30, 30)
    first = _save_image_original("t1", 0, url)
    second = _save_image_original("t1", 0, url)
    assert first != second
    a = chat_images.resolve_ref(first, tmp_path, "t1")
    b = chat_images.resolve_ref(second, tmp_path, "t1")
    assert a is not None and b is not None
    assert a.read_bytes() == b.read_bytes()
