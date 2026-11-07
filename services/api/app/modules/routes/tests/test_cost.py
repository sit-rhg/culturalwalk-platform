"""RT-04：费用汇总（验收：空节点返回 0；部分节点缺价返回 unknown_count）。

汇总逻辑在 RT-02 的 `_cost_of` 里，本文件只把卡面的边界单独钉住：
空列表、全缺价、部分缺价、只有单边价格——这几种情况下 totals 与
unknown_count 的组合最容易写错。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.catalog.models import Place, PlaceStatus
from app.modules.routes.models import RouteDraft

API = "/api/v1/route-drafts"
# 与 conftest.USER_ID 一致：user_headers 签的就是这个身份。
OWNER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _key() -> str:
    return str(uuid.uuid4())


def _add_place(db: Session, name: str) -> uuid.UUID:
    place = Place(
        name=name,
        district="岳麓区",
        culture_tags=["历史"],
        status=PlaceStatus.VERIFIED,
        lat=Decimal("28.186700"),
        lon=Decimal("112.951000"),
    )
    db.add(place)
    db.flush()
    return place.id


def _draft(db: Session) -> RouteDraft:
    draft = RouteDraft(owner_id=OWNER_ID, title="费用汇总")
    db.add(draft)
    db.flush()
    return draft


def _put_nodes(client: TestClient, draft_id: uuid.UUID, nodes: list[dict], headers: dict) -> dict:
    response = client.put(
        f"{API}/{draft_id}/nodes",
        json={"nodes": nodes},
        headers={**headers, "Idempotency-Key": _key(), "If-Match": "1"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _node(place_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {"place_id": str(place_id), "stay_minutes": 30, "transport": "walk"}
    data.update(overrides)
    return data


def test_empty_draft_costs_zero(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    draft = _draft(db_session)
    detail = client.get(f"{API}/{draft.id}", headers=user_headers).json()

    assert detail["cost"] == {
        "min_total": 0,
        "max_total": 0,
        "currency": "CNY",
        "unknown_count": 0,
    }


def test_all_missing_prices_count_as_unknown(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    draft = _draft(db_session)
    places = [_add_place(db_session, f"全缺价{index}") for index in range(3)]

    detail = _put_nodes(client, draft.id, [_node(place_id) for place_id in places], user_headers)

    assert detail["cost"]["min_total"] == 0
    assert detail["cost"]["max_total"] == 0
    assert detail["cost"]["unknown_count"] == 3


def test_mixed_prices_exclude_missing_from_totals(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    draft = _draft(db_session)
    priced = _add_place(db_session, "有价格")
    unpriced = _add_place(db_session, "没价格")

    detail = _put_nodes(
        client,
        draft.id,
        [
            _node(priced, price_min=1000, price_max=2000),
            _node(unpriced),
            # 只有下限：上限缺失，这一条也算 unknown，且不按 0 计入 max_total。
            _node(priced, price_min=500),
        ],
        user_headers,
    )

    assert detail["cost"] == {
        "min_total": 1500,
        "max_total": 2000,
        "currency": "CNY",
        "unknown_count": 2,
    }


def test_get_detail_after_reload_keeps_cost(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    """汇总不是写死的返回值：重新 GET 也要按当前节点算出来。"""
    draft = _draft(db_session)
    place = _add_place(db_session, "重新读取")
    _put_nodes(client, draft.id, [_node(place, price_min=300, price_max=900)], user_headers)

    detail = client.get(f"{API}/{draft.id}", headers=user_headers).json()
    assert detail["cost"] == {
        "min_total": 300,
        "max_total": 900,
        "currency": "CNY",
        "unknown_count": 0,
    }


def test_only_min_and_only_max_nodes_never_invert_the_range(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    """A 站只有最低价、B 站只有最高价：拼出来的区间不能倒挂（¥1000 - ¥50）。"""
    draft = _draft(db_session)
    cheaper = _add_place(db_session, "只有最低价")
    pricier = _add_place(db_session, "只有最高价")

    detail = _put_nodes(
        client,
        draft.id,
        [
            _node(cheaper, price_min=1000),
            _node(pricier, price_max=50),
        ],
        user_headers,
    )

    assert detail["cost"]["min_total"] == 1000
    assert detail["cost"]["max_total"] == 1000
    assert detail["cost"]["unknown_count"] == 2
