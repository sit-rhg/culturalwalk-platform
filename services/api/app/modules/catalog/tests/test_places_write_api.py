"""地点录入验收：RBAC、坐标范围、坐标系、来源必填、事实落库与审计。"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.deps import create_access_token
from app.modules.audit.models import AuditLog
from app.modules.catalog.models import Place, PlaceFact, PlaceSource, PlaceStatus

BASE = "/api/v1/places"


def _headers(role: str = "editor") -> dict[str, str]:
    user_id = uuid.uuid4()
    token = create_access_token(user_id=str(user_id), username=f"place-{role}", role=role)
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())}


def _payload(**overrides) -> dict:
    data = {
        "name": "岳麓书院",
        "district": "岳麓区",
        "lat": 28.18363,
        "lon": 112.93515,
        "summary": "中国古代四大书院之一",
        "culture_tags": ["历史", "书院", "历史"],
    }
    data.update(overrides)
    return data


def _create_place(client: TestClient, **overrides) -> dict:
    response = client.post(BASE, headers=_headers("editor"), json=_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def _fact_payload(**overrides) -> dict:
    data = {
        "claim": "岳麓书院始建于北宋开宝九年（976 年）。",
        "confidence": "verified",
        "source": {
            "title": "岳麓书院官网",
            "url": "https://example.edu.cn/history",
            "publisher": "湖南大学",
        },
    }
    data.update(overrides)
    return data


def _audit_actions(session_factory: sessionmaker[Session], object_id: str) -> list[str]:
    with session_factory() as db:
        rows = db.scalars(
            select(AuditLog).where(AuditLog.object_id == object_id).order_by(AuditLog.created_at)
        ).all()
        return [row.action for row in rows]


def test_create_place_lands_as_draft_with_crs(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    body = _create_place(client)

    assert body["status"] == "draft"
    assert body["name"] == "岳麓书院"
    assert body["culture_tags"] == ["历史", "书院"]
    assert set(body) == {
        "id",
        "name",
        "district",
        "lat",
        "lon",
        "summary",
        "culture_tags",
        "status",
    }

    with session_factory() as db:
        place = db.get(Place, uuid.UUID(body["id"]))
        assert place.crs == "OGC:CRS84"
        assert place.lat == Decimal("28.183630")
        assert place.status is PlaceStatus.DRAFT
    assert _audit_actions(session_factory, body["id"]) == ["place.create"]


def test_create_place_accepts_gcj02(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    body = _create_place(client, crs="GCJ-02")

    with session_factory() as db:
        assert db.get(Place, uuid.UUID(body["id"])).crs == "GCJ-02"


def test_create_place_requires_editor_role(client: TestClient) -> None:
    assert client.post(BASE, headers=_headers("user"), json=_payload()).status_code == 403
    assert client.post(BASE, json=_payload()).status_code == 401


def test_create_place_validates_coordinates_and_name(client: TestClient) -> None:
    lat = client.post(BASE, headers=_headers(), json=_payload(lat=91))
    assert lat.status_code == 422
    assert lat.json()["error"]["details"][0]["field"] == "lat"

    lon = client.post(BASE, headers=_headers(), json=_payload(lon=-181))
    assert lon.status_code == 422
    assert lon.json()["error"]["details"][0]["field"] == "lon"

    not_number = client.post(BASE, headers=_headers(), json=_payload(lat="北纬28度"))
    assert not_number.status_code == 422
    assert not_number.json()["error"]["details"][0]["field"] == "lat"

    blank = client.post(BASE, headers=_headers(), json=_payload(name="   "))
    assert blank.status_code == 422

    bad_crs = client.post(BASE, headers=_headers(), json=_payload(crs="BD-09"))
    assert bad_crs.status_code == 422
    assert "OGC:CRS84" in bad_crs.json()["error"]["details"][0]["expected"]

    without_key = client.post(
        BASE, headers={"Authorization": _headers()["Authorization"]}, json=_payload()
    )
    assert without_key.status_code == 422
    assert without_key.json()["error"]["details"][0]["field"] == "Idempotency-Key"


def test_create_fact_with_source(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    place = _create_place(client)

    response = client.post(f"{BASE}/{place['id']}/facts", headers=_headers(), json=_fact_payload())

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["claim"].startswith("岳麓书院始建于")
    assert body["confidence"] == "verified"
    assert body["sources"] == [
        {"title": "岳麓书院官网", "url": "https://example.edu.cn/history", "publisher": "湖南大学"}
    ]
    with session_factory() as db:
        fact = db.get(PlaceFact, uuid.UUID(body["id"]))
        assert fact.place_id == uuid.UUID(place["id"])
        assert fact.source.url == "https://example.edu.cn/history"
        assert db.scalar(select(PlaceSource).where(PlaceSource.id == fact.source_id)) is not None
    assert _audit_actions(session_factory, body["id"]) == ["place.fact.create"]

    # 编辑核对后把地点置为 verified，公开详情应能看到这条带来源的 fact
    with session_factory() as db:
        db.get(Place, uuid.UUID(place["id"])).status = PlaceStatus.VERIFIED
        db.commit()
    detail = client.get(f"{BASE}/{place['id']}")
    assert detail.status_code == 200
    assert detail.json()["facts"][0]["sources"][0]["url"] == "https://example.edu.cn/history"


def test_create_fact_requires_source_and_valid_url(client: TestClient) -> None:
    place = _create_place(client)
    endpoint = f"{BASE}/{place['id']}/facts"

    no_source = client.post(
        endpoint,
        headers=_headers(),
        json={"claim": "据传始建于北宋", "confidence": "likely"},
    )
    assert no_source.status_code == 422
    assert no_source.json()["error"]["details"][0]["field"] == "source"

    empty_source = client.post(endpoint, headers=_headers(), json=_fact_payload(source={}))
    assert empty_source.status_code == 422
    assert empty_source.json()["error"]["details"][0]["field"] == "source.title"

    ftp = client.post(
        endpoint,
        headers=_headers(),
        json=_fact_payload(source={"title": "某论坛", "url": "ftp://example.com/a"}),
    )
    assert ftp.status_code == 422
    assert ftp.json()["error"]["details"][0]["field"] == "source.url"

    no_scheme = client.post(
        endpoint,
        headers=_headers(),
        json=_fact_payload(source={"title": "某论坛", "url": "example.com/a"}),
    )
    assert no_scheme.status_code == 422

    bad_confidence = client.post(
        endpoint,
        headers=_headers(),
        json=_fact_payload(confidence="probably"),
    )
    assert bad_confidence.status_code == 422
    assert bad_confidence.json()["error"]["details"][0]["field"] == "confidence"


def test_create_fact_permissions_and_missing_place(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    place = _create_place(client)

    assert (
        client.post(
            f"{BASE}/{place['id']}/facts", headers=_headers("user"), json=_fact_payload()
        ).status_code
        == 403
    )
    assert client.post(f"{BASE}/{place['id']}/facts", json=_fact_payload()).status_code == 401

    missing = client.post(f"{BASE}/{uuid.uuid4()}/facts", headers=_headers(), json=_fact_payload())
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "PLACE_NOT_FOUND"
    with session_factory() as db:
        assert list(db.scalars(select(PlaceFact))) == []


def test_create_place_is_idempotent_per_key(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """同一个 Idempotency-Key 重试必须返回同一个 id，不能再落一行。"""
    headers = _headers("editor")
    first = client.post(BASE, headers=headers, json=_payload())
    second = client.post(BASE, headers=headers, json=_payload())
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    with session_factory() as db:
        assert len(db.scalars(select(Place)).all()) == 1


def test_same_key_with_different_body_is_conflict(client: TestClient) -> None:
    headers = _headers("editor")
    assert client.post(BASE, headers=headers, json=_payload()).status_code == 201
    changed = client.post(BASE, headers=headers, json=_payload(name="橘子洲"))
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_add_fact_is_idempotent_per_key(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    place = _create_place(client)
    headers = _headers("editor")
    url = f"{BASE}/{place['id']}/facts"
    first = client.post(url, headers=headers, json=_fact_payload())
    second = client.post(url, headers=headers, json=_fact_payload())
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    with session_factory() as db:
        assert len(db.scalars(select(PlaceFact)).all()) == 1
        assert len(db.scalars(select(PlaceSource)).all()) == 1
