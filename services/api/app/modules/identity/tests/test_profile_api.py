"""ID-04 验收：GET /users/{user_id}/profile（公开的个人主页）。

契约：docs/api/openapi.yaml `/users/{user_id}/profile`（security: []，只有 200/404）
与 components/schemas 的 UserProfile / RouteSummary；数据：docs/02 §2.1 的 users。

用例里的账号自己种，不依赖 conftest 那两条固定用户：这里的重点之一是 role/bio 这类
主页字段与"账号状态"，固定夹具改一次就会连着别处一起碎。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.identity.models import (
    ROLE_USER,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    User,
)
from app.modules.identity.service import IdentityService, hash_password
from app.modules.moderation.models import Publication, PublicationState
from app.modules.routes.models import DraftState, RouteDraft, RouteVersion

PROFILE_URL = "/api/v1/users/{user_id}/profile"
UNKNOWN_ID = "99999999-9999-9999-9999-999999999999"
# argon2 只算一次，别让每个用例都等 50ms
_PASSWORD_HASH = hash_password("password123")


def _add_user(
    db: Session,
    *,
    username: str,
    status: str = STATUS_ACTIVE,
    bio: str | None = None,
) -> User:
    user = User(
        username=username,
        bio=bio,
        password_hash=_PASSWORD_HASH,
        role=ROLE_USER,
        status=status,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _add_route(
    db: Session,
    author: User,
    *,
    title: str,
    state: PublicationState = PublicationState.PUBLISHED,
    published_at: datetime | None = None,
    nodes: list[dict[str, object]] | None = None,
) -> uuid.UUID:
    """种一条「冻结过、发布过」的路线，返回它的 route_id。

    直接建三张表而不是调 moderation 的发布服务：本用例验的是主页的读路径，
    不该顺带把发布流程也验一遍（那条链有自己的测试）。state 传 UNPUBLISHED
    就模拟"发过又下线"，用来证明下线路线不会回到主页。
    """
    route_id = uuid.uuid4()
    snapshot: dict[str, object] = {
        "title": title,
        "summary": f"{title} 的一句话介绍",
        "theme_tags": ["历史", "建筑"],
        "duration_minutes": 180,
        "nodes": nodes
        if nodes is not None
        else [
            {
                "place_id": str(uuid.uuid4()),
                "stay_minutes": 60,
                "price_min": 3000,
                "price_max": 5000,
            }
        ],
    }
    draft = RouteDraft(
        owner_id=author.id,
        title=title,
        state=DraftState.APPROVED_LOCKED,
    )
    db.add(draft)
    db.flush()
    version = RouteVersion(
        draft_id=draft.id,
        route_id=route_id,
        version_no=1,
        snapshot=snapshot,
        content_hash="0" * 64,
        created_by=author.id,
    )
    db.add(version)
    db.flush()
    db.add(
        Publication(
            submission_id=uuid.uuid4(),
            route_id=route_id,
            version_id=version.id,
            content_hash=version.content_hash,
            state=state,
            published_by=author.id,
            published_at=published_at or datetime.now(UTC),
        )
    )
    db.commit()
    return route_id


def _get(client: TestClient, user_id: object, headers: dict[str, str] | None = None):
    return client.get(PROFILE_URL.format(user_id=user_id), headers=headers)


@pytest.fixture()
def author(db_session: Session) -> User:
    return _add_user(db_session, username="wanderer", bio="沿着运河走")


# ===== 200 的形状 =====


def test_profile_without_routes_returns_empty_list(client: TestClient, author: User) -> None:
    resp = _get(client, author.id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"user", "published_routes"}
    assert body["published_routes"] == []
    assert body["user"]["id"] == str(author.id)
    assert body["user"]["username"] == "wanderer"
    assert body["user"]["role"] == "user"
    assert body["user"]["bio"] == "沿着运河走"


def test_profile_returns_published_route_summary(
    client: TestClient, db_session: Session, author: User
) -> None:
    route_id = _add_route(db_session, author, title="岳麓山到橘子洲")

    body = _get(client, author.id).json()
    routes = body["published_routes"]

    assert len(routes) == 1
    card = routes[0]
    assert card["id"] == str(route_id)
    assert card["title"] == "岳麓山到橘子洲"
    assert card["channel"] == "user"
    assert card["summary"] == "岳麓山到橘子洲 的一句话介绍"
    assert card["theme_tags"] == ["历史", "建筑"]
    assert card["duration_minutes"] == 180
    assert card["author"] == {"id": str(author.id), "username": "wanderer"}
    assert card["cost"] == {
        "min_total": 3000,
        "max_total": 5000,
        "currency": "CNY",
        "unknown_count": 0,
    }


def test_profile_hides_unknown_counts(
    client: TestClient, db_session: Session, author: User
) -> None:
    """评论数/收藏数还没真值时整个不出现：契约里它们是 integer，不是 nullable。"""
    _add_route(db_session, author, title="沿江走")

    card = _get(client, author.id).json()["published_routes"][0]

    assert "comment_count" not in card
    assert "favorite_count" not in card


def test_profile_cost_counts_nodes_missing_price(
    client: TestClient, db_session: Session, author: User
) -> None:
    _add_route(
        db_session,
        author,
        title="半价也能走",
        nodes=[
            {
                "place_id": str(uuid.uuid4()),
                "stay_minutes": 60,
                "price_min": 1000,
                "price_max": 2000,
            },
            {"place_id": str(uuid.uuid4()), "stay_minutes": 30},
        ],
    )

    cost = _get(client, author.id).json()["published_routes"][0]["cost"]

    assert cost["min_total"] == 1000
    assert cost["max_total"] == 2000
    assert cost["unknown_count"] == 1


def test_profile_renders_snapshot_not_draft(
    client: TestClient, db_session: Session, author: User
) -> None:
    """发布后作者改了草稿标题，主页仍显示冻结时的版本，不和线上内容打架。"""
    _add_route(db_session, author, title="冻结时的标题")
    draft = db_session.query(RouteDraft).one()
    draft.title = "草稿被改过的新标题"
    db_session.commit()

    title = _get(client, author.id).json()["published_routes"][0]["title"]

    assert title == "冻结时的标题"


# ===== 只出现在主页该出现的路线 =====


def test_profile_skips_draft_only_route(
    client: TestClient, db_session: Session, author: User
) -> None:
    """只有草稿、还没发布：不能出现在公开主页上。"""
    draft = RouteDraft(owner_id=author.id, title="还没发", state=DraftState.EDITING)
    db_session.add(draft)
    db_session.commit()

    assert _get(client, author.id).json()["published_routes"] == []


def test_profile_skips_unpublished_route(
    client: TestClient, db_session: Session, author: User
) -> None:
    """发过又下线：state 不是 PUBLISHED 就不出现（route_id 仍留在版本上）。"""
    _add_route(db_session, author, title="已经下线", state=PublicationState.UNPUBLISHED)

    assert _get(client, author.id).json()["published_routes"] == []


def test_profile_orders_by_published_at_desc(
    client: TestClient, db_session: Session, author: User
) -> None:
    now = datetime.now(UTC)
    _add_route(db_session, author, title="先发的", published_at=now - timedelta(days=2))
    _add_route(db_session, author, title="后发的", published_at=now)

    titles = [card["title"] for card in _get(client, author.id).json()["published_routes"]]

    assert titles == ["后发的", "先发的"]


def test_profile_does_not_leak_other_authors_routes(
    client: TestClient, db_session: Session, author: User
) -> None:
    other = _add_user(db_session, username="someone-else")
    _add_route(db_session, other, title="别人的路线")
    mine = _add_route(db_session, author, title="我的路线")

    ids = [card["id"] for card in _get(client, author.id).json()["published_routes"]]

    assert ids == [str(mine)]


# ===== 公开端点：不需要令牌 =====


def test_profile_is_anonymous(client: TestClient, author: User) -> None:
    resp = _get(client, author.id)
    assert resp.status_code == 200
    assert "authorization" not in {key.lower() for key in resp.request.headers}


def test_profile_ignores_garbage_token(client: TestClient, author: User) -> None:
    """security: [] 意味着"有没有令牌都不影响"，坏令牌不该把它变成 401。"""
    resp = _get(client, author.id, headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 200


# ===== 404：不存在与已停用对外一致 =====


def test_profile_unknown_user_is_404(client: TestClient) -> None:
    resp = _get(client, UNKNOWN_ID)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_profile_disabled_user_is_404(client: TestClient, db_session: Session) -> None:
    """停用账号的公开主页按"不存在"处理，不泄露这个 id 存在过。"""
    disabled = _add_user(db_session, username="gone", status=STATUS_DISABLED)

    resp = _get(client, disabled.id)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_disabled_and_unknown_look_identical(client: TestClient, db_session: Session) -> None:
    disabled = _add_user(db_session, username="gone2", status=STATUS_DISABLED)

    disabled_body = _get(client, disabled.id).json()["error"]
    unknown_body = _get(client, UNKNOWN_ID).json()["error"]

    assert disabled_body["code"] == unknown_body["code"]
    assert disabled_body["message"] == unknown_body["message"]


# ===== 422：非法 uuid =====


def test_profile_invalid_uuid_is_422(client: TestClient) -> None:
    resp = _get(client, "not-a-uuid")

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


# ===== service 层：数据来自版本而不是草稿 =====


def test_published_summaries_come_from_versions(db_session: Session, author: User) -> None:
    """repository 返回的是 ORM 行、service 才组 DTO：契约要求的分层。"""
    _add_route(db_session, author, title="分层检查")

    from app.modules.identity.repository import IdentityRepository

    rows = IdentityRepository(db_session).list_published_versions(author.id)
    assert len(rows) == 1
    route_id, version, _published_at = rows[0]
    assert isinstance(version, RouteVersion)
    assert version.version_no == 1

    summaries = IdentityService(db_session).published_route_summaries(author)
    assert [card.id for card in summaries] == [route_id]
