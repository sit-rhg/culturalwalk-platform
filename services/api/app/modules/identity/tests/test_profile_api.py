# ID-04：用户公开主页的接口测试
"""钉住四件事：匿名可看、只露公开字段、已发布口径、不存在/停用都 404。

「已发布」以 publications.state = PUBLISHED 为准（发布接口会写这一行）。
不能只看 route_versions.route_id 非空：下线只改 publications.state，route_id 会
一直留在版本行上，用它当判据会让已下线的路线继续挂在公开主页。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.identity.models import STATUS_DISABLED
from app.modules.moderation.models import Publication, PublicationState
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
    publication_state: PublicationState | None = PublicationState.PUBLISHED,
    channel: str = "user",
) -> RouteVersion:
    """造一条冻结过的路线；给了 route_id 就同时写一条发布记录。

    publication_state=None 表示「有版本、没发布」，传 UNPUBLISHED 表示发过又下线——
    后两种都不该出现在公开主页上。
    """
    draft = RouteDraft(
        owner_id=owner_id,
        title=title,
        summary=summary,
        theme_tags=["老街"],
        duration_minutes=180,
        channel=channel,
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
    if route_id is not None and publication_state is not None:
        db.add(
            Publication(
                submission_id=uuid.uuid4(),
                route_id=route_id,
                version_id=version.id,
                content_hash=version.content_hash,
                state=publication_state,
                published_by=owner_id,
                published_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
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
    """同一 route 下线再发新版本：主页只展示当前发布的那一版。

    一条路线同时只能有一条 PUBLISHED（0014 的部分唯一索引），所以旧版本这条必须
    是 UNPUBLISHED —— 真实流程里它也确实是被下线之后才发的新版。
    """
    first_id = uuid.uuid4()
    _published_route(
        db_session,
        owner_id=USER_ID,
        title="旧标题",
        route_id=first_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        publication_state=PublicationState.UNPUBLISHED,
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


def test_已下线的路线不出现在主页(client: TestClient, db_session: Session) -> None:
    """下线只改 publications.state，route_id 还留在版本上——不能凭它判断可见性。"""
    gone_id = uuid.uuid4()
    _published_route(
        db_session,
        owner_id=USER_ID,
        title="已经下线",
        route_id=gone_id,
        publication_state=PublicationState.UNPUBLISHED,
    )
    alive_id = uuid.uuid4()
    _published_route(db_session, owner_id=USER_ID, title="还在线", route_id=alive_id)
    db_session.commit()

    body = client.get(f"{API}/{USER_ID}/profile").json()

    assert [item["id"] for item in body["published_routes"]] == [str(alive_id)]


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


def test_官方路线在主页显示官方来源(client: TestClient, db_session: Session) -> None:
    """编辑产出的精选是 channel=official；主页不能把它说成用户投稿。"""
    official_id = uuid.uuid4()
    _published_route(db_session, owner_id=ADMIN_ID, route_id=official_id, channel="official")
    db_session.commit()

    body = client.get(f"{API}/{ADMIN_ID}/profile").json()

    assert [item["channel"] for item in body["published_routes"]] == ["official"]


def test_非法用户id返回404而不是422(client: TestClient) -> None:
    """契约只声明 200 / 404；路径参数不是 UUID 时也不该落到 422。"""
    resp = client.get(f"{API}/not-a-uuid/profile")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
