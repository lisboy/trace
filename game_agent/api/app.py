"""FastAPI 应用装配（设计 §10）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from game_agent.api.routers import (artifacts, auth, channels, config, doctor, imagegen, memory,
                                    prefs, projects, providers, questions, runs, sessions,
                                    skills, storage, system, videogen, voicegen)
from game_agent.api.routers.ws import router as ws_router
from game_agent.api.state import state

# 根目录静态文件（favicon 等）：SPA 兜底会吞掉这些路径，必须显式接住
_ROOT_FILES = ("favicon.ico", "favicon.svg", "favicon-32.png", "apple-touch-icon.png",
               "default-avatar.png")


def create_app(frontend_dir: str | None = None) -> FastAPI:
    app = FastAPI(title="循迹（Trace）", version="0.3.4")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"], allow_credentials=False,
    )

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(sessions.router)
    app.include_router(runs.router)
    app.include_router(artifacts.router)
    app.include_router(imagegen.router)
    app.include_router(videogen.router)
    app.include_router(voicegen.router)
    app.include_router(providers.router)
    app.include_router(memory.router)
    app.include_router(config.router)
    app.include_router(skills.router)
    app.include_router(prefs.router)
    app.include_router(questions.router)
    app.include_router(channels.router)
    app.include_router(system.router)
    app.include_router(storage.router)
    app.include_router(doctor.router)
    app.include_router(ws_router)

    @app.get("/health")
    async def health():
        return {"ok": True, "mode": __import__("game_agent.config", fromlist=["MODE"]).MODE}

    @app.get("/metrics")
    async def metrics():
        return HTMLResponse(
            "# HELP gameagent_runs_total 示例指标\n# TYPE gameagent_runs_total counter\n"
            "gameagent_runs_total 0\n", media_type="text/plain")

    # 背景壁纸等上传文件（图片/视频，非敏感；<video>/<img> 标签不带鉴权头）。
    # 不依赖前端构建产物：没建前端时也要能拿到已上传的壁纸。
    uploads_dir = __import__("game_agent.config", fromlist=["DATA_DIR"]).DATA_DIR / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    @app.get("/uploads/{path:path}")
    async def uploads_static(path: str):
        p = (uploads_dir / path).resolve()
        if not str(p).startswith(str(uploads_dir.resolve())) or not p.is_file():
            return HTMLResponse("404", status_code=404)
        return FileResponse(str(p), headers={"Cache-Control": "public, max-age=86400"})

    # ---- 前端静态 + SPA fallback ----
    fe = Path(frontend_dir) if frontend_dir else None
    index = fe / "index.html" if fe else None

    if index and index.exists():
        @app.get("/assets/{path:path}")
        async def static_assets(path: str):
            p = (fe / "assets" / path)
            if not p.exists():
                return HTMLResponse("404", status_code=404)
            # 带 hash 的静态资源可长缓存
            return FileResponse(str(p), headers={"Cache-Control": "public, max-age=31536000, immutable"})

        @app.get("/{full_path:path}")
        async def spa(full_path: str):
            # API / ws / docs 已由上面路由处理；其余回 index.html
            if full_path.startswith(("api/", "ws/", "docs", "openapi", "health", "metrics")):
                return HTMLResponse("not found", status_code=404)
            if full_path in _ROOT_FILES and (fe / full_path).is_file():
                return FileResponse(str(fe / full_path), headers={"Cache-Control": "public, max-age=3600"})
            # HTML 入口禁缓存，保证发版即生效
            return FileResponse(str(index), headers={"Cache-Control": "no-cache"})
    else:
        @app.get("/")
        async def landing():
            return HTMLResponse(
                "<h1>循迹（Trace）</h1><p>后端已启动。API 文档见 <a href='/docs'>/docs</a>。"
                "前端请构建 <code>web/</code> 后由本服务托管，或用 Vite dev server。</p>")

    return app
