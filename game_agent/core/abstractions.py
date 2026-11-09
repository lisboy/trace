"""5 个跨模式抽象接口（设计 §2.1）。

业务代码（agent / tool / orchestration）只依赖这些接口，不出现 ``if mode == "lite"``。
lite 用进程内实现；server 换成 Redis/MySQL/S3 实现即可，业务零改动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from game_agent.db.models import Run, Task, WorkOrder, Artifact, Event, Message  # noqa: F401


@dataclass
class LockInfo:
    project_id: str
    run_id: Optional[str]
    mode: str
    fencing_token: int
    holder_worker: str
    expires_at: Any = None


@dataclass
class EventEnvelope:
    team_id: str
    project_id: Optional[str]
    run_id: Optional[str]
    type: str
    payload: dict[str, Any]
    task_id: Optional[str] = None
    ephemeral: bool = False  # 高频轻量事件（如流式思考 delta）：只走 WS 实时推送，不落库


class Repository(ABC):
    """领域实体持久化。lite / server 共用 DB 实现（见 core.repository.DbRepository）。"""

    # ---- runs ----
    @abstractmethod
    async def create_run(self, **kw) -> Run: ...

    @abstractmethod
    async def get_run(self, run_id: str) -> Optional[Run]: ...

    @abstractmethod
    async def update_run(self, run_id: str, **fields) -> None: ...

    # ---- tasks ----
    @abstractmethod
    async def create_task(self, **kw) -> Task: ...

    @abstractmethod
    async def get_task(self, task_id: str) -> Optional[Task]: ...

    @abstractmethod
    async def update_task(self, task_id: str, **fields) -> None: ...

    @abstractmethod
    async def list_tasks(self, run_id: str) -> list[Task]: ...

    # ---- work orders ----
    @abstractmethod
    async def create_work_order(self, **kw) -> WorkOrder: ...

    @abstractmethod
    async def get_work_order(self, run_id: str) -> Optional[WorkOrder]: ...

    # ---- artifacts ----
    @abstractmethod
    async def create_artifact(self, **kw) -> Artifact: ...

    @abstractmethod
    async def list_artifacts(self, run_id: str) -> list[Artifact]: ...

    @abstractmethod
    async def get_artifact(self, artifact_id: str) -> Optional[Artifact]: ...

    @abstractmethod
    async def update_artifact(self, artifact_id: str, **fields) -> None: ...

    # ---- messages ----
    @abstractmethod
    async def create_message(self, **kw) -> Message: ...

    @abstractmethod
    async def list_messages(self, session_id: str) -> list[Message]: ...

    # ---- events ----
    @abstractmethod
    async def append_event(self, env: EventEnvelope) -> int: ...

    @abstractmethod
    async def get_events_since(self, run_id: str, seq: int) -> list[Event]: ...

    # ---- projects / approvals / comments / audit / usage ----
    @abstractmethod
    async def get_project(self, project_id: str): ...  # noqa: ANN201

    @abstractmethod
    async def create_approval(self, **kw): ...  # noqa: ANN201

    @abstractmethod
    async def get_approval(self, approval_id: str): ...  # noqa: ANN201

    @abstractmethod
    async def update_approval(self, approval_id: str, **fields) -> None: ...

    @abstractmethod
    async def create_comment(self, **kw): ...  # noqa: ANN201

    @abstractmethod
    async def audit(self, **kw) -> None: ...

    @abstractmethod
    async def record_usage(self, **kw) -> None: ...


class EventBus(ABC):
    """事件扇出。权威源是 DB（EventBus 内部落库），实时通道可选（lite=进程内 / server=Redis）。"""

    @abstractmethod
    async def emit(self, env: EventEnvelope) -> int:
        """持久化事件并返回 seq。"""

    @abstractmethod
    def subscribe(self, project_id: str) -> AsyncIterator[EventEnvelope]:
        """订阅某项目的实时事件流。"""

    @abstractmethod
    async def attach_session(self, session) -> None:
        """把当前 DB 会话绑给定时器/补拉使用（lite 无操作）。"""


class Queue(ABC):
    """run 串行队列。lite=进程内；server=PG ``SKIP LOCKED``。"""

    @abstractmethod
    async def enqueue(self, run_id: str, priority: int = 0) -> None: ...

    @abstractmethod
    async def claim(self) -> Optional[str]:
        """取出一个待执行 run 的 id；无则 None。"""

    @abstractmethod
    async def cancel(self, run_id: str) -> None: ...

    @abstractmethod
    async def waiting(self, project_id: str) -> list[str]:
        """当前在某项目前排队的 run id（用于 UI 显示队列位置）。"""


class LockManager(ABC):
    """项目级读写锁 + fencing token（防僵尸 worker 双写）。

    返回值即 fencing token；持锁方每次写操作前校验 ``token == 当前值``。
    """

    @abstractmethod
    async def acquire(self, project_id: str, run_id: str, mode: str = "exclusive",
                     ttl: int = 120) -> int: ...

    @abstractmethod
    async def release(self, project_id: str, run_id: str, token: int) -> None: ...

    @abstractmethod
    async def heartbeat(self, project_id: str, run_id: str, token: int) -> None: ...

    @abstractmethod
    async def current(self, project_id: str) -> Optional[LockInfo]: ...

    @abstractmethod
    def make_fencing(self, project_id: str, token: int) -> "Fencing": ...


class Fencing:
    """运行期 fencing 校验器：锁丢了立即自杀退出，杜绝双写。"""

    def __init__(self, project_id: str, token: int, locks: LockManager) -> None:
        self.project_id = project_id
        self.token = token
        self._locks = locks

    async def assert_valid(self) -> None:
        cur = await self._locks.current(self.project_id)
        if cur is None or cur.fencing_token != self.token:
            raise FencingLost(f"project {self.project_id} 锁已易主，停止写入")


class FencingLost(Exception):
    pass


class ObjectStore(ABC):
    """素材/产物对象存储。lite=本地磁盘；server=S3/MinIO。"""

    @abstractmethod
    async def put_file(self, key: str, local_path: str) -> None: ...

    @abstractmethod
    async def get_url(self, key: str) -> str:
        """返回可访问的 URL 或本地绝对路径。"""

    @abstractmethod
    async def exists(self, key: str) -> bool: ...
