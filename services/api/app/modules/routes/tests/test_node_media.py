"""RT-05：节点引用媒体资产。

覆盖卡面验收「引用非 CLEAN 资产返回 422」，以及：
- 本人的 CLEAN 资产落库并在 PUT / GET 详情里回显；
- 他人资产、未知 id 一律同为 422（不泄露别人 asset 是否存在）；
- 整表替换后旧引用被清空（换一组节点就是换一组图）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from app.modules.catalog.models import Place, PlaceStatus
from app.modules.media.models import MediaAsset, MediaZone, ScanState

API = "/api/v1/route-drafts"

# 与 conftest 的 user_headers / admin_headers 保持一致：
# 共享 token 才有「本人资产」与「他人资产」的区别。
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ADMIN_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _key() -> str:
    return str(uuid.uuid4())


def _add_place(db: Session) -> uuid.UUID:
    place = Place(
        name="岳麓书院",
        district="岳麓区",
        culture_tags=["历史"],
        status=PlaceStatus.VERIFIED,
        lat=Decimal("28.186700"),
        lon=Decimal("112.951000"),
    )
    db.add(place)
    db.flush()
    return place.id


def _add_asset(
    db: Session,
    *,
    owner_id: uuid.UUID = USER_ID,
    zone: MediaZone = MediaZone.CLEAN,
    scan_state: ScanState = ScanState.CLEAN,
) -> uuid.UUID:
    asset = MediaAsset(
        owner_id=owner_id,
        object_key=f"clean/{uuid.uuid4()}",
        filename="cover.jpg",
        mime="image/jpeg",
        size=1024,
        zone=zone,
        scan_state=scan_state,
        upload_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db.add(asset)
    db.flush()
    return asset.id


def _create_draft(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.post(
        API,
        json={"title": "岳麓山半日"},
        headers={**headers, "Idempotency-Key": _key()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _node(place_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {"place_id": str(place_id), "stay_minutes": 60, "transport": "walk"}
    data.update(overrides)
    return data


def _put(
    client: TestClient,
    headers: dict[str, str],
    draft_id: str,
    nodes: list[dict[str, object]],
    *,
    if_match: int,
) -> Response:
    return client.put(
        f"{API}/{draft_id}/nodes",
        json={"nodes": nodes},
        headers={**headers, "If-Match": str(if_match), "Idempotency-Key": _key()},
    )


def test_clean_asset_is_stored_and_rendered(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    place_id = _add_place(db_session)
    asset_id = _add_asset(db_session)
    draft = _create_draft(client, user_headers)

    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(place_id, media_asset_ids=[str(asset_id)])],
        if_match=draft["lock_version"],
    )

    assert response.status_code == 200, response.text
    node = response.json()["nodes"][0]
    assert node["media_asset_ids"] == [str(asset_id)]

    detail = client.get(f"{API}/{draft['id']}", headers=user_headers)
    assert detail.status_code == 200
    assert detail.json()["nodes"][0]["media_asset_ids"] == [str(asset_id)]


def test_non_clean_asset_is_rejected(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    place_id = _add_place(db_session)
    asset_id = _add_asset(db_session, zone=MediaZone.QUARANTINE, scan_state=ScanState.SCANNING)
    draft = _create_draft(client, user_headers)

    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(place_id, media_asset_ids=[str(asset_id)])],
        if_match=draft["lock_version"],
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"][0]["field"] == "nodes.0.media_asset_ids"


def test_other_users_asset_is_rejected(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    place_id = _add_place(db_session)
    asset_id = _add_asset(db_session, owner_id=ADMIN_ID)
    draft = _create_draft(client, user_headers)

    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(place_id, media_asset_ids=[str(asset_id)])],
        if_match=draft["lock_version"],
    )

    # 与「不存在」同样的 422：否则可以用状态码探测别人的 asset id。
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"][0]["field"] == "nodes.0.media_asset_ids"


def test_unknown_asset_is_rejected(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    place_id = _add_place(db_session)
    draft = _create_draft(client, user_headers)

    response = _put(
        client,
        user_headers,
        draft["id"],
        [_node(place_id, media_asset_ids=[str(uuid.uuid4())])],
        if_match=draft["lock_version"],
    )

    assert response.status_code == 422, response.text


def test_replacing_nodes_drops_old_media(
    client: TestClient, db_session: Session, user_headers: dict[str, str]
) -> None:
    place_id = _add_place(db_session)
    asset_id = _add_asset(db_session)
    draft = _create_draft(client, user_headers)

    first = _put(
        client,
        user_headers,
        draft["id"],
        [_node(place_id, media_asset_ids=[str(asset_id)])],
        if_match=draft["lock_version"],
    )
    assert first.status_code == 200, first.text

    second = _put(
        client,
        user_headers,
        draft["id"],
        [_node(place_id)],
        if_match=first.json()["lock_version"],
    )

    assert second.status_code == 200, second.text
    assert second.json()["nodes"][0]["media_asset_ids"] == []
