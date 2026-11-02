"""ME-02 扫描与缩略图：先写用例，实现前应当全红。

卡面：worker 校验 → 扫描 → 重编码 → 缩略图；非法 MIME 拒绝、EXIF 被清除。
规范：docs/CulturalWalk_VibeCoding底层架构与开发范式.md §11.1.2 第 3 步——
扫描要做「实际格式/解码边界、重编码、EXIF/GPS 清理」，失败不得转 CLEAN。
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.adapters.storage import LocalStorageAdapter, get_storage
from app.main import app
from app.modules.media.models import MediaZone, ScanState

PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")


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
    return f"media-test-{raw}"


def _jpeg_with_gps(width: int = 64, height: int = 48) -> bytes:
    """造一张带 EXIF（含 GPS）的 JPEG，用来验证重编码确实清掉了元数据。"""
    img = Image.new("RGB", (width, height), color=(32, 96, 160))
    exif = Image.Exif()
    exif[0x0112] = 1  # Orientation
    gps = exif.get_ifd(0x8825)  # GPSInfo：写进 IFD 才会被序列化
    gps[1] = "N"
    gps[2] = (28.0, 11.0, 0.0)  # 纬度
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _stage_asset(
    db_session, storage: LocalStorageAdapter, client: TestClient, headers, data: bytes, mime: str
) -> uuid.UUID:
    """走真实接口：intent → 直传 → complete，把资产推进到 SCANNING。"""
    intent = client.post(
        "/api/v1/media/intent",
        json={"filename": "photo.jpg", "mime": mime, "size": len(data)},
        headers={**headers, "Idempotency-Key": _k(f"intent-{uuid.uuid4().hex}")},
    )
    assert intent.status_code == 201, intent.text
    asset_id = intent.json()["asset_id"]
    storage.put(f"quarantine/{asset_id}", data)
    done = client.post(
        f"/api/v1/media/{asset_id}/complete",
        headers={**headers, "Idempotency-Key": _k(f"done-{uuid.uuid4().hex}")},
    )
    assert done.status_code == 202, done.text
    return uuid.UUID(asset_id)


def test_scan_clean_strips_exif_and_sets_dimensions(
    db_session, storage: LocalStorageAdapter, client: TestClient, user_headers
) -> None:
    from app.modules.media.service import MediaService

    data = _jpeg_with_gps()
    assert Image.open(io.BytesIO(data)).getexif(), "夹具本身应带 EXIF"

    asset_id = _stage_asset(db_session, storage, client, user_headers, data, "image/jpeg")

    asset = MediaService(db_session, storage).scan(asset_id)
    db_session.commit()

    assert asset.zone is MediaZone.CLEAN
    assert asset.scan_state is ScanState.CLEAN
    assert (asset.width, asset.height) == (64, 48)

    stored = storage.get(f"clean/{asset_id}")
    assert stored, "重编码后的对象应落在 clean 前缀"
    assert not storage.head(f"quarantine/{asset_id}"), "隔离区原对象应被清走"

    rewritten = Image.open(io.BytesIO(stored))
    assert rewritten.format == "JPEG"
    assert not dict(rewritten.getexif()), "EXIF 必须被清除"
    assert not rewritten.getexif().get_ifd(0x8825), "GPS 信息必须被清除"

    thumb = storage.get(f"clean/{asset_id}.thumb.webp")
    assert thumb, "应生成缩略图"
    thumb_img = Image.open(io.BytesIO(thumb))
    assert thumb_img.format == "WEBP"
    assert max(thumb_img.size) <= 320


def test_scan_rejects_undecodable_payload(
    db_session, storage: LocalStorageAdapter, client: TestClient, user_headers
) -> None:
    from app.modules.media.service import MediaService

    # 魔数像 PNG，但内容解不出来：complete 放行，扫描必须拒绝。
    asset_id = _stage_asset(
        db_session,
        storage,
        client,
        user_headers,
        PNG_SIGNATURE + b"not really an image",
        "image/png",
    )

    asset = MediaService(db_session, storage).scan(asset_id)
    db_session.commit()

    assert asset.scan_state is ScanState.REJECTED
    assert asset.zone is MediaZone.QUARANTINE
    assert not storage.head(f"clean/{asset_id}"), "被拒绝的资产不得进 clean 区"
    assert not storage.head(f"clean/{asset_id}.thumb.webp")


def test_scan_is_idempotent(
    db_session, storage: LocalStorageAdapter, client: TestClient, user_headers
) -> None:
    from app.modules.media.service import MediaService

    asset_id = _stage_asset(
        db_session, storage, client, user_headers, _jpeg_with_gps(), "image/jpeg"
    )
    service = MediaService(db_session, storage)

    first = service.scan(asset_id)
    db_session.commit()
    thumb_first = storage.get(f"clean/{asset_id}.thumb.webp")

    second = service.scan(asset_id)
    db_session.commit()

    assert second.id == first.id
    assert second.scan_state is ScanState.CLEAN
    assert storage.get(f"clean/{asset_id}.thumb.webp") == thumb_first, "重复扫描不应重写缩略图"


def test_scan_pending_batch_marks_results(
    db_session, storage: LocalStorageAdapter, client: TestClient, user_headers
) -> None:
    from app.modules.media.service import MediaService

    ok_id = _stage_asset(db_session, storage, client, user_headers, _jpeg_with_gps(), "image/jpeg")
    bad_id = _stage_asset(
        db_session, storage, client, user_headers, PNG_SIGNATURE + b"broken", "image/png"
    )

    results = MediaService(db_session, storage).scan_pending(limit=10)
    db_session.commit()

    by_id = {asset.id: asset for asset in results}
    assert by_id[ok_id].scan_state is ScanState.CLEAN
    assert by_id[bad_id].scan_state is ScanState.REJECTED
