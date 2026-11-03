"""M0-03 验收：GET /health 返回 {"status": "ok"}。

根路径与 /api/v1/health 两个地址都要能探活：前者给容器探针/负载均衡用，
后者是契约里的正式地址（docs/api/openapi.yaml: /health）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def test_root_health_returns_ok() -> None:
    response = _client().get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_prefixed_health_matches_contract() -> None:
    response = _client().get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_root_health_is_not_in_openapi_schema() -> None:
    """别名不写进契约：契约里只保留 /api/v1/health 一个正式地址。"""
    paths = _client().get("/openapi.json").json()["paths"]

    assert "/api/v1/health" in paths
    assert "/health" not in paths
