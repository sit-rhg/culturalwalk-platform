# 媒体资源 · HTTP 入口
"""只做编解码与依赖注入。业务规则一律写进 service.py。

契约以 docs/api/openapi.yaml 为准，本文件只负责把端点挂上去。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status

from app.adapters.storage import StorageAdapter, get_storage
from app.core.db import DbDep
from app.core.deps import CurrentUserDep
from app.core.errors import NotFound
from app.modules.media.models import MediaZone, ScanState
from app.modules.media.repository import MediaRepository
from app.modules.media.schemas import MediaAssetOut, MediaIntentIn, MediaIntentOut
from app.modules.media.service import MediaService

router = APIRouter(prefix="/media", tags=["media"])

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=64,
        description="同一请求重试时带上同一个 key，服务端返回首次结果",
    ),
]

StorageDep = Annotated[StorageAdapter, Depends(get_storage)]


@router.post("/intent", status_code=status.HTTP_201_CREATED, response_model=MediaIntentOut)
def create_intent(
    payload: MediaIntentIn,
    db: DbDep,
    storage: StorageDep,
    user: CurrentUserDep,
    idempotency_key: IdempotencyKey,
) -> MediaIntentOut:
    """申请直传：返回只对这一条资产有效的签名 URL。"""
    return MediaService(db, storage).create_intent(
        owner_id=uuid.UUID(user.id), payload=payload, idempotency_key=idempotency_key
    )


@router.post(
    "/{media_id}/complete", status_code=status.HTTP_202_ACCEPTED, response_model=MediaAssetOut
)
def complete_upload(
    media_id: uuid.UUID,
    db: DbDep,
    storage: StorageDep,
    user: CurrentUserDep,
    idempotency_key: IdempotencyKey,
) -> MediaAssetOut:
    """确认上传：校验真实大小与魔数，通过后进入扫描队列。"""
    return MediaService(db, storage).complete(
        owner_id=uuid.UUID(user.id), asset_id=media_id, idempotency_key=idempotency_key
    )


@router.get("/{media_id}", response_model=MediaAssetOut)
def get_asset(
    media_id: uuid.UUID, db: DbDep, storage: StorageDep, user: CurrentUserDep
) -> MediaAssetOut:
    """只有本人能查；别人的资产按不存在处理。"""
    return MediaService(db, storage).get(owner_id=uuid.UUID(user.id), asset_id=media_id)


@router.get("/{media_id}/raw", include_in_schema=False)
def read_raw(media_id: uuid.UUID, db: DbDep, storage: StorageDep) -> Response:
    """清洁区对象对外可读；隔离区的对象一律 404，不泄露存在性。"""
    asset = MediaRepository(db).get(media_id)
    if (
        asset is None
        or asset.zone is not MediaZone.CLEAN
        or asset.scan_state is not ScanState.CLEAN
    ):
        raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
    data = storage.get(asset.object_key)
    if data is None:
        raise NotFound("媒体不存在", code="MEDIA_NOT_FOUND")
    return Response(
        content=data,
        media_type=asset.mime,
        headers={"Cache-Control": "public, max-age=60"},
    )
