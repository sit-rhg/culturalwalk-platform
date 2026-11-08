# 打包运行时的静态托管：一个进程同时发 API 和前端
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.main import create_app


@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=app>spa-index</div>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log('cw')", encoding="utf-8")
    return root


@pytest.fixture()
def mounted_app(
    dist: Path, monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> Iterator[FastAPI]:
    # create_app() 时读 settings.web_dist，所以必须先打桩再建 app。
    monkeypatch.setattr(settings, "web_dist", dist)
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield app


@pytest.fixture()
def client(mounted_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(mounted_app) as test_client:
        yield test_client


def test_root_serves_the_built_index(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "spa-index" in response.text


def test_hashed_assets_are_served_from_disk(client: TestClient) -> None:
    response = client.get("/assets/app.js")

    assert response.status_code == 200
    assert response.text == "console.log('cw')"


def test_frontend_routes_fall_back_to_index(client: TestClient) -> None:
    # 浏览器里刷新 /routes/<id> 不能 404：这是前端路由，不是 API。
    response = client.get("/routes/2dd40f41-8805-4cfa-9682-4fd97c7e2154")

    assert response.status_code == 200
    assert "spa-index" in response.text


def test_api_and_health_still_win_over_the_spa_fallback(client: TestClient) -> None:
    health = client.get("/health")
    routes = client.get("/api/v1/routes?limit=1")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert routes.status_code == 200
    assert "items" in routes.json()
    assert "spa-index" not in routes.text


def test_path_traversal_cannot_escape_the_dist_directory(client: TestClient, dist: Path) -> None:
    secret = dist.parent / "secret.txt"
    secret.write_text("nope", encoding="utf-8")

    response = client.get("/../secret.txt")

    # 越界读不到：要么被路由规范化，要么落到 index。两种都不许返回 secret 内容。
    assert "nope" not in response.text


def test_without_web_dist_the_app_has_no_root_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "web_dist", None)
    with TestClient(create_app()) as test_client:
        response = test_client.get("/")

    assert response.status_code == 404
