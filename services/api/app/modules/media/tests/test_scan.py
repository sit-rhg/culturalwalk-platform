"""扫描流水线测试：真图解码/重编码/EXIF 清理/缩略图，对象存储落临时目录。

这里不打桩 Pillow：扫描的要点正是「真实字节长什么样」，假图测不出 EXIF 是否被清掉。
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from app.modules.media.adapters.objects import LocalObjectStore
from app.modules.media.models import MediaAsset, MediaZone, ScanState, utcnow
from app.modules.media.scan import ScanRejected, scan_image
from app.modules.media.service import MediaService

OWNER = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "media")


def _jpeg_with_gps(size: tuple[int, int] = (640, 480)) -> bytes:
    image = Image.new("RGB", size, (200, 40, 40))
    exif = Image.Exif()
    exif[0x0112] = 1
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (28.0, 11.0, 15.0)
    gps[3] = "E"
    gps[4] = (112.0, 57.0, 3.0)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _stage(
    session: Session,
    store: LocalObjectStore,
    raw: bytes,
    *,
    mime: str = "image/jpeg",
    store_bytes: bool = True,
) -> MediaAsset:
    asset_id = uuid.uuid4()
    key = f"quarantine/{OWNER}/{asset_id}/photo.jpg"
    if store_bytes:
        store.put(key, raw)
    asset = MediaAsset(
        id=asset_id,
        owner_id=OWNER,
        object_key=key,
        filename="photo.jpg",
        mime=mime,
        size=len(raw),
        zone=MediaZone.QUARANTINE,
        scan_state=ScanState.SCANNING,
        upload_expires_at=utcnow(),
        completed_at=utcnow(),
    )
    session.add(asset)
    session.commit()
    return asset


def test_scan_clean_strips_exif_and_writes_thumbnail(
    session_factory: sessionmaker[Session], store: LocalObjectStore
) -> None:
    with session_factory() as session:
        asset = _stage(session, store, _jpeg_with_gps())
        original_key = asset.object_key

        out = MediaService(session).scan_asset(asset.id, store=store)
        session.commit()

        assert out.zone == "clean"
        assert out.scan_state == "clean"
        assert out.width == 640
        assert out.height == 480
        assert out.url is not None and "/clean/" in out.url

        assert not store.exists(original_key)
        clean_key = next(iter(_keys(store, "clean")))
        assert store.exists(f"{clean_key}.thumb.webp")

        with Image.open(io.BytesIO(store.get(clean_key))) as cleaned:
            # 重编码之后 EXIF（含 GPS）必须为空：原图里有 28°N/112°E。
            assert len(cleaned.getexif()) == 0


def test_scan_rejects_undecodable_bytes(
    session_factory: sessionmaker[Session], store: LocalObjectStore
) -> None:
    with session_factory() as session:
        asset = _stage(session, store, b"not-an-image")
        out = MediaService(session).scan_asset(asset.id, store=store)
        session.commit()

        assert out.scan_state == "rejected"
        assert out.zone == "quarantine"
        assert list(_keys(store, "clean")) == []


def test_declared_mime_mismatch_is_rejected(
    session_factory: sessionmaker[Session], store: LocalObjectStore
) -> None:
    with session_factory() as session:
        asset = _stage(session, store, _jpeg_with_gps(), mime="image/png")
        out = MediaService(session).scan_asset(asset.id, store=store)
        assert out.scan_state == "rejected"


def test_rescan_is_idempotent(
    session_factory: sessionmaker[Session], store: LocalObjectStore
) -> None:
    with session_factory() as session:
        asset = _stage(session, store, _jpeg_with_gps())
        service = MediaService(session)
        first = service.scan_asset(asset.id, store=store)
        session.commit()
        clean_key = next(iter(_keys(store, "clean")))
        thumb = store.get(f"{clean_key}.thumb.webp")

        second = service.scan_asset(asset.id, store=store)
        session.commit()

        assert second.scan_state == first.scan_state == "clean"
        assert second.url == first.url
        assert store.get(f"{clean_key}.thumb.webp") == thumb


def test_missing_object_is_rejected_so_the_queue_keeps_moving(
    session_factory: sessionmaker[Session], store: LocalObjectStore
) -> None:
    """对象丢了要判拒绝，不能停在 SCANNING。

    旧行为是抛 409 让调用方重试，但队列按 created_at 升序取前 N 条：
    一条永远扫不动的资产会把队头堵死，后面真正传完的文件永远轮不到。
    """
    with session_factory() as session:
        asset = _stage(session, store, b"", store_bytes=False)
        out = MediaService(session).scan_asset(asset.id, store=store)
        session.commit()

        assert out.scan_state == "rejected"
        refreshed = session.get(MediaAsset, asset.id)
        assert refreshed is not None
        assert refreshed.scan_state is ScanState.REJECTED


def test_missing_object_does_not_block_later_assets(
    session_factory: sessionmaker[Session], store: LocalObjectStore
) -> None:
    with session_factory() as session:
        _stage(session, store, b"", store_bytes=False)
        _stage(session, store, _jpeg_with_gps())
        service = MediaService(session)

        counts = service.scan_pending(store=store, limit=10)
        session.commit()

        assert counts == {"scanned": 2, "clean": 1, "rejected": 1, "skipped": 0}
        assert service.repo.list_scanning(10) == []


def test_scan_pending_mixes_clean_and_rejected(
    session_factory: sessionmaker[Session], store: LocalObjectStore
) -> None:
    with session_factory() as session:
        good = _stage(session, store, _jpeg_with_gps())
        bad = _stage(session, store, b"broken")
        counts = MediaService(session).scan_pending(store=store, limit=10)
        session.commit()

        assert counts == {"scanned": 2, "clean": 1, "rejected": 1, "skipped": 0}
        good_asset = session.get(MediaAsset, good.id)
        bad_asset = session.get(MediaAsset, bad.id)
        assert good_asset is not None and bad_asset is not None
        assert good_asset.scan_state is ScanState.CLEAN
        assert bad_asset.scan_state is ScanState.REJECTED


def test_scan_image_rejects_empty_and_unsupported() -> None:
    with pytest.raises(ScanRejected) as empty:
        scan_image(b"", "image/jpeg")
    assert empty.value.code == "MEDIA_EMPTY"

    gif = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 0, 0)).save(gif, format="GIF")
    with pytest.raises(ScanRejected) as unsupported:
        scan_image(gif.getvalue(), "image/gif")
    assert unsupported.value.code == "MEDIA_FORMAT_UNSUPPORTED"


def _keys(store: LocalObjectStore, prefix: str) -> Iterator[str]:
    root = store.root
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            key = path.relative_to(root).as_posix()
            if key.startswith(prefix + "/"):
                yield key


def _jpeg_with_orientation(orientation: int = 6, size: tuple[int, int] = (640, 480)) -> bytes:
    """orientation=6 是「显示时顺时针转 90°」：文件里存 640×480，摆正后应为 480×640。"""
    image = Image.new("RGB", size, (200, 40, 40))
    exif = Image.Exif()
    exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def test_scan_applies_exif_orientation(
    session_factory: sessionmaker[Session], store: LocalObjectStore
) -> None:
    """竖拍照片不能横躺：重编码丢掉 EXIF 之前必须先把像素摆正。"""
    with session_factory() as session:
        asset = _stage(session, store, _jpeg_with_orientation(6))

        out = MediaService(session).scan_asset(asset.id, store=store)
        session.commit()

        assert out.scan_state == "clean"
        assert (out.width, out.height) == (480, 640)
        clean_key = next(iter(_keys(store, "clean")))
        with Image.open(io.BytesIO(store.get(clean_key))) as cleaned:
            assert cleaned.size == (480, 640)
            assert len(cleaned.getexif()) == 0
