# 路线与版本 · HTTP 入口
"""只做编解码与依赖注入。业务规则一律写进 service.py。

契约以 docs/api/openapi.yaml 为准，本文件只负责把端点挂上去。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.core.db import DbDep
from app.core.deps import CurrentUser, CurrentUserDep
from app.core.errors import Unauthorized
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT
from app.modules.routes.schemas import (
    RouteDraftCreate,
    RouteDraftDetailOut,
    RouteDraftListOut,
    RouteDraftOut,
    RouteDraftPatch,
    RouteNodesReplace,
)
from app.modules.routes.service import RoutesService

router = APIRouter(prefix="/route-drafts", tags=["drafts"])

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=64,
        description="同一请求重试时带上同一个 key，服务端返回首次结果",
    ),
]


def _user_id_of(user: CurrentUser) -> uuid.UUID:
    try:
        return uuid.UUID(user.id)
    except ValueError as exc:  # token 里的 sub 不是 UUID，等同登录失效
        raise Unauthorized("登录已失效，请重新登录") from exc


@router.get("", summary="我的草稿列表", response_model=RouteDraftListOut)
def list_drafts(
    user: CurrentUserDep,
    db: DbDep,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> RouteDraftListOut:
    items, next_cursor = RoutesService(db).list_mine(
        owner_id=_user_id_of(user), cursor=cursor, limit=limit
    )
    return RouteDraftListOut(
        items=[RouteDraftOut.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


@router.post(
    "",
    summary="新建草稿",
    response_model=RouteDraftOut,
    status_code=status.HTTP_201_CREATED,
)
def create_draft(
    payload: RouteDraftCreate,
    user: CurrentUserDep,
    db: DbDep,
    idempotency_key: IdempotencyKey,
) -> RouteDraftOut:
    draft = RoutesService(db).create(
        owner_id=_user_id_of(user),
        payload=payload,
        idempotency_key=idempotency_key,
    )
    return RouteDraftOut.model_validate(draft)


@router.get(
    "/{draft_id}",
    summary="草稿详情",
    response_model=RouteDraftDetailOut,
)
def get_draft(draft_id: uuid.UUID, user: CurrentUserDep, db: DbDep) -> RouteDraftDetailOut:
    return RoutesService(db).get_detail(draft_id=draft_id, owner_id=_user_id_of(user))


@router.patch(
    "/{draft_id}",
    summary="更新草稿",
    response_model=RouteDraftOut,
)
def patch_draft(
    draft_id: uuid.UUID,
    payload: RouteDraftPatch,
    user: CurrentUserDep,
    db: DbDep,
    if_match: Annotated[int, Header(alias="If-Match", description="草稿的 lock_version")],
    idempotency_key: IdempotencyKey,
) -> RouteDraftOut:
    draft = RoutesService(db).patch(
        draft_id=draft_id,
        owner_id=_user_id_of(user),
        payload=payload,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )
    return RouteDraftOut.model_validate(draft)


@router.delete(
    "/{draft_id}",
    summary="删除草稿（仅 EDITING 且从未发布）",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_draft(draft_id: uuid.UUID, user: CurrentUserDep, db: DbDep) -> Response:
    RoutesService(db).delete(draft_id=draft_id, owner_id=_user_id_of(user))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{draft_id}/nodes",
    summary="整表替换草稿节点（顺序=数组顺序）",
    response_model=RouteDraftDetailOut,
)
def replace_nodes(
    draft_id: uuid.UUID,
    payload: RouteNodesReplace,
    user: CurrentUserDep,
    db: DbDep,
    if_match: Annotated[int, Header(alias="If-Match", description="草稿的 lock_version")],
    idempotency_key: IdempotencyKey,
) -> RouteDraftDetailOut:
    return RoutesService(db).replace_nodes(
        draft_id=draft_id,
        owner_id=_user_id_of(user),
        payload=payload,
        if_match=if_match,
        idempotency_key=idempotency_key,
    )
