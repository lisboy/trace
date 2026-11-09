"""run 过程图（截图 / 回灌视觉 / 合图预览）的稳定存放。

背景：``screenshot_page`` 默认把图落在工作区 ``.trace/screenshots/``，``view_images``
的合图落在 ``.trace/previews/``。这两个目录在 run 收尾时会被整目录清空（见
orchestration/tempfiles.SCRATCH_DIRS），用户之后在轨迹里点「截图验收」那条缩略图就
只剩「图片已清理 / 不在工作区」。而这类图恰恰是用户最想回看的「模型到底看见了什么」。

现在把工具产出的图**另存一份**到 ``DATA_DIR/run_shots/<team>/<run>/<file>``（与 db、
日志同级，随安装目录走），与工作区生命周期解耦；工具结果文本里追加一行
``（固定预览：run-shot/<team>/<run>/<file>）`` 标记，前端 ShotThumbs 认这个标记，
改从稳定目录取图。

前端取图**带 Authorization 头 → blob URL**（见 api.ts 的 fetchRunShotUrl）：
``<img>`` 标签发不出 Bearer，只靠会话 cookie 在换 cookie 名/单点登录的部署里会 401
（和聊天附图当初一片空是同一个坑）；先 fetch 拿字节再转 blob: 地址喂给 <img>，
既不依赖 cookie 名，也不用把签名密钥写进会入库的事件文本。

本模块只依赖 config，不 import 路由，避免循环引用。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from game_agent.config import DATA_DIR

RUN_SHOT_PREFIX = "run-shot"
# 单个 run 目录最多保留多少张（超出按 mtime 删最旧）：防止长跑/批量截图把磁盘撑爆
_MAX_PER_RUN = 400


def _root() -> Path:
    return DATA_DIR / "run_shots"


def _safe_seg(v: object) -> str:
    s = str(v or "").strip().replace("\\", "/").strip("/")
    s = s.replace("/", "_")
    return s or "_"


def norm_id(v: object) -> str:
    """UUID 归一化：ORM 给带横线、裸 SQL 给 32 位 hex，两边直接比会全部落空
    （聊天附图迁移第一版一条没搬就是这个坑）。目录命名与团队校验都走这里。"""
    return str(v or "").replace("-", "").strip().lower()


def save_file(team_id: str | None, run_id: str | None, src: Path) -> str:
    """把工具产出图复制到稳定目录，返回标记 ``run-shot/<team>/<run>/<file>``。

    内容寻址命名（原名 + 内容哈希前缀）：同一张图重复保存不会越存越多；
    复制失败返回 ""（调用方降级为原工作区路径，不影响工具本身）。
    """
    try:
        src = Path(src)
        if not src.is_file() or src.stat().st_size == 0:
            return ""
        raw = src.read_bytes()
        h = hashlib.sha1(raw).hexdigest()[:8]
        name = f"{src.stem}-{h}{src.suffix.lower() or '.png'}"
        seg_team = norm_id(team_id) or "_"
        seg_run = norm_id(run_id) or "_"
        d = _root() / seg_team / seg_run
        d.mkdir(parents=True, exist_ok=True)
        dst = d / name
        if not dst.is_file() or dst.stat().st_size != len(raw):
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(dst)
        _prune(d)
        return f"{RUN_SHOT_PREFIX}/{seg_team}/{seg_run}/{name}"
    except OSError:
        return ""


def _prune(d: Path) -> None:
    try:
        files = [p for p in d.iterdir() if p.is_file()]
        if len(files) <= _MAX_PER_RUN:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[: len(files) - _MAX_PER_RUN]:
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def resolve_ref(ref: str) -> Path | None:
    """把标记 ``run-shot/<team>/<run>/<file>`` 解析成磁盘文件；越界/不存在返回 None。"""
    s = str(ref or "").strip().replace("\\", "/")
    if not s.startswith(RUN_SHOT_PREFIX + "/"):
        return None
    rest = s[len(RUN_SHOT_PREFIX) + 1:]
    if rest.count("/") < 2:
        return None
    root = _root().resolve()
    target = (root / rest).resolve()
    if root != target.parent and root not in target.parents:
        return None
    return target if target.is_file() else None


def team_of(ref: str) -> str:
    s = str(ref or "").strip().replace("\\", "/")
    if not s.startswith(RUN_SHOT_PREFIX + "/"):
        return ""
    rest = s[len(RUN_SHOT_PREFIX) + 1:]
    return rest.split("/", 1)[0] if "/" in rest else ""
