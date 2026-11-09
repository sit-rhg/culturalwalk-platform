# 地点与文化资料 · HTTP 入口
"""只做编解码与依赖注入。业务规则一律写进 service.py。

契约以 docs/api/openapi.yaml 为准，本文件只负责把端点挂上去。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request

from app.core.db import DbDep
from app.core.deps import EditorDep
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT
from app.modules.catalog.schemas import (
    PlaceCreateIn,
    PlaceDetailOut,
    PlaceFactCreateIn,
    PlaceFactOut,
    PlaceListOut,
    PlaceOut,
)
from app.modules.catalog.service import CatalogService

router = APIRouter(prefix="/places", tags=["places"])

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=64, description="幂等键"),
]


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


@router.get("", summary="地点列表", response_model=PlaceListOut)
def list_places(
    db: DbDep,
    q: Annotated[str | None, Query(description="关键词，匹配名称/别名/简介")] = None,
    district: Annotated[str | None, Query(description="行政区，如「岳麓区」")] = None,
    tag: Annotated[str | None, Query(description="主题标签，如「红色」")] = None,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> PlaceListOut:
    items, next_cursor = CatalogService(db).list_places(
        q=q, district=district, tag=tag, cursor=cursor, limit=limit
    )
    return PlaceListOut(
        items=[PlaceOut.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


@router.post(
    "",
    response_model=PlaceOut,
    status_code=201,
    summary="新增地点（编辑）",
    responses={403: {"description": "需要编辑/管理员权限"}},
)
def create_place(
    payload: PlaceCreateIn,
    user: EditorDep,
    db: DbDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> PlaceOut:
    place = CatalogService(db).create_place(
        payload, actor=user, request_id=_request_id(request), idempotency_key=idempotency_key
    )
    return PlaceOut.model_validate(place)


@router.get("/{place_id}", summary="地点详情（含事实与来源）", response_model=PlaceDetailOut)
def get_place(place_id: uuid.UUID, db: DbDep) -> PlaceDetailOut:
    return CatalogService(db).get_place(place_id)


@router.post(
    "/{place_id}/facts",
    response_model=PlaceFactOut,
    status_code=201,
    summary="新增文化资料与来源（编辑）",
    responses={403: {"description": "需要编辑/管理员权限"}},
)
def create_place_fact(
    place_id: uuid.UUID,
    payload: PlaceFactCreateIn,
    user: EditorDep,
    db: DbDep,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> PlaceFactOut:
    return CatalogService(db).add_fact(
        place_id,
        payload,
        actor=user,
        request_id=_request_id(request),
        idempotency_key=idempotency_key,
    )
