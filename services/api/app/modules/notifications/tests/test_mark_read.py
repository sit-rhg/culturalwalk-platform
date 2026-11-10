"""通知已读验收：204/200 幂等、只能本人、越权 404、审计。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.deps import create_access_token
from app.modules.audit.models import AuditLog
from app.modules.notifications.models import Notification

ENDPOINT = "/api/v1/notifications"


def _headers(user_id: uuid.UUID, *, with_key: bool = True) -> dict[str, str]:
    token = create_access_token(user_id=str(user_id), username="nt-reader", role="user")
    headers = {"Authorization": f"Bearer {token}"}
    if with_key:
        headers["Idempotency-Key"] = str(uuid.uuid4())
    return headers


def _seed(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> uuid.UUID:
    with session_factory() as db:
        row = Notification(
            user_id=user_id,
            type="review_approved",
            payload={"place": "岳麓书院"},
            created_at=datetime(2026, 10, 28, 8, 0, tzinfo=UTC),
        )
        db.add(row)
        db.commit()
        return row.id


def _audit_count(session_factory: sessionmaker[Session], notification_id: uuid.UUID) -> int:
    with session_factory() as db:
        rows = db.scalars(
            select(AuditLog).where(
                AuditLog.object_type == "notification",
                AuditLog.object_id == str(notification_id),
                AuditLog.action == "notification.read",
            )
        ).all()
        return len(list(rows))


def test_mark_read_sets_read_at_and_writes_audit(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    me = uuid.uuid4()
    notification_id = _seed(session_factory, me)

    response = client.post(f"{ENDPOINT}/{notification_id}/read", headers=_headers(me))

    assert response.status_code == 204
    assert response.content == b""
    with session_factory() as db:
        row = db.get(Notification, notification_id)
        assert row.read_at is not None
    assert _audit_count(session_factory, notification_id) == 1


def test_repeat_mark_read_is_idempotent(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    me = uuid.uuid4()
    notification_id = _seed(session_factory, me)

    marked = client.post(f"{ENDPOINT}/{notification_id}/read", headers=_headers(me))
    assert marked.status_code == 204
    first_read_at = client.get(ENDPOINT, headers=_headers(me, with_key=False)).json()["items"][0][
        "read_at"
    ]

    repeated = client.post(f"{ENDPOINT}/{notification_id}/read", headers=_headers(me))

    assert repeated.status_code == 204
    assert repeated.content == b""
    second_read_at = client.get(ENDPOINT, headers=_headers(me, with_key=False)).json()["items"][0][
        "read_at"
    ]
    assert second_read_at == first_read_at
    assert _audit_count(session_factory, notification_id) == 1


def test_cannot_mark_someone_elses_notification(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    owner, intruder = uuid.uuid4(), uuid.uuid4()
    notification_id = _seed(session_factory, owner)

    response = client.post(f"{ENDPOINT}/{notification_id}/read", headers=_headers(intruder))

    # 404 而不是 403：不泄露"这条通知存在"。
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOTIFICATION_NOT_FOUND"
    with session_factory() as db:
        assert db.get(Notification, notification_id).read_at is None

    missing = client.post(f"{ENDPOINT}/{uuid.uuid4()}/read", headers=_headers(owner))
    assert missing.status_code == 404


def test_mark_read_requires_login_and_idempotency_key(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    notification_id = _seed(session_factory, uuid.uuid4())

    assert client.post(f"{ENDPOINT}/{notification_id}/read").status_code == 401

    owner = uuid.uuid4()
    other_id = _seed(session_factory, owner)
    without_key = client.post(
        f"{ENDPOINT}/{other_id}/read", headers=_headers(owner, with_key=False)
    )
    assert without_key.status_code == 422
    assert without_key.json()["error"]["details"][0]["field"] == "Idempotency-Key"
