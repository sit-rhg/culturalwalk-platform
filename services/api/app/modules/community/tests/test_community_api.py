"""CM-01/02/03 + 公开路线浏览：评论、置顶、收藏、列表筛选与游标分页。

卡面验收：匿名 401、评论越权 403、置顶每人一条、收藏幂等、只列已发布、
未发布/不存在同 404、坏游标 400、写操作落审计。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.catalog.models import Place, PlaceStatus
from app.modules.community.models import Comment, Favorite
from app.modules.routes.models import RouteDraft, RouteVersion

# 与 conftest 种的两个账号一致（conftest 里是字符串常量，这里用 UUID 方便落库）
ADMIN_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

ROUTES_API = "/api/v1/routes"
COMMENTS_API = "/api/v1/comments"


def _key() -> str:
    return str(uuid.uuid4())


# ---------- 造数据 ----------


def _place(db: Session) -> uuid.UUID:
    place = Place(
        id=uuid.uuid4(),
        name="岳麓书院",
        district="岳麓区",
        lat=28.186,
        lon=112.951,
        culture_tags=["书院", "历史"],
        status=PlaceStatus.VERIFIED,
    )
    db.add(place)
    db.flush()
    return place.id


def _snapshot(
    *,
    title: str,
    theme_tags: list[str],
    duration_minutes: int | None,
    place_id: uuid.UUID | None,
    price_min: int | None,
    price_max: int | None,
) -> dict:
    return {
        "title": title,
        "summary": "从东方红广场上山",
        "theme_tags": theme_tags,
        "duration_minutes": duration_minutes,
        "nodes": [
            {
                "position": 1,
                "place_id": str(place_id or uuid.uuid4()),
                "stay_minutes": 60,
                "transport": "walk",
                "note": None,
                "price_min": price_min,
                "price_max": price_max,
            }
        ],
    }


def _published_route(
    db: Session,
    *,
    author_id: uuid.UUID = ADMIN_ID,
    title: str = "岳麓山半日",
    theme_tags: list[str] | None = None,
    duration_minutes: int | None = 240,
    published_at: datetime | None = None,
    place_id: uuid.UUID | None = None,
    price_min: int | None = 1000,
    price_max: int | None = 2000,
) -> uuid.UUID:
    """造一条「已发布」路线：发布态用 route_versions.route_id 非空来代表。"""
    tags = theme_tags or ["历史"]
    draft = RouteDraft(
        owner_id=author_id,
        title=title,
        summary="从东方红广场上山",
        theme_tags=tags,
        duration_minutes=duration_minutes,
    )
    db.add(draft)
    db.flush()
    route_id = uuid.uuid4()
    db.add(
        RouteVersion(
            route_id=route_id,
            draft_id=draft.id,
            version_no=1,
            snapshot=_snapshot(
                title=title,
                theme_tags=tags,
                duration_minutes=duration_minutes,
                place_id=place_id,
                price_min=price_min,
                price_max=price_max,
            ),
            content_hash="a" * 64,
            created_by=author_id,
            created_at=published_at or datetime.now(UTC),
        )
    )
    db.commit()
    return route_id


def _unpublished_draft(db: Session, *, owner_id: uuid.UUID = USER_ID) -> uuid.UUID:
    """草稿 + 冻结版本，但版本没有 route_id：发布前不属于任何 route。"""
    draft = RouteDraft(owner_id=owner_id, title="还没发布的草稿", summary="草稿")
    db.add(draft)
    db.flush()
    db.add(
        RouteVersion(
            route_id=None,
            draft_id=draft.id,
            version_no=1,
            snapshot=_snapshot(
                title="还没发布的草稿",
                theme_tags=["历史"],
                duration_minutes=120,
                place_id=None,
                price_min=None,
                price_max=None,
            ),
            content_hash="b" * 64,
            created_by=owner_id,
        )
    )
    db.commit()
    return draft.id


def _post_comment(
    client: TestClient,
    headers: dict[str, str],
    route_id: uuid.UUID,
    body: str = "周末走过，很棒",
) -> str:
    response = client.post(
        f"{ROUTES_API}/{route_id}/comments",
        json={"body": body},
        headers={**headers, "Idempotency-Key": _key()},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ---------- 公开列表 / 详情 / 版本 ----------


def test_public_list_only_returns_published(client: TestClient, db_session: Session) -> None:
    published = _published_route(db_session, title="已发布路线")
    draft_id = _unpublished_draft(db_session)

    body = client.get(ROUTES_API).json()

    assert [item["id"] for item in body["items"]] == [str(published)]
    assert body["next_cursor"] is None
    summary = body["items"][0]
    assert summary["channel"] == "user"
    assert summary["author"] == {"id": str(ADMIN_ID), "username": "boss"}
    assert summary["comment_count"] == 0
    assert summary["favorite_count"] == 0
    # 草稿不属于任何 route：用它自己的 id 查详情，和随便一个 UUID 一样 404。
    assert client.get(f"{ROUTES_API}/{draft_id}").status_code == 404
    assert client.get(f"{ROUTES_API}/{uuid.uuid4()}").status_code == 404


def test_list_filters(client: TestClient, db_session: Session) -> None:
    place_id = _place(db_session)
    historical = _published_route(
        db_session,
        title="历史路线",
        theme_tags=["历史"],
        duration_minutes=180,
        place_id=place_id,
        published_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    natural = _published_route(
        db_session,
        title="自然路线",
        theme_tags=["自然"],
        duration_minutes=300,
        published_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    def ids(**params: object) -> list[str]:
        return [item["id"] for item in client.get(ROUTES_API, params=params).json()["items"]]

    assert ids(theme="历史") == [str(historical)]
    assert ids(q="自然") == [str(natural)]
    assert ids(duration="0-200") == [str(historical)]
    # 排序：默认按发布时间倒序（扩展参数，契约里没有 sort）
    assert ids() == [str(natural), str(historical)]
    assert ids(sort="oldest") == [str(historical), str(natural)]
    # 没有 city/channel 字段：非长沙城市与 official 渠道都只能给空页
    assert ids(city="beijing") == []
    assert ids(city="长沙") == [str(natural), str(historical)]
    assert ids(channel="official") == []
    assert client.get(ROUTES_API, params={"duration": "bad"}).status_code == 400


def test_route_detail_days_and_etag(client: TestClient, db_session: Session) -> None:
    place_id = _place(db_session)
    route_id = _published_route(db_session, place_id=place_id)

    response = client.get(f"{ROUTES_API}/{route_id}")

    assert response.status_code == 200
    detail = response.json()
    assert detail["version_id"]
    assert detail["content_hash"] == "a" * 64
    assert detail["is_favorited"] is False
    assert detail["days"][0]["day_index"] == 1
    item = detail["days"][0]["items"][0]
    assert item["place"]["name"] == "岳麓书院"
    assert (item["price_min"], item["price_max"]) == (1000, 2000)
    etag = response.headers["ETag"]
    cached = client.get(f"{ROUTES_API}/{route_id}", headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""


def test_versions_require_login_and_published(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    route_id = _published_route(db_session)

    assert client.get(f"{ROUTES_API}/{route_id}/versions").status_code == 401
    listed = client.get(f"{ROUTES_API}/{route_id}/versions", headers=user_headers)
    assert listed.status_code == 200
    assert [item["version_no"] for item in listed.json()["items"]] == [1]
    missing = client.get(f"{ROUTES_API}/{uuid.uuid4()}/versions", headers=user_headers)
    assert missing.status_code == 404


# ---------- 评论 ----------


def test_comment_requires_login_and_validates_body(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    route_id = _published_route(db_session)
    url = f"{ROUTES_API}/{route_id}/comments"

    assert client.post(url, json={"body": "好路线"}).status_code == 401

    blank = client.post(url, json={"body": "   "}, headers=user_headers)
    assert blank.status_code == 422
    assert blank.json()["error"]["details"][0]["field"] == "body"

    too_long = client.post(url, json={"body": "字" * 501}, headers=user_headers)
    assert too_long.status_code == 422

    ok = client.post(
        url,
        json={"body": "  周末走过，很棒  "},
        headers={**user_headers, "Idempotency-Key": _key()},
    )
    assert ok.status_code == 201
    comment = ok.json()
    assert comment["body"] == "周末走过，很棒"
    assert comment["pinned"] is False
    assert comment["author"] == {"id": str(USER_ID), "username": "xiaoming"}

    # 未发布 / 不存在的路线不可评论（同 404，不泄露存在性）
    ghost = client.post(
        f"{ROUTES_API}/{uuid.uuid4()}/comments",
        json={"body": "有人吗"},
        headers=user_headers,
    )
    assert ghost.status_code == 404


def test_pin_only_route_author(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
    user_headers: dict[str, str],
) -> None:
    route_id = _published_route(db_session, author_id=ADMIN_ID)
    first = _post_comment(client, user_headers, route_id)

    denied = client.post(
        f"{COMMENTS_API}/{first}/pin",
        headers={**user_headers, "Idempotency-Key": _key()},
    )
    assert denied.status_code == 403

    pinned = client.post(
        f"{COMMENTS_API}/{first}/pin",
        headers={**admin_headers, "Idempotency-Key": _key()},
    )
    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True

    # 再置顶第二条：第一条自动取消，列表里置顶的仍只有一条且在最前
    second = _post_comment(client, user_headers, route_id, body="第二条评论")
    again = client.post(
        f"{COMMENTS_API}/{second}/pin",
        headers={**admin_headers, "Idempotency-Key": _key()},
    )
    assert again.status_code == 200
    pinned_rows = db_session.query(Comment).filter_by(route_id=route_id, pinned=True).all()
    assert [row.id for row in pinned_rows] == [uuid.UUID(second)]

    listed = client.get(f"{ROUTES_API}/{route_id}/comments").json()["items"]
    assert listed[0]["id"] == second
    assert [item["pinned"] for item in listed] == [True, False]

    missing = client.post(
        f"{COMMENTS_API}/{uuid.uuid4()}/pin",
        headers={**admin_headers, "Idempotency-Key": _key()},
    )
    assert missing.status_code == 404


def test_comment_cursor_pagination(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    route_id = _published_route(db_session)
    created = [
        _post_comment(client, user_headers, route_id, body=f"第 {index} 条") for index in range(3)
    ]

    first = client.get(f"{ROUTES_API}/{route_id}/comments", params={"limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"]

    second = client.get(
        f"{ROUTES_API}/{route_id}/comments",
        params={"limit": 2, "cursor": first["next_cursor"]},
    ).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None

    ids = [item["id"] for item in first["items"] + second["items"]]
    assert ids == list(reversed(created))  # 时间倒序，无重复

    bad = client.get(f"{ROUTES_API}/{route_id}/comments", params={"cursor": "not-a-cursor"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_CURSOR"


# ---------- 收藏 ----------


def test_favorite_is_idempotent(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    route_id = _published_route(db_session)
    url = f"{ROUTES_API}/{route_id}/favorite"

    assert client.post(url).status_code == 401

    assert client.post(url, headers={**user_headers, "Idempotency-Key": _key()}).status_code == 204
    assert client.post(url, headers={**user_headers, "Idempotency-Key": _key()}).status_code == 204
    assert db_session.query(Favorite).filter_by(route_id=route_id, user_id=USER_ID).count() == 1

    detail = client.get(f"{ROUTES_API}/{route_id}", headers=user_headers).json()
    assert detail["is_favorited"] is True
    assert detail["favorite_count"] == 1

    assert client.delete(url, headers=user_headers).status_code == 204
    assert client.delete(url, headers=user_headers).status_code == 204
    assert db_session.query(Favorite).filter_by(route_id=route_id).count() == 0

    # 不存在的路线：404（收藏与取消收藏都不允许对幽灵路线操作）
    ghost = uuid.uuid4()
    assert client.post(f"{ROUTES_API}/{ghost}/favorite", headers=user_headers).status_code == 404
    assert client.delete(f"{ROUTES_API}/{ghost}/favorite", headers=user_headers).status_code == 404


# ---------- 审计 ----------


def test_writes_are_audited(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
    user_headers: dict[str, str],
) -> None:
    route_id = _published_route(db_session, author_id=ADMIN_ID)
    comment_id = _post_comment(client, user_headers, route_id)
    client.post(
        f"{COMMENTS_API}/{comment_id}/pin",
        headers={**admin_headers, "Idempotency-Key": _key()},
    )
    favorite_url = f"{ROUTES_API}/{route_id}/favorite"
    client.post(favorite_url, headers={**user_headers, "Idempotency-Key": _key()})
    client.delete(favorite_url, headers=user_headers)

    rows = db_session.query(AuditLog).all()
    assert len(rows) == 4
    assert {row.action for row in rows} == {
        "comment.create",
        "comment.pin",
        "favorite.create",
        "favorite.delete",
    }
    assert {row.object_type for row in rows} == {"comment", "favorite"}
