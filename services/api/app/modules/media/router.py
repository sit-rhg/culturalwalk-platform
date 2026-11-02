# 媒体资源 · HTTP 入口
"""只做编解码与依赖注入。业务规则一律写进 service.py。

契约以 docs/api/openapi.yaml 为准，本文件只负责把端点挂上去。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Request

from app.core.db import DbDep
from app.core.deps import CurrentUserDep
from app.modules.media.schemas import MediaAssetOut, MediaIntentIn, MediaIntentOut
from app.modules.media.service import MediaService

router = APIRouter(prefix="/media", tags=["media"])

# 契约参数定义是 format: uuid；既有模块（routes/planner）实际用字符串长度校验，
# 这里跟随既有实现，避免同一套接口出现两种校验口味。
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=64, description="幂等键"),
]


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


@router.post(
    "/intent",
    response_model=MediaIntentOut,
    status_code=201,
    summary="申请上传（返回预签名地址）",
)
def create_media_intent(
    payload: MediaIntentIn,
    user: CurrentUserDep,
    db: DbDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MediaIntentOut:
    return MediaService(db).create_intent(
        actor=user,
        payload=payload,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
    )


@router.post(
    "/{media_id}/complete",
    response_model=MediaAssetOut,
    status_code=202,
    summary="确认上传完成，进入扫描",
)
def complete_media_upload(
    media_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MediaAssetOut:
    # Idempotency-Key 是契约必填；状态机本身可重放（重复 complete 不改变状态）。
    return MediaService(db).complete(media_id=media_id, actor=user, request_id=_request_id(request))


@router.get(
    "/{media_id}",
    response_model=MediaAssetOut,
    summary="媒体状态（clean 后才有公开 URL）",
)
def get_media(media_id: uuid.UUID, user: CurrentUserDep, db: DbDep) -> MediaAssetOut:
    return MediaService(db).get_for_viewer(media_id=media_id, actor=user)
