"""ME-03 媒体删除验收：软删、引用检查、越权与审计。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.deps import create_access_token
from app.core.errors import NotFound
from app.modules.audit.models import AuditLog
from app.modules.media.models import MediaAsset, MediaZone, ScanState
from app.modules.media.service import MediaService
from app.modules.routes.models import DraftState, RouteDraft, RouteNode

BASE = "/api/v1/media"


def _headers(user_id: uuid.UUID, role: str = "user") -> dict[str, str]:
    token = create_access_token(user_id=str(user_id), username=f"media-{role}", role=role)
    return {"Authorization": f"Bearer {token}"}


def _asset(session_factory, *, owner_id: uuid.UUID) -> MediaAsset:
    asset = MediaAsset(
        id=uuid.uuid4(),
        owner_id=owner_id,
        object_key=f"clean/{owner_id}/{uuid.uuid4()}/photo.jpg",
        filename="photo.jpg",
        mime="image/jpeg",
        size=1024,
        zone=MediaZone.CLEAN,
        scan_state=ScanState.CLEAN,
        upload_expires_at=datetime.now(UTC) + timedelta(hours=1),
        url="http://localhost:9000/culturalwalk/clean/photo.jpg",
    )
    with session_factory() as db:
        db.add(asset)
        draft = RouteDraft(
            id=uuid.uuid4(),
            owner_id=owner_id,
            title="引用媒体的草稿",
            state=DraftState.EDITING,
            lock_version=1,
        )
        db.add(draft)
        db.flush()
        db.add(
            RouteNode(
                id=uuid.uuid4(),
                draft_id=draft.id,
                place_id=uuid.uuid4(),
                position=1,
                stay_minutes=30,
                media_asset_ids=[str(asset.id)],
            )
        )
        db.commit()
        db.refresh(asset)
        return asset


def test_delete_own_unreferenced_asset_is_soft_delete(client: TestClient, session_factory) -> None:
    me = uuid.uuid4()
    asset = _asset(session_factory, owner_id=me)
    # 先解除引用：这条用例只验软删本身
    with session_factory() as db:
        for node in db.scalars(select(RouteNode)).all():
            node.media_asset_ids = []
        db.commit()

    response = client.delete(f"{BASE}/{asset.id}", headers=_headers(me))

    assert response.status_code == 204
    assert client.get(f"{BASE}/{asset.id}", headers=_headers(me)).status_code == 404
    with session_factory() as db:
        row = db.get(MediaAsset, asset.id)
        assert row is not None
        assert row.deleted_at is not None


def test_delete_referenced_asset_returns_409(client: TestClient, session_factory) -> None:
    me = uuid.uuid4()
    asset = _asset(session_factory, owner_id=me)

    response = client.delete(f"{BASE}/{asset.id}", headers=_headers(me))

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "MEDIA_IN_USE"
    assert body["error"]["details"]
    with session_factory() as db:
        assert db.get(MediaAsset, asset.id).deleted_at is None


def test_delete_others_asset_is_404(client: TestClient, session_factory) -> None:
    owner = uuid.uuid4()
    asset = _asset(session_factory, owner_id=owner)

    response = client.delete(f"{BASE}/{asset.id}", headers=_headers(uuid.uuid4()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MEDIA_NOT_FOUND"


def test_delete_unknown_asset_is_404(client: TestClient) -> None:
    response = client.delete(f"{BASE}/{uuid.uuid4()}", headers=_headers(uuid.uuid4()))

    assert response.status_code == 404


def test_delete_twice_is_404(client: TestClient, session_factory) -> None:
    me = uuid.uuid4()
    asset = _asset(session_factory, owner_id=me)
    with session_factory() as db:
        for node in db.scalars(select(RouteNode)).all():
            node.media_asset_ids = []
        db.commit()

    assert client.delete(f"{BASE}/{asset.id}", headers=_headers(me)).status_code == 204
    assert client.delete(f"{BASE}/{asset.id}", headers=_headers(me)).status_code == 404


def test_deleted_asset_cannot_be_referenced(client: TestClient, session_factory) -> None:
    me = uuid.uuid4()
    asset = _asset(session_factory, owner_id=me)
    with session_factory() as db:
        for node in db.scalars(select(RouteNode)).all():
            node.media_asset_ids = []
        db.commit()

    assert client.delete(f"{BASE}/{asset.id}", headers=_headers(me)).status_code == 204
    with session_factory() as db, pytest.raises(NotFound):
        MediaService(db).require_referenceable(asset.id)


def test_delete_writes_audit_log(client: TestClient, session_factory) -> None:
    me = uuid.uuid4()
    asset = _asset(session_factory, owner_id=me)
    with session_factory() as db:
        for node in db.scalars(select(RouteNode)).all():
            node.media_asset_ids = []
        db.commit()

    client.delete(f"{BASE}/{asset.id}", headers=_headers(me))

    with session_factory() as db:
        actions = db.scalars(
            select(AuditLog.action).where(AuditLog.object_id == str(asset.id))
        ).all()
    assert "media.delete" in actions
