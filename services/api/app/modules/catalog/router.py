# 地点与文化资料 · HTTP 入口
"""只做编解码与依赖注入。业务规则一律写进 service.py。

契约以 docs/api/openapi.yaml 为准，本文件只负责把端点挂上去。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.db import DbDep
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT
from app.modules.catalog.schemas import PlaceDetailOut, PlaceListOut, PlaceOut
from app.modules.catalog.service import CatalogService

router = APIRouter(prefix="/places", tags=["places"])


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


@router.get("/{place_id}", summary="地点详情（含事实与来源）", response_model=PlaceDetailOut)
def get_place(place_id: uuid.UUID, db: DbDep) -> PlaceDetailOut:
    return CatalogService(db).get_place(place_id)


# ===== 待实现端点（认领任务卡后逐条填） =====
#   POST   /places                                          新增地点（编辑）
#   POST   /places/{place_id}/facts                         新增事实与来源（编辑）
