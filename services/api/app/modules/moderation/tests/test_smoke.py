"""骨架自检：模块可导入、8 个契约端点都挂在应用上。

契约端点分属 /route-drafts、/review、/submissions、/publications 四个前缀，
所以把原来的「router 必须有单一 prefix」断言改成从 OpenAPI 里逐条检查——
聚合 router 的 prefix 为空是设计结果，不是漏配。

FastAPI 0.162 把 include_router 实现成惰性的 _IncludedRouter，直接读
router.routes 拿不到子路由；生成一次 OpenAPI 文档才能看到展开后的路径。
"""

from __future__ import annotations

from app.main import app
from app.modules.moderation.router import router

EXPECTED_ROUTES: set[tuple[str, str]] = {
    ("post", "/api/v1/route-drafts/{draft_id}/submit"),
    ("post", "/api/v1/route-drafts/{draft_id}/withdraw"),
    ("get", "/api/v1/review/tasks"),
    ("post", "/api/v1/review/tasks/{task_id}/claim"),
    ("post", "/api/v1/review/tasks/{task_id}/decision"),
    ("post", "/api/v1/review/tasks/{task_id}/release"),
    ("post", "/api/v1/submissions/{submission_id}/publish"),
    ("post", "/api/v1/publications/{publication_id}/unpublish"),
}


def _registered_routes() -> set[tuple[str, str]]:
    paths = app.openapi()["paths"]
    return {
        (method, path)
        for path, operations in paths.items()
        for method in operations
        if method not in {"head", "options"}
    }


def test_router_importable() -> None:
    assert router is not None


def test_contract_paths_registered() -> None:
    assert _registered_routes() >= EXPECTED_ROUTES
