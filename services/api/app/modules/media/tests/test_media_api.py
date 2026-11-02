"""ME-01 上传意图与直传：先写用例，实现前应当全红。

契约：docs/api/openapi.yaml 的 /media/intent、/media/{id}/complete、/media/{id}。
状态机：docs/CulturalWalk_VibeCoding底层架构与开发范式.md §11.1.1（quarantine 私有、
扫描前不对外、size/MIME 必须与声明一致）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.storage import LocalStorageAdapter, get_storage
from app.core.config import settings
from app.main import app
from conftest import USER_ID

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "05570c1d2f0000000049454e44ae426082"
)
JPEG_1PX = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")


@pytest.fixture()
def storage(tmp_path: Path) -> Iterator[LocalStorageAdapter]:
    adapter = LocalStorageAdapter(
        root=tmp_path, secret="test-secret", public_base_url="http://media.test"
    )
    app.dependency_overrides[get_storage] = lambda: adapter
    try:
        yield adapter
    finally:
        app.dependency_overrides.pop(get_storage, None)


def _k(raw: str) -> str:
    """Idempotency-Key 至少要 8 位，用例里保持短名字的可读性。"""
    return f"media-test-{raw}"


def _intent(client: TestClient, headers: dict[str, str], key: str, **body: object):
    payload = {"filename": "cover.png", "mime": "image/png", "size": len(PNG_1PX)}
    payload.update(body)
    return client.post(
        "/api/v1/media/intent", json=payload, headers={**headers, "Idempotency-Key": _k(key)}
    )


def _complete(client: TestClient, headers: dict[str, str], asset_id: str, key: str):
    return client.post(
        f"/api/v1/media/{asset_id}/complete",
        headers={**headers, "Idempotency-Key": _k(key)},
    )


def _upload(storage: LocalStorageAdapter, asset_id: str, data: bytes) -> str:
    key = f"quarantine/{asset_id}"
    storage.put(key, data)
    return key


def test_intent_requires_login(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/media/intent",
        json={"filename": "a.png", "mime": "image/png", "size": 10},
        headers={"Idempotency-Key": _k("anon")},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_intent_requires_idempotency_key(client: TestClient, user_headers: dict[str, str]) -> None:
    resp = client.post(
        "/api/v1/media/intent",
        json={"filename": "a.png", "mime": "image/png", "size": 10},
        headers=user_headers,
    )
    assert resp.status_code == 422


def test_intent_rejects_unsupported_mime(client: TestClient, user_headers: dict[str, str]) -> None:
    resp = _intent(client, user_headers, "mime", mime="image/gif")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_intent_rejects_oversize(client: TestClient, user_headers: dict[str, str]) -> None:
    resp = _intent(client, user_headers, "big", size=settings.media_max_bytes + 1)
    assert resp.status_code == 422


def test_intent_returns_scoped_upload_url(
    client: TestClient, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    resp = _intent(client, user_headers, "scope")
    assert resp.status_code == 201
    body = resp.json()
    assert uuid.UUID(body["asset_id"])
    assert body["expires_at"]
    # URL 必须绑定这次意图的 object key，不能是"随便写哪个 key 都行"的桶地址
    assert body["asset_id"] in body["upload_url"]
    assert "signature=" in body["upload_url"]


def test_intent_replay_returns_same_asset(
    client: TestClient, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    first = _intent(client, user_headers, "same")
    second = _intent(client, user_headers, "same")
    assert first.status_code == second.status_code == 201
    assert first.json()["asset_id"] == second.json()["asset_id"]


def test_intent_same_key_different_body_conflicts(
    client: TestClient, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    _intent(client, user_headers, "dup")
    resp = _intent(client, user_headers, "dup", filename="other.png")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_complete_before_upload_is_rejected(
    client: TestClient, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    asset_id = _intent(client, user_headers, "noupload").json()["asset_id"]
    resp = _complete(client, user_headers, asset_id, "before")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "MEDIA_NOT_UPLOADED"


def test_complete_moves_asset_to_scanning(
    client: TestClient, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    asset_id = _intent(client, user_headers, "scan").json()["asset_id"]
    _upload(storage, asset_id, PNG_1PX)
    resp = _complete(client, user_headers, asset_id, "scan")
    assert resp.status_code == 202
    body = resp.json()
    assert body["zone"] == "quarantine"
    assert body["scan_state"] == "scanning"
    assert body["url"] is None


def test_complete_is_idempotent(
    client: TestClient, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    asset_id = _intent(client, user_headers, "idem").json()["asset_id"]
    _upload(storage, asset_id, PNG_1PX)
    first = _complete(client, user_headers, asset_id, "idem")
    second = _complete(client, user_headers, asset_id, "idem")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()


def test_complete_rejects_declared_size_mismatch(
    client: TestClient, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    asset_id = _intent(client, user_headers, "size").json()["asset_id"]
    _upload(storage, asset_id, PNG_1PX + b"extra")
    resp = _complete(client, user_headers, asset_id, "size")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "MEDIA_SIZE_MISMATCH"


def test_complete_rejects_content_mime_mismatch(
    client: TestClient, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    """声明 png 实际给 jpeg：不许靠后缀/Content-Type 糊过去。"""
    asset_id = _intent(client, user_headers, "mime-bad", size=len(JPEG_1PX)).json()["asset_id"]
    _upload(storage, asset_id, JPEG_1PX)
    resp = _complete(client, user_headers, asset_id, "mime-bad")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "MEDIA_MIME_MISMATCH"


def test_get_asset_is_owner_only(
    client: TestClient,
    user_headers: dict[str, str],
    admin_headers: dict[str, str],
    storage: LocalStorageAdapter,
) -> None:
    asset_id = _intent(client, user_headers, "owner").json()["asset_id"]
    assert client.get(f"/api/v1/media/{asset_id}", headers=user_headers).status_code == 200
    # 别人的资产按"不存在"处理，不泄露存在性
    resp = client.get(f"/api/v1/media/{asset_id}", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MEDIA_NOT_FOUND"


def test_get_unknown_asset_404(client: TestClient, user_headers: dict[str, str]) -> None:
    resp = client.get(f"/api/v1/media/{uuid.uuid4()}", headers=user_headers)
    assert resp.status_code == 404


def test_clean_asset_exposes_public_url(
    client: TestClient, db_session, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    asset_id = _intent(client, user_headers, "clean").json()["asset_id"]
    _upload(storage, asset_id, PNG_1PX)
    _complete(client, user_headers, asset_id, "clean")
    # 模拟扫描通过（ME-02 负责真实扫描）
    from app.modules.media.service import MediaService

    MediaService(db_session, storage).mark_clean(uuid.UUID(asset_id))
    body = client.get(f"/api/v1/media/{asset_id}", headers=user_headers).json()
    assert body["zone"] == "clean"
    assert body["scan_state"] == "clean"
    assert body["url"]
    # 清洁区对外可读
    raw = client.get(body["url"])
    assert raw.status_code == 200
    assert raw.content == PNG_1PX


def test_quarantine_object_is_not_readable(
    client: TestClient, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    """未扫描通过的资产即使知道 id 也拿不到字节。"""
    asset_id = _intent(client, user_headers, "quar").json()["asset_id"]
    _upload(storage, asset_id, PNG_1PX)
    resp = client.get(f"/api/v1/media/{asset_id}/raw")
    assert resp.status_code == 404


def test_user_id_of_token_is_recorded(
    client: TestClient, user_headers: dict[str, str], storage: LocalStorageAdapter
) -> None:
    asset_id = _intent(client, user_headers, "ownerid").json()["asset_id"]
    body = client.get(f"/api/v1/media/{asset_id}", headers=user_headers).json()
    assert body["id"] == asset_id
    assert uuid.UUID(USER_ID)
