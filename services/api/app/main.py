# CulturalWalk API · 应用入口
"""只做三件事：注册中间件、注册路由、注册异常处理。业务逻辑一行都不许写在这里。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.deps import register_session_guard
from app.core.errors import (
    AppError,
    app_error_handler,
    request_validation_handler,
    unhandled_handler,
)

# 模块路由。骨架阶段全部为空 router（只有 tag 和 TODO 注释），
# 谁认领哪张卡，谁就往自己模块的 router.py 里填端点。
from app.modules.audit.router import router as audit_router
from app.modules.catalog.router import router as catalog_router
from app.modules.community.router import router as community_router
from app.modules.discovery.router import router as discovery_router
from app.modules.identity.router import router as identity_router
from app.modules.identity.service import session_is_revoked
from app.modules.media.router import router as media_router
from app.modules.moderation.router import router as moderation_router
from app.modules.notifications.router import router as notifications_router
from app.modules.planner.router import router as planner_router
from app.modules.routes.router import router as routes_router

MODULE_ROUTERS = [
    identity_router,
    catalog_router,
    routes_router,
    discovery_router,
    moderation_router,
    community_router,
    planner_router,
    media_router,
    notifications_router,
    audit_router,
]


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 登出/轮换后 access 令牌立刻失效，检查器由 identity 提供（core 不反向依赖模块）。
    register_session_guard(session_is_revoked)

    @app.middleware("http")
    async def commit_session(request: Request, call_next):  # type: ignore[no-untyped-def]
        """在响应发出**之前**提交事务。

        yield 依赖的 teardown 在响应之后才跑，commit 失败时客户端已经拿到 200，
        数据却没写进去，只剩日志里一句异常。这里提前提交：失败就抛，客户端看到 5xx。
        4xx/5xx 不提交，交给 get_db 的 finally 回滚，避免把失败的半截事务写进去。
        """
        response = await call_next(request)
        db = getattr(request.state, "db", None)
        if db is not None and response.status_code < 400 and db.in_transaction():
            db.commit()
        return response

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = request.headers.get(
            "X-Request-Id", f"req_{uuid.uuid4().hex[:16]}"
        )
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    # 请求体/查询参数校验失败也走统一信封，契约要求所有非 2xx 都是 {"error": {...}}。
    app.add_exception_handler(RequestValidationError, request_validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_handler)

    api = APIRouter(prefix=settings.api_prefix)

    @api.get("/health", tags=["system"], summary="健康检查")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    for module_router in MODULE_ROUTERS:
        api.include_router(module_router)

    # 根路径再挂一个健康检查：监控/容器探针默认打 /health，不带 /api/v1 前缀。
    # 返回体和上面完全一致，避免运维侧为了探针去改前缀。
    @app.get("/health", tags=["system"], summary="健康检查（根路径别名）", include_in_schema=False)
    def health_root() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    app.include_router(api)
    _mount_web(app)
    return app


def _mount_web(app: FastAPI) -> None:
    """有构建产物就由后端托管前端。

    打包版只起一个进程：模块加载时的 vite dev server 不能被带走，前端 dist 直接
    由 FastAPI 发。API 路由先注册，路径匹配优先，只有没命中的 GET 才落到 SPA 兜底，
    这样 /routes/<id> 这类前端路由刷新不会 404。没配 WEB_DIST 或目录不存在时
    什么都不做，本地开发仍然照旧走 vite。
    """
    dist = settings.web_dist
    if dist is None:
        return
    index = dist / "index.html"
    if not index.is_file():
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    resolved_root = dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        candidate = (dist / full_path).resolve()
        # resolve() 之后再比对父目录，挡住 ../ 穿越读到包外文件。
        if candidate.is_file() and resolved_root in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
