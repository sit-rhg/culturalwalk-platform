# D9：地点收录状态的流转端点
"""以前草稿点位一旦建出来就永远出不去：没有端点能把 status 改成 verified，
公开列表过滤 `status == verified`，于是新地点只能靠进库手改上线。

这组用例钉住四件事：
1. 编辑把草稿发布成 verified —— 公开详情与列表立刻读得到；
2. 撤回成 draft —— 公开读回到 404（与 #95 的可见性口径一致）；
3. 重复发布是幂等（不报错、不重复写审计）；
4. 权限与存在性：普通用户 403、不存在 404。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.core.deps import create_access_token
from app.main import create_app
from app.modules.audit.models import AuditLog

API = "/api/v1/places"


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _client(session_factory: sessionmaker[Session]) -> TestClient:
    def override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _headers(role: str = "editor", *, key: str | None = None) -> dict[str, str]:
    token = create_access_token(user_id=str(uuid.uuid4()), username="editor", role=role)
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _create_place(client: TestClient) -> dict[str, object]:
    response = client.post(
        API,
        json={
            "name": f"草稿点位-{uuid.uuid4().hex[:6]}",
            "district": "岳麓区",
            "lat": 28.18,
            "lon": 112.94,
        },
        headers=_headers(key=str(uuid.uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_publish_makes_draft_visible(
    session_factory: sessionmaker[Session],
) -> None:
    """发布后公开详情与列表都能读到，status 变成 verified。"""
    with _client(session_factory) as client:
        place = _create_place(client)
        place_id = place["id"]
        assert place["status"] == "draft"
        # 草稿阶段公开读是 404（#95 的口径）
        assert client.get(f"{API}/{place_id}").status_code == 404

        published = client.patch(
            f"{API}/{place_id}", json={"status": "verified"}, headers=_headers()
        )
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "verified"

        detail = client.get(f"{API}/{place_id}")
        assert detail.status_code == 200
        assert detail.json()["id"] == place_id

        listed = client.get(API, params={"q": place["name"]})
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [place_id]


def test_unpublish_hides_place_again(session_factory: sessionmaker[Session]) -> None:
    """撤回成 draft 之后，公开读回到 404，列表里也消失。"""
    with _client(session_factory) as client:
        place = _create_place(client)
        place_id = place["id"]
        client.patch(f"{API}/{place_id}", json={"status": "verified"}, headers=_headers())

        withdrawn = client.patch(f"{API}/{place_id}", json={"status": "draft"}, headers=_headers())
        assert withdrawn.status_code == 200
        assert withdrawn.json()["status"] == "draft"

        assert client.get(f"{API}/{place_id}").status_code == 404
        listed = client.get(API, params={"q": place["name"]})
        assert listed.json()["items"] == []


def test_repeating_same_status_is_idempotent(
    session_factory: sessionmaker[Session],
) -> None:
    """连点两次「发布」：第二次照样 200，但不会多写一条审计。"""
    with _client(session_factory) as client:
        place = _create_place(client)
        place_id = place["id"]
        first = client.patch(f"{API}/{place_id}", json={"status": "verified"}, headers=_headers())
        second = client.patch(f"{API}/{place_id}", json={"status": "verified"}, headers=_headers())
        assert (first.status_code, second.status_code) == (200, 200)

    with session_factory() as db:
        rows = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "place.status",
                AuditLog.object_id == place_id,
            )
        ).all()
    assert len(rows) == 1
    # 审计里记了"从哪到哪"，事后能追是谁把点位放上线的
    assert rows[0].actor_username == "editor"


def test_status_requires_editor_role(session_factory: sessionmaker[Session]) -> None:
    """普通用户点发布：403，状态不动。"""
    with _client(session_factory) as client:
        place = _create_place(client)
        forbidden = client.patch(
            f"{API}/{place['id']}", json={"status": "verified"}, headers=_headers(role="user")
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "FORBIDDEN"
        assert client.get(f"{API}/{place['id']}").status_code == 404


def test_unknown_place_returns_404(session_factory: sessionmaker[Session]) -> None:
    with _client(session_factory) as client:
        response = client.patch(
            f"{API}/{uuid.uuid4()}", json={"status": "verified"}, headers=_headers()
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLACE_NOT_FOUND"


def test_invalid_status_value_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    """status 只认 draft / verified：乱传一个值必须是 422 而不是静默改成草稿。"""
    with _client(session_factory) as client:
        place = _create_place(client)
        response = client.patch(
            f"{API}/{place['id']}", json={"status": "published"}, headers=_headers()
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
