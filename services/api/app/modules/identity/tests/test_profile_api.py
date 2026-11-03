# ID-04：用户公开主页的接口测试
"""钉住四件事：匿名可看、只露公开字段、已发布口径与社区一致、不存在/停用都 404。

「已发布」沿用 community 的判定（route_versions.route_id 非空），所以这里直接
造 route_drafts + route_versions 两行，不经过发布流程。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.identity.models import STATUS_DISABLED
from app.modules.routes.models import DraftState, RouteDraft, RouteVersion

# 与 conftest 种的两个账号一致（conftest 里是字符串常量，这里用 UUID 方便落库）
ADMIN_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

API = "/api/v1/users"


def _published_route(
    db: Session,
    *,
    owner_id: uuid.UUID,
    title: str = "岳麓山一日",
    summary: str = "从书院走到山顶",
    route_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> RouteVersion:
    draft = RouteDraft(
        owner_id=owner_id,
        title=title,
        summary=summary,
        theme_tags=["老街"],
        duration_minutes=180,
        state=DraftState.ARCHIVED,
    )
    db.add(draft)
    db.flush()
    version = RouteVersion(
        route_id=route_id,
        draft_id=draft.id,
        version_no=1,
        snapshot={
            "title": title,
            "summary": summary,
            "theme_tags": ["老街", "夜宵"],
            "duration_minutes": 180,
            "nodes": [],
        },
        content_hash="a" * 64,
        created_by=owner_id,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(version)
    db.flush()
    return version


def test_匿名可看主页且只返回公开字段与已发布路线(client: TestClient, db_session: Session) -> None:
    published_id = uuid.uuid4()
    _published_route(db_session, owner_id=USER_ID, route_id=published_id)
    # 草稿还没发布：route_id 为空，不该出现在主页
    _published_route(db_session, owner_id=USER_ID, title="还在编辑", summary="不该出现")
    # 别人的已发布路线：只属于别人的主页
    _published_route(db_session, owner_id=ADMIN_ID, title="别人的路线", route_id=uuid.uuid4())
    db_session.commit()

    resp = client.get(f"{API}/{USER_ID}/profile")  # 故意不带 Authorization
    assert resp.status_code == 200
    body = resp.json()

    user = body["user"]
    assert user["id"] == str(USER_ID)
    assert user["username"] == "xiaoming"
    assert user["role"] == "user"
    assert user["created_at"]
    assert "email" not in user

    assert [item["id"] for item in body["published_routes"]] == [str(published_id)]
    card = body["published_routes"][0]
    assert card["title"] == "岳麓山一日"
    assert card["summary"] == "从书院走到山顶"
    assert card["channel"] == "user"
    assert card["author"] == {"id": str(USER_ID), "username": "xiaoming"}
    assert card["theme_tags"] == ["老街", "夜宵"]
    assert card["duration_minutes"] == 180
    # 可选但本模块不提供的聚合字段，宁可不返回也不瞎填
    assert "comment_count" not in card
    assert "cost" not in card


def test_每条路线只返回最新版本(client: TestClient, db_session: Session) -> None:
    first_id = uuid.uuid4()
    _published_route(
        db_session,
        owner_id=USER_ID,
        title="旧标题",
        route_id=first_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _published_route(
        db_session,
        owner_id=USER_ID,
        title="新标题",
        route_id=first_id,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    db_session.commit()

    body = client.get(f"{API}/{USER_ID}/profile").json()
    assert [item["id"] for item in body["published_routes"]] == [str(first_id)]
    assert body["published_routes"][0]["title"] == "新标题"


def test_不存在的用户返回404(client: TestClient) -> None:
    resp = client.get(f"{API}/{uuid.uuid4()}/profile")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_已停用的用户也按不存在处理(client: TestClient, db_session: Session) -> None:
    from app.modules.identity.models import User

    user = db_session.get(User, USER_ID)
    assert user is not None
    user.status = STATUS_DISABLED
    db_session.commit()

    resp = client.get(f"{API}/{USER_ID}/profile")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
