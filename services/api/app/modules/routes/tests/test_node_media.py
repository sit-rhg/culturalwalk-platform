"""RT-05 媒体绑定：先写用例，实现前应当全红。

卡面：草稿节点引用 media 资产；引用非 CLEAN 资产返回 422。
契约：RouteNodeInput.media_asset_ids 是 uuid 数组；本卡把它真正落库并校验，
详情里回显同一份 id（前端草稿页要显示"已配图 X 张"）。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.modules.catalog.models import Place, PlaceStatus
from app.modules.media.models import MediaAsset, MediaZone, ScanState
from conftest import USER_ID


def _place(db_session, name: str = "岳麓书院") -> uuid.UUID:
    place = Place(
        id=uuid.uuid4(),
        name=name,
        lat=28.1867,
        lon=112.9510,
        district="岳麓区",
        summary="测试点位，用于 RT-05 的节点引用。",
        culture_tags=["历史"],
        status=PlaceStatus.VERIFIED,
    )
    db_session.add(place)
    db_session.flush()
    return place.id


def _asset(
    db_session,
    *,
    owner_id: uuid.UUID = uuid.UUID(USER_ID),
    zone: MediaZone = MediaZone.CLEAN,
    scan_state: ScanState = ScanState.CLEAN,
) -> uuid.UUID:
    asset = MediaAsset(
        id=uuid.uuid4(),
        owner_id=owner_id,
        object_key=f"clean/{uuid.uuid4()}",
        mime="image/jpeg",
        size=1024,
        checksum="0" * 64,
        zone=zone,
        scan_state=scan_state,
        idempotency_key=f"rt05-{uuid.uuid4()}",
        input_hash="0" * 64,
    )
    db_session.add(asset)
    db_session.flush()
    return asset.id


def _draft(client: TestClient, user_headers, key: str = "rt05-create") -> str:
    resp = client.post(
        "/api/v1/route-drafts",
        json={"title": "带图的路线"},
        headers={**user_headers, "Idempotency-Key": f"rt05-draft-{key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _put_nodes(
    client: TestClient,
    user_headers,
    draft_id: str,
    nodes: list[dict],
    key: str = "n1",
    if_match: int = 1,
):
    return client.put(
        f"/api/v1/route-drafts/{draft_id}/nodes",
        json={"nodes": nodes},
        headers={
            **user_headers,
            "Idempotency-Key": f"rt05-nodes-{key}",
            "If-Match": str(if_match),
        },
    )


def _node(place_id: uuid.UUID, asset_ids: list[str] | None = None) -> dict:
    body: dict = {"place_id": str(place_id), "stay_minutes": 60}
    if asset_ids is not None:
        body["media_asset_ids"] = asset_ids
    return body


def test_clean_media_ids_are_persisted_and_echoed(
    db_session, client: TestClient, user_headers
) -> None:
    draft_id = _draft(client, user_headers)
    place_id = _place(db_session)
    asset_id = _asset(db_session)

    resp = _put_nodes(client, user_headers, draft_id, [_node(place_id, [str(asset_id)])])
    assert resp.status_code == 200, resp.text
    assert resp.json()["nodes"][0]["media_asset_ids"] == [str(asset_id)]

    detail = client.get(f"/api/v1/route-drafts/{draft_id}", headers=user_headers)
    assert detail.status_code == 200
    assert detail.json()["nodes"][0]["media_asset_ids"] == [str(asset_id)]


def test_media_not_scanned_yet_is_rejected(db_session, client: TestClient, user_headers) -> None:
    draft_id = _draft(client, user_headers)
    place_id = _place(db_session)
    asset_id = _asset(db_session, zone=MediaZone.QUARANTINE, scan_state=ScanState.SCANNING)

    resp = _put_nodes(client, user_headers, draft_id, [_node(place_id, [str(asset_id)])])

    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["field"] == "nodes.0.media_asset_ids"


def test_other_users_media_is_rejected(db_session, client: TestClient, user_headers) -> None:
    draft_id = _draft(client, user_headers)
    place_id = _place(db_session)
    asset_id = _asset(db_session, owner_id=uuid.uuid4())

    resp = _put_nodes(client, user_headers, draft_id, [_node(place_id, [str(asset_id)])])

    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["field"] == "nodes.0.media_asset_ids"


def test_unknown_media_is_rejected(db_session, client: TestClient, user_headers) -> None:
    draft_id = _draft(client, user_headers)
    place_id = _place(db_session)

    resp = _put_nodes(client, user_headers, draft_id, [_node(place_id, [str(uuid.uuid4())])])

    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["field"] == "nodes.0.media_asset_ids"


def test_replacing_nodes_drops_old_media(db_session, client: TestClient, user_headers) -> None:
    draft_id = _draft(client, user_headers)
    place_id = _place(db_session)
    asset_id = _asset(db_session)

    first = _put_nodes(client, user_headers, draft_id, [_node(place_id, [str(asset_id)])])
    assert first.status_code == 200, first.text
    resp = _put_nodes(
        client,
        user_headers,
        draft_id,
        [_node(place_id)],
        key="n2",
        if_match=first.json()["lock_version"],
    )

    assert resp.status_code == 200
    assert resp.json()["nodes"][0]["media_asset_ids"] == []
