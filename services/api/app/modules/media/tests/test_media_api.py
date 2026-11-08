"""媒体上传验收测试：预签名占位、状态机、越权、过期、幂等与 clean 引用闸门。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.deps import create_access_token
from app.core.errors import Conflict
from app.modules.audit.models import AuditLog
from app.modules.media.models import MediaAsset, MediaZone, ScanState
from app.modules.media.service import MediaService

BASE = "/api/v1/media"


def _headers(user_id: uuid.UUID, role: str = "user") -> dict[str, str]:
    token = create_access_token(user_id=str(user_id), username=f"media-{role}", role=role)
    return {"Authorization": f"Bearer {token}"}


def _intent(
    client: TestClient,
    user_id: uuid.UUID,
    *,
    key: str | None = None,
    role: str = "user",
    payload: dict | None = None,
):
    return client.post(
        f"{BASE}/intent",
        headers={**_headers(user_id, role), "Idempotency-Key": key or str(uuid.uuid4())},
        json=payload or {"filename": "yuelu.jpg", "mime": "image/jpeg", "size": 2048},
    )


def _complete(client: TestClient, media_id: str, user_id: uuid.UUID, *, role: str = "user"):
    return client.post(
        f"{BASE}/{media_id}/complete",
        headers={**_headers(user_id, role), "Idempotency-Key": str(uuid.uuid4())},
    )


def _actions(session_factory: sessionmaker[Session], object_id: str) -> list[str]:
    with session_factory() as db:
        rows = db.scalars(
            select(AuditLog).where(AuditLog.object_id == object_id).order_by(AuditLog.created_at)
        ).all()
        return [row.action for row in rows]


def test_intent_returns_ticket_and_stays_in_quarantine(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    me = uuid.uuid4()

    response = _intent(client, me, role="editor")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"asset_id", "upload_url", "fields", "expires_at"}
    assert body["upload_url"].startswith("http://localhost:9000/culturalwalk")
    assert body["fields"]["key"].startswith(f"quarantine/{me}/")
    assert body["fields"]["x-amz-signature"]
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert expires_at > datetime.now(UTC)

    with session_factory() as db:
        asset = db.get(MediaAsset, uuid.UUID(body["asset_id"]))
        assert asset is not None
        assert asset.owner_id == me
        assert asset.zone is MediaZone.QUARANTINE
        assert asset.scan_state is ScanState.PENDING
        assert asset.size == 2048
    assert _actions(session_factory, body["asset_id"]) == ["media.intent.create"]


def test_intent_is_idempotent(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    me = uuid.uuid4()
    key = str(uuid.uuid4())

    first = _intent(client, me, key=key)
    second = _intent(client, me, key=key)

    assert first.status_code == second.status_code == 201
    assert first.json()["asset_id"] == second.json()["asset_id"]
    assert first.json()["expires_at"] == second.json()["expires_at"]
    with session_factory() as db:
        assert len(list(db.scalars(select(MediaAsset)))) == 1


def test_intent_rejects_bad_input(client: TestClient) -> None:
    me = uuid.uuid4()

    bad_mime = _intent(client, me, payload={"filename": "a.gif", "mime": "image/gif", "size": 10})
    assert bad_mime.status_code == 422
    detail = bad_mime.json()["error"]["details"][0]
    assert detail["field"] == "mime"
    assert "image/jpeg" in detail["expected"]

    too_big = _intent(
        client,
        me,
        payload={"filename": "a.jpg", "mime": "image/jpeg", "size": 10 * 1024 * 1024 + 1},
    )
    assert too_big.status_code == 422
    assert too_big.json()["error"]["details"][0]["field"] == "size"

    blank_name = _intent(client, me, payload={"filename": "  ", "mime": "image/jpeg", "size": 10})
    assert blank_name.status_code == 422

    assert client.post(f"{BASE}/intent", json={"filename": "a.jpg"}).status_code == 401


def test_complete_moves_to_scanning_and_is_idempotent(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    me = uuid.uuid4()
    asset_id = _intent(client, me).json()["asset_id"]

    first = _complete(client, asset_id, me)
    second = _complete(client, asset_id, me)

    assert first.status_code == 202
    assert first.json()["zone"] == "quarantine"
    assert first.json()["scan_state"] == "scanning"
    assert first.json()["url"] is None
    assert second.status_code == 202
    assert second.json()["scan_state"] == "scanning"
    with session_factory() as db:
        assert db.get(MediaAsset, uuid.UUID(asset_id)).completed_at is not None
    assert _actions(session_factory, asset_id) == ["media.intent.create", "media.complete"]


def test_complete_requires_owner(client: TestClient) -> None:
    me, other = uuid.uuid4(), uuid.uuid4()
    asset_id = _intent(client, me).json()["asset_id"]

    response = _complete(client, asset_id, other)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MEDIA_FORBIDDEN"


def test_complete_rejects_expired_and_rejected(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    me = uuid.uuid4()
    expired_id = _intent(client, me).json()["asset_id"]
    with session_factory() as db:
        asset = db.get(MediaAsset, uuid.UUID(expired_id))
        asset.upload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    expired = _complete(client, expired_id, me)
    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "MEDIA_UPLOAD_EXPIRED"

    rejected_id = _intent(client, me).json()["asset_id"]
    with session_factory() as db:
        asset = db.get(MediaAsset, uuid.UUID(rejected_id))
        asset.scan_state = ScanState.REJECTED
        db.commit()

    rejected = _complete(client, rejected_id, me)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "MEDIA_REJECTED"


def test_get_permissions_and_url_visibility(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    me, other = uuid.uuid4(), uuid.uuid4()
    asset_id = _intent(client, me).json()["asset_id"]
    _complete(client, asset_id, me)

    mine = client.get(f"{BASE}/{asset_id}", headers=_headers(me))
    assert mine.status_code == 200
    assert mine.json()["url"] is None

    assert client.get(f"{BASE}/{asset_id}", headers=_headers(other)).status_code == 403
    assert client.get(f"{BASE}/{asset_id}", headers=_headers(other, "editor")).status_code == 200
    assert client.get(f"{BASE}/{uuid.uuid4()}", headers=_headers(me)).status_code == 404
    assert client.get(f"{BASE}/{asset_id}").status_code == 401

    # 扫描通过后才允许给公开 URL（本机没有扫描器，走 service 钩子模拟）
    with session_factory() as db:
        MediaService(db).mark_clean(uuid.UUID(asset_id))
        db.commit()

    cleaned = client.get(f"{BASE}/{asset_id}", headers=_headers(me))
    assert cleaned.json()["zone"] == "clean"
    assert cleaned.json()["scan_state"] == "clean"
    assert cleaned.json()["url"].startswith("http://localhost:9000/culturalwalk/")


def test_intent_replay_after_complete_does_not_hand_out_a_writable_ticket(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """扫描开始后同一个幂等键不能再换一张可写 policy。

    否则 900 秒内可以按当前 object_key 再签一次，直接覆盖已过审的对象。
    """
    me = uuid.uuid4()
    key = str(uuid.uuid4())
    asset_id = _intent(client, me, key=key).json()["asset_id"]
    assert _complete(client, asset_id, me).status_code == 202

    replay = _intent(client, me, key=key)

    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "MEDIA_ALREADY_UPLOADED"
    with session_factory() as db:
        assert len(list(db.scalars(select(MediaAsset)))) == 1
    assert _actions(session_factory, asset_id) == ["media.intent.create", "media.complete"]


def test_intent_replay_after_expiry_asks_for_a_new_intent(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    me = uuid.uuid4()
    key = str(uuid.uuid4())
    asset_id = _intent(client, me, key=key).json()["asset_id"]
    with session_factory() as db:
        asset = db.get(MediaAsset, uuid.UUID(asset_id))
        asset.upload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    replay = _intent(client, me, key=key)

    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "MEDIA_UPLOAD_EXPIRED"


def test_mark_clean_rejects_asset_that_never_finished_upload(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """pending 直接 clean 会放行一个根本不存在于对象存储的文件。"""
    me = uuid.uuid4()
    asset_id = uuid.UUID(_intent(client, me).json()["asset_id"])

    with session_factory() as db:
        with pytest.raises(Conflict) as rejected:
            MediaService(db).mark_clean(asset_id)
        assert rejected.value.code == "MEDIA_NOT_UPLOADED"
        assert db.get(MediaAsset, asset_id).scan_state is ScanState.PENDING


def test_reference_gate_blocks_until_clean_and_publish_is_idempotent(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    me = uuid.uuid4()
    asset_id = uuid.UUID(_intent(client, me).json()["asset_id"])
    _complete(client, str(asset_id), me)

    with session_factory() as db:
        service = MediaService(db)
        with pytest.raises(Conflict) as blocked:
            service.require_referenceable(asset_id)
        assert blocked.value.code == "MEDIA_NOT_CLEAN"

        service.mark_clean(asset_id)
        assert service.require_referenceable(asset_id).id == asset_id
        service.publish(asset_id)
        service.publish(asset_id)
        db.commit()

    with session_factory() as db:
        asset = db.get(MediaAsset, asset_id)
        assert asset.published_at is not None
    assert _actions(session_factory, str(asset_id)) == [
        "media.intent.create",
        # 修掉「pending 可直接 clean」之后，引用闸门的用例必须先走一次 complete，
        # 审计里也就多出这一条；这不是放宽断言，是把真实状态机的中间步骤补回来。
        "media.complete",
        "media.scan.clean",
        "media.publish",
    ]


def test_intent_key_reuse_after_delete_is_conflict_not_500(client: TestClient) -> None:
    """软删不释放唯一约束：同键重放必须得到明确的 409，而不是 IntegrityError 的 500。"""
    me = uuid.uuid4()
    key = str(uuid.uuid4())
    first = _intent(client, me, key=key, role="editor")
    assert first.status_code == 201, first.text
    asset_id = first.json()["asset_id"]
    deleted = client.delete(f"{BASE}/{asset_id}", headers=_headers(me, "editor"))
    assert deleted.status_code == 204, deleted.text

    replay = _intent(client, me, key=key, role="editor")
    assert replay.status_code == 409, replay.text
    assert replay.json()["error"]["code"] == "MEDIA_INTENT_DELETED"
