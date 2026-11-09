"""lite ObjectStore：本地磁盘存储（server 模式换成 S3/MinIO）。

素材本体由美术工具直接写入项目工作区 ``assets/``，这里用于其它对象（如导出包）。
"""
from __future__ import annotations

from pathlib import Path

from game_agent.config import DATA_DIR
from game_agent.core.abstractions import ObjectStore


class LiteObjectStore(ObjectStore):
    def __init__(self, base: Path | None = None) -> None:
        self._base = base or (DATA_DIR / "objects")
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # 防目录穿越
        p = (self._base / key).resolve()
        if not str(p).startswith(str(self._base.resolve())):
            raise ValueError("非法 key")
        return p

    async def put_file(self, key: str, local_path: str) -> None:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(Path(local_path).read_bytes())

    async def get_url(self, key: str) -> str:
        return str(self._path(key))

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()
