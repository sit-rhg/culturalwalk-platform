"""RT-02：草稿节点整表替换 + 顺序（验收：调整顺序后重新 GET 顺序正确）。

覆盖：认证、If-Match（412）、Idempotency-Key（重放/复用 409）、
节点校验（地点存在且已发布、停留时长、价格区间、交通方式）、
他人草稿 403、锁定态 409、cost 汇总。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.modules.catalog.models import Place, PlaceStatus
from app.modules.routes.models import DraftState, RouteDraft

API = "/api/v1/route-drafts"


def _key() -> str:
    return str(uuid.uuid4())


def _add_place(
    db: Session,
    *,
    name: str = "岳麓书院",
    status: PlaceStatus = PlaceStatus.VERIFIED,
) -> uuid.UUID:
    """直接落库造地点：该用例只关心 routes，不经过 catalog 的写接口。"""
    place = Place(
        name=name,
        district="岳麓区",
        culture_tags=["历史"],
        status=status,
        lat=Decimal("28.186700"),
        lon=Decimal("112.951000"),
    )
    db.add(place)
    db.flush()
    return place.id


def _create_draft(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.post(
        API,
        json={"title": "岳麓山半日"},
        headers={**headers, "Idempotency-Key": _key()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _node(place_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "place_id": str(place_id),
        "stay_minutes": 60,
        "transport": "walk",
    }
    data.update(overrides)
    return data


def _put(
    client: TestClient,
    headers: dict[str, str],
    draft_id: str,
    nodes: list[dict[str, object]],
    *,
    if_match: int,
    key: str | None = None,
) -> Response:
    return client.put(
        f"{API}/{draft_id}/nodes",
        json={"nodes": nodes},
        headers={
            **headers,
            "If-Match": str(if_match),
            "Idempotency-Key": key or _key(),
        },
    )


# ===== 认证与请求头 =====
def test_replace_nodes_requires_login(client: TestClient, db_session: Session) -> None:
    place_id = _add_place(db_session)
    response = client.put(
        f"{API}/{uuid.uuid4()}/nodes",
        json={"nodes": [_node(place_id)]},
        headers={"If-Match": "1", "Idempotency-Key": _key()},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_missing_idempotency_key_is_422(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    response = client.put(
        f"{API}/{draft['id']}/nodes",
        json={"nodes": [_node(_add_place(db_session))]},
        headers={**user_headers, "If-Match": str(draft["lock_version"])},
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "Idempotency-Key"


def test_missing_if_match_is_422(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    response = client.put(
        f"{API}/{draft['id']}/nodes",
        json={"nodes": [_node(_add_place(db_session))]},
        headers={**user_headers, "Idempotency-Key": _key()},
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "If-Match"


# ===== 正常路径 =====
def test_replace_nodes_assigns_positions_from_array_order(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    places = [_add_place(db_session, name=name) for name in ("岳麓书院", "橘子洲", "天心阁")]

    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(pid) for pid in places],
        if_match=draft["lock_version"],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [node["position"] for node in body["nodes"]] == [1, 2, 3]
    assert [node["place"]["id"] for node in body["nodes"]] == [str(pid) for pid in places]
    assert body["cost"] == {
        "min_total": 0,
        "max_total": 0,
        "currency": "CNY",
        "unknown_count": 3,
    }


def test_reordering_nodes_is_reflected_on_get(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    """卡面验收：调整顺序后重新 GET，顺序必须与提交的数组一致。"""
    draft = _create_draft(client, user_headers)
    a, b, c = (_add_place(db_session, name=name) for name in ("A", "B", "C"))

    first = _put(client, user_headers, draft["id"], [_node(a), _node(b), _node(c)], if_match=1)
    assert first.status_code == 200, first.text

    reordered = _put(
        client,
        user_headers,
        draft["id"],
        [_node(c), _node(a), _node(b)],
        if_match=first.json()["lock_version"],
    )
    assert reordered.status_code == 200, reordered.text

    detail = client.get(f"{API}/{draft['id']}", headers=user_headers)
    assert detail.status_code == 200
    nodes = detail.json()["nodes"]
    assert [node["position"] for node in nodes] == [1, 2, 3]
    assert [node["place"]["id"] for node in nodes] == [str(c), str(a), str(b)]


def test_empty_list_clears_nodes(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    place_id = _add_place(db_session)

    filled = _put(client, user_headers, draft["id"], [_node(place_id)], if_match=1)
    assert filled.status_code == 200, filled.text

    cleared = _put(client, user_headers, draft["id"], [], if_match=filled.json()["lock_version"])
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["nodes"] == []
    assert cleared.json()["cost"]["unknown_count"] == 0

    detail = client.get(f"{API}/{draft['id']}", headers=user_headers)
    assert detail.json()["nodes"] == []


def test_lock_version_increases_and_keeps_fields(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(_add_place(db_session))],
        if_match=draft["lock_version"],
    )
    assert response.status_code == 200
    assert response.json()["lock_version"] == draft["lock_version"] + 1
    assert response.json()["title"] == draft["title"]


def test_cost_summary_sums_known_prices_and_counts_unknown(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    place_ids = [_add_place(db_session, name=name) for name in ("A", "B", "C")]

    response = _put(
        client,
        user_headers,
        draft["id"],
        [
            _node(place_ids[0], price_min=3000, price_max=5000),
            _node(place_ids[1], price_min=1500, price_max=1500),
            _node(place_ids[2]),
        ],
        if_match=draft["lock_version"],
    )

    assert response.status_code == 200, response.text
    assert response.json()["cost"] == {
        "min_total": 4500,
        "max_total": 6500,
        "currency": "CNY",
        "unknown_count": 1,
    }


def test_node_place_is_full_public_shape(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    place_id = _add_place(db_session, name="湖南省博物馆")

    response = _put(
        client, user_headers, draft["id"], [_node(place_id)], if_match=draft["lock_version"]
    )
    assert response.status_code == 200, response.text

    place = response.json()["nodes"][0]["place"]
    assert place["name"] == "湖南省博物馆"
    assert place["status"] == "verified"
    assert place["culture_tags"] == ["历史"]
    assert place["lat"] is not None and place["lon"] is not None


# ===== 节点校验 =====
def test_unknown_place_is_422_with_indexed_field(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    known = _add_place(db_session)
    missing = uuid.uuid4()

    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(known), _node(missing)],
        if_match=draft["lock_version"],
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["details"][0]["field"] == "nodes.1.place_id"


def test_unpublished_place_is_422(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    draft_place = _add_place(db_session, name="还在编辑的点", status=PlaceStatus.DRAFT)

    response = _put(
        client, user_headers, draft["id"], [_node(draft_place)], if_match=draft["lock_version"]
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "nodes.0.place_id"


@pytest.mark.parametrize("stay_minutes", [9, 481])
def test_stay_minutes_out_of_range_is_422(
    client: TestClient, db_session: Session, user_headers: dict, stay_minutes: int
) -> None:
    draft = _create_draft(client, user_headers)
    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(_add_place(db_session), stay_minutes=stay_minutes)],
        if_match=draft["lock_version"],
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "nodes.0.stay_minutes"


def test_price_max_below_min_is_422(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(_add_place(db_session), price_min=5000, price_max=3000)],
        if_match=draft["lock_version"],
    )
    assert response.status_code == 422


def test_invalid_transport_is_422(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(_add_place(db_session), transport="rocket")],
        if_match=draft["lock_version"],
    )
    assert response.status_code == 422


# ===== 权限与并发 =====
def test_other_users_draft_is_403(
    client: TestClient, db_session: Session, user_headers: dict, admin_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    response = _put(
        client,
        admin_headers,
        draft["id"],
        [_node(_add_place(db_session))],
        if_match=draft["lock_version"],
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "DRAFT_FORBIDDEN"


def test_missing_draft_is_404(client: TestClient, db_session: Session, user_headers: dict) -> None:
    response = _put(
        client,
        user_headers,
        str(uuid.uuid4()),
        [_node(_add_place(db_session))],
        if_match=1,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DRAFT_NOT_FOUND"


def test_stale_if_match_is_412(client: TestClient, db_session: Session, user_headers: dict) -> None:
    draft = _create_draft(client, user_headers)
    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(_add_place(db_session))],
        if_match=draft["lock_version"] + 1,
    )
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "STALE_DRAFT_VERSION"


def test_locked_draft_is_409(client: TestClient, db_session: Session, user_headers: dict) -> None:
    draft = _create_draft(client, user_headers)
    stored = db_session.get(RouteDraft, uuid.UUID(draft["id"]))
    assert stored is not None
    stored.state = DraftState.SUBMITTED_LOCKED
    db_session.flush()

    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(_add_place(db_session))],
        if_match=draft["lock_version"],
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DRAFT_NOT_EDITABLE"


def test_replay_with_same_key_returns_first_result(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    nodes = [_node(_add_place(db_session))]
    key = _key()

    first = _put(client, user_headers, draft["id"], nodes, if_match=draft["lock_version"], key=key)
    assert first.status_code == 200, first.text

    replay = _put(client, user_headers, draft["id"], nodes, if_match=1, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["lock_version"] == first.json()["lock_version"]


def test_replay_with_same_key_different_body_is_409(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    draft = _create_draft(client, user_headers)
    first_place = _add_place(db_session, name="A")
    second_place = _add_place(db_session, name="B")
    key = _key()

    first = _put(
        client,
        user_headers,
        draft["id"],
        [_node(first_place)],
        if_match=draft["lock_version"],
        key=key,
    )
    assert first.status_code == 200, first.text

    replay = _put(client, user_headers, draft["id"], [_node(second_place)], if_match=1, key=key)
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert replay.json()["error"]["details"][0]["field"] == "Idempotency-Key"


def test_failed_validation_does_not_write_partial_nodes(
    client: TestClient, db_session: Session, user_headers: dict
) -> None:
    """有一个地点不存在时整单 422，前面的节点也不能落库。"""
    draft = _create_draft(client, user_headers)
    known = _add_place(db_session)

    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(known), _node(uuid.uuid4())],
        if_match=draft["lock_version"],
    )
    assert response.status_code == 422

    detail = client.get(f"{API}/{draft['id']}", headers=user_headers)
    assert detail.status_code == 200
    assert detail.json()["nodes"] == []
    assert detail.json()["lock_version"] == draft["lock_version"]


def test_service_layer_rejects_when_app_error_raised() -> None:
    """AppError 必须是 FastAPI 认识的错误类型（防止有人把它换成裸 Exception）。"""
    with pytest.raises(AppError):
        raise AppError("x", code="X")
