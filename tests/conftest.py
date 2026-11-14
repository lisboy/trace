"""冒烟测试的引导：在 import game_agent 之前把配置钉死到临时沙箱。

为什么必须在模块顶部（而不是 fixture 里）改环境变量：
``game_agent/db/session.py`` 在 **import 时**就用 ``DATABASE_URL`` 建好了 engine，
``game_agent/config.py`` 也在 import 时算好 DATA_DIR / PROJECTS_ROOT。
所以只要晚一步，测试就会连到开发者的真实库（``~/.trace/game_agent.db``）
并往真实项目目录里写文件。

注意：``config.py`` 里的 ``load_dotenv()`` 是 override=False 语义，
本文件先写入的环境变量优先级更高，仓库根目录的 ``.env`` 不会把它盖掉。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ga-smoke-")).resolve()
_PROJECTS = _TMP / "projects"
_PROJECTS.mkdir(parents=True, exist_ok=True)

# ---- 沙箱：所有路径 / 数据库都进临时目录 ----
os.environ["GA_MODE"] = "lite"
os.environ["GA_DATA_DIR"] = str(_TMP)
os.environ["PROJECTS_ROOT"] = str(_PROJECTS)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP / 'smoke.db').as_posix()}"
os.environ["GA_SECRET_KEY"] = "smoke-test-secret-not-for-production"

# ---- 大脑：测试用离线假大脑（零 key、零网络）----
# 生产已彻底移除 mock 模式（永远走真实大模型）。冒烟测试仍需要一个不发网络请求的
# 假大脑来跑完整一轮，所以这里直接把 MockBrain 注入到所有 build_llm 引用点，
# 不再依赖 GA_LLM_MODE 之类的「运行模式」开关。

# ---- 供应商：全部钉成空串，强制走占位素材链路，保证测试不联网 ----
# 注意必须是「赋空值」而不是 pop：config.py 的 load_dotenv(override=False) 只让步于
# **已存在**的环境变量，pop 掉的键会被仓库根目录 .env 重新填上（COMFYUI_BASE_URL 等），
# MockBrain 冒烟测试就会真的去连本机 ComfyUI / TTS，180s 超时全红（实测踩过）。
for _k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "COMFYUI_BASE_URL", "TTS_BASE_URL",
           "GA_ROLE_MODELS", "REDIS_URL"):
    os.environ[_k] = ""
os.environ["PLACEHOLDER_ENABLED"] = "1"

atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from game_agent.config import DATABASE_URL, PROJECTS_ROOT  # noqa: E402
from game_agent.db.migrate import init_db  # noqa: E402
from game_agent.db.session import SessionLocal, engine  # noqa: E402

# 自检：配置真的落在沙箱里（防止有人把上面的赋值改错后静默连到真实库）
assert _TMP.as_posix() in DATABASE_URL, DATABASE_URL
assert Path(PROJECTS_ROOT) == _PROJECTS, PROJECTS_ROOT


@pytest.fixture(scope="session", autouse=True)
def _fake_brain() -> None:
    """把所有 build_llm 引用点替换成返回 MockBrain（离线假大脑）。

    生产没有 mock 模式了（永远真实大模型）；这些冒烟测试要跑完整一轮编排但不能联网，
    所以在这里统一打桩。MockBrain 只按脚本吐工具调用/汇报，不发起任何 HTTP 请求。
    """
    from unittest.mock import patch

    import game_agent.agents.runtime as rt
    import game_agent.orchestration.orchestrator as orch_mod
    import game_agent.orchestration.project_memory as pm_mod
    import game_agent.orchestration.intent as intent_mod
    from game_agent.llm.mock_brain import MockBrain

    def _make(role, ctx=None):  # noqa: ANN001, ANN202
        if ctx is not None and isinstance(getattr(ctx, "model_profile", None), object) \
                and hasattr(ctx.model_profile, "chat"):
            return ctx.model_profile
        return MockBrain(role)

    patches = [patch.object(rt, "build_llm", _make),
               patch.object(orch_mod, "build_llm", _make),
               patch.object(pm_mod, "build_llm", _make),
               patch.object(intent_mod, "build_llm", _make)]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    """整个测试会话建一次表（create_all 幂等）。"""
    import asyncio

    asyncio.run(init_db())


@pytest.fixture(scope="session")
def session_factory() -> async_sessionmaker[AsyncSession]:
    return SessionLocal


@pytest.fixture(scope="session", autouse=True)
def _dispose_engine():
    yield
    import asyncio

    asyncio.run(engine.dispose())
