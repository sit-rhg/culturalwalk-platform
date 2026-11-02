"""骨架自检：模块可导入、两个路由前缀已挂到上层。真正业务测试在 test_community_api.py。"""

from __future__ import annotations

from fastapi import FastAPI

from app.modules.community.router import router


def test_router_importable() -> None:
    assert router is not None


def test_prefix_configured() -> None:
    # 顶层 router 只做挂载，前缀落在 /routes 与 /comments 两个子 router 上；
    # 没有前缀端点就会跑到根路径，故按真实挂载结果（openapi 路径）检查。
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    paths = set(app.openapi()["paths"])
    assert "/api/v1/routes" in paths
    assert "/api/v1/routes/{route_id}" in paths
    assert "/api/v1/routes/{route_id}/comments" in paths
    assert "/api/v1/comments/{comment_id}/pin" in paths
