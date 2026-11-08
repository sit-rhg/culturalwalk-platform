"""CM-01/02/03 + 公开路线浏览：评论、置顶、收藏、列表筛选与游标分页。

卡面验收：匿名 401、评论越权 403、置顶每人一条、收藏幂等、只列已发布、
未发布/不存在同 404、坏游标 400、写操作落审计。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.catalog.models import Place, PlaceStatus
from app.modules.community.models import Comment, Favorite
from app.modules.community.repository import CommunityRepository
from app.modules.moderation.models import Publication, PublicationState
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
    audience: list[str] | None = None,
) -> dict:
    return {
        "title": title,
        "summary": "从东方红广场上山",
        "theme_tags": theme_tags,
        "audience": audience or [],
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
    audience: list[str] | None = None,
) -> uuid.UUID:
    """造一条「已发布」路线：版本 + 一条 PUBLISHED 发布记录（真实发布流程的形状）。"""
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
    version = RouteVersion(
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
            audience=audience,
        ),
        content_hash="a" * 64,
        created_by=author_id,
        created_at=published_at or datetime.now(UTC),
    )
    db.add(version)
    db.flush()
    db.add(
        Publication(
            submission_id=uuid.uuid4(),
            route_id=route_id,
            version_id=version.id,
            content_hash=version.content_hash,
            state=PublicationState.PUBLISHED,
            published_by=author_id,
            published_at=version.created_at,
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


def test_unpublished_route_is_not_public(client: TestClient, db_session: Session) -> None:
    """下线只改 publications.state；版本行的 route_id 仍留着，不能据此继续公开。"""
    alive = _published_route(db_session, title="还在线")
    gone = _published_route(db_session, title="已经下线")
    publication = db_session.execute(
        select(Publication).where(Publication.route_id == gone)
    ).scalar_one()
    publication.state = PublicationState.UNPUBLISHED
    db_session.commit()

    body = client.get(ROUTES_API).json()

    assert [item["id"] for item in body["items"]] == [str(alive)]
    assert client.get(f"{ROUTES_API}/{gone}").status_code == 404


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
    # 排序：默认按收藏数倒序（同数看发布时间），扩展参数 latest/oldest 按发布时间
    assert ids() == [str(natural), str(historical)]
    assert ids(sort="latest") == [str(natural), str(historical)]
    assert ids(sort="oldest") == [str(historical), str(natural)]
    # 没有 city/channel 字段：非长沙城市与 official 渠道都只能给空页
    assert ids(city="beijing") == []
    assert ids(city="长沙") == [str(natural), str(historical)]
    assert ids(channel="official") == []
    assert client.get(ROUTES_API, params={"duration": "bad"}).status_code == 400


def test_list_default_sorts_by_favorites(client: TestClient, db_session: Session) -> None:
    """默认按收藏数倒序；游标必须带着收藏数才能翻页，换排序模式要报坏游标。"""
    popular = _published_route(
        db_session,
        title="被收藏的路线",
        published_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    fresh = _published_route(
        db_session,
        title="新发布但没人收藏",
        published_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    # 唯一约束是 (user_id, route_id)，两个不同用户各收藏一次。
    db_session.add(Favorite(user_id=ADMIN_ID, route_id=popular))
    db_session.add(Favorite(user_id=USER_ID, route_id=popular))
    db_session.commit()

    first = client.get(ROUTES_API, params={"limit": 1}).json()
    assert [item["id"] for item in first["items"]] == [str(popular)]
    assert first["items"][0]["favorite_count"] == 2
    assert first["next_cursor"] is not None

    second = client.get(ROUTES_API, params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [item["id"] for item in second["items"]] == [str(fresh)]
    assert second["next_cursor"] is None

    # 显式 latest 仍按发布时间倒序；热门把发布时间更早的路线排到前面。
    latest = client.get(ROUTES_API, params={"sort": "latest"}).json()
    assert [item["id"] for item in latest["items"]] == [str(fresh), str(popular)]

    # 排序模式与游标不一致时直接 400，不悄悄给出错位结果。
    mismatched = client.get(ROUTES_API, params={"sort": "latest", "cursor": first["next_cursor"]})
    assert mismatched.status_code == 400
    assert mismatched.json()["error"]["code"] == "INVALID_CURSOR"


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


def test_concurrent_pin_is_conflict_not_500(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发置顶：另一路已经提交了置顶，这边 unpin 看不到它，只能撞唯一索引。

    真实场景是 READ COMMITTED 快照差；这里把 unpin 换成空操作来稳定复现，
    断言的是「撞索引要翻成可重试的 409，而不是 500」。
    """
    route_id = _published_route(db_session, author_id=ADMIN_ID)
    first = _post_comment(client, user_headers, route_id)
    second = _post_comment(client, user_headers, route_id, body="第二条评论")
    assert (
        client.post(
            f"{COMMENTS_API}/{first}/pin",
            headers={**admin_headers, "Idempotency-Key": _key()},
        ).status_code
        == 200
    )

    monkeypatch.setattr(
        CommunityRepository,
        "unpin_others",
        lambda self, *, route_id, keep_id: None,
    )
    conflict = client.post(
        f"{COMMENTS_API}/{second}/pin",
        headers={**admin_headers, "Idempotency-Key": _key()},
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "COMMENT_PIN_CONFLICT"


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


def test_route_detail_published_at_comes_from_publication(
    client: TestClient, db_session: Session
) -> None:
    """对外发布时间取发布记录，不取版本冻结时间。

    版本在提交审核时就冻结了，发布可能排在几天之后；拿 created_at 顶替会让详情页
    显示的发布时间早于审核通过时间，和列表里的排序口径也对不上。
    """
    route_id = _published_route(db_session)
    publication = db_session.scalars(
        select(Publication).where(Publication.route_id == route_id)
    ).one()
    published_at = publication.published_at + timedelta(days=3)
    publication.published_at = published_at
    db_session.commit()

    response = client.get(f"{ROUTES_API}/{route_id}")

    assert response.status_code == 200, response.text
    assert datetime.fromisoformat(response.json()["published_at"]) == published_at


def test_list_filters_by_audience(client: TestClient, db_session: Session) -> None:
    """多选标签按「包含」筛选：路线标了 family+solo，两类人群都能搜到。"""
    family_route = _published_route(db_session, title="亲子路线", audience=["family", "solo"])
    _published_route(db_session, title="只在夜里", audience=["couple"])

    hit = client.get(ROUTES_API, params={"audience": "family"})
    assert hit.status_code == 200, hit.text
    assert [item["id"] for item in hit.json()["items"]] == [str(family_route)]
    assert hit.json()["items"][0]["audience"] == ["family", "solo"]

    second_label = client.get(ROUTES_API, params={"audience": "solo"})
    assert [item["id"] for item in second_label.json()["items"]] == [str(family_route)]

    miss = client.get(ROUTES_API, params={"audience": "senior"})
    assert miss.json()["items"] == []


def test_unknown_audience_query_is_422(client: TestClient) -> None:
    response = client.get(ROUTES_API, params={"audience": "famliy"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_comment_replay_with_same_key_returns_same_row(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    """同 key 同 body 重试：回放首次评论，不插第二行（B12）。"""
    route_id = _published_route(db_session)
    url = f"{ROUTES_API}/{route_id}/comments"
    headers = {**user_headers, "Idempotency-Key": _key()}

    first = client.post(url, json={"body": "同一条评论只写一次"}, headers=headers)
    assert first.status_code == 201, first.text
    replay = client.post(url, json={"body": "同一条评论只写一次"}, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]

    listed = client.get(url).json()["items"]
    assert [item["id"] for item in listed] == [first.json()["id"]]


def test_comment_same_key_different_body_is_409(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    """同 key 换内容：409 IDEMPOTENCY_KEY_REUSED，不能悄悄复用首次结果。"""
    route_id = _published_route(db_session)
    url = f"{ROUTES_API}/{route_id}/comments"
    headers = {**user_headers, "Idempotency-Key": _key()}

    assert client.post(url, json={"body": "第一次的内容"}, headers=headers).status_code == 201
    conflict = client.post(url, json={"body": "换了内容"}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
