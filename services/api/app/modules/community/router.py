# 社区互动 · HTTP 入口
"""只做编解码与依赖注入。业务规则一律写进 service.py。

契约以 docs/api/openapi.yaml 为准，本文件只负责把端点挂上去。
两个前缀（/routes、/comments）合到一个模块 router 里：main.py 每个模块只 include 一次，
所以这里建子 router 再挂到顶层 router，别在 main.py 里加第二行。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Response, status
from pydantic import BaseModel

from app.core.db import DbDep
from app.core.deps import CurrentUser, CurrentUserDep, OptionalUserDep
from app.core.errors import Unauthorized
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT
from app.modules.community.schemas import (
    CommentCreate,
    CommentListOut,
    CommentOut,
    RouteDetailOut,
    RouteListOut,
    RouteVersionListOut,
)
from app.modules.community.service import CommunityService
from app.modules.routes.schemas import Audience

_routes_router = APIRouter(prefix="/routes", tags=["community"])
_comments_router = APIRouter(prefix="/comments", tags=["community"])

router = APIRouter(tags=["community"])
router.include_router(_routes_router)
router.include_router(_comments_router)

# 契约把 Idempotency-Key 标成必填；但本轮没有 idempotency 表去存 key→响应，
# 为了不把请求挡在 422 上，先做成可选接收、不做重放去重（见交付报告「存疑」）。
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        max_length=64,
        description="契约要求必填；当前实现接受但不按 key 重放（幂等由状态本身保证）",
    ),
]


def _user_id_of(user: CurrentUser | None) -> uuid.UUID | None:
    if user is None:
        return None
    try:
        return uuid.UUID(user.id)
    except ValueError as exc:  # token 里的 sub 不是 UUID，等同登录失效
        raise Unauthorized("登录已失效，请重新登录") from exc


# 公开读接口的缓存策略：内容随发布/收藏变化，60 秒足够兜住热点读。
_CACHE_CONTROL = "public, max-age=60"


def _payload_etag(payload: BaseModel) -> str:
    """列表响应的 ETag：内容变了才换标签，游标/条数不同也算不同内容。"""
    raw = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f'"{hashlib.sha256(raw.encode()).hexdigest()[:32]}"'


# ===== 公开路线浏览 =====


@_routes_router.get("", summary="公开路线列表", response_model=RouteListOut)
def list_routes(
    db: DbDep,
    response: Response,
    q: Annotated[str | None, Query(description="标题/摘要关键字")] = None,
    theme: Annotated[str | None, Query(description="文化主题标签")] = None,
    audience: Annotated[
        Audience | None, Query(description="适合人群，命中路线 audience 里包含该值即可")
    ] = None,
    city: Annotated[str | None, Query(description="扩展参数：当前只收录长沙")] = None,
    duration: Annotated[str | None, Query(description="时长区间，例如 0-120")] = None,
    budget_max: Annotated[int | None, Query(ge=0, description="分，人均预算上限")] = None,
    channel: Annotated[Literal["official", "user"] | None, Query()] = None,
    sort: Annotated[
        Literal["hot", "latest", "oldest"],
        Query(description="默认 hot：收藏数倒序（同数看发布时间）；latest/oldest 按发布时间"),
    ] = "hot",
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> RouteListOut | Response:
    items, next_cursor = CommunityService(db).list_routes(
        q=q,
        theme=theme,
        audience=audience.value if audience else None,
        city=city,
        duration=duration,
        budget_max=budget_max,
        channel=channel,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )
    payload = RouteListOut(items=items, next_cursor=next_cursor)
    etag = _payload_etag(payload)
    headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
    if if_none_match is not None and if_none_match.strip() in {etag, etag.strip('"')}:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response.headers.update(headers)
    return payload


@_routes_router.get("/{route_id}", summary="公开路线详情", response_model=RouteDetailOut)
def get_route(
    route_id: uuid.UUID,
    response: Response,
    db: DbDep,
    viewer: OptionalUserDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> RouteDetailOut | Response:
    detail = CommunityService(db).get_route(route_id=route_id, viewer_id=_user_id_of(viewer))
    etag = f'"{detail.content_hash}"'
    headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
    if if_none_match is not None and if_none_match.strip() in {etag, detail.content_hash}:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response.headers.update(headers)
    return detail


@_routes_router.get(
    "/{route_id}/versions",
    summary="版本历史（登录可见）",
    response_model=RouteVersionListOut,
)
def list_versions(
    route_id: uuid.UUID,
    # 契约没有 security: []，所以继承全局 bearer 认证；未登录 401，未发布仍 404。
    _user: CurrentUserDep,
    db: DbDep,
) -> RouteVersionListOut:
    return RouteVersionListOut(items=CommunityService(db).list_versions(route_id=route_id))


# ===== 评论 =====


@_routes_router.get(
    "/{route_id}/comments",
    summary="评论列表（公开，置顶优先）",
    response_model=CommentListOut,
)
def list_comments(
    route_id: uuid.UUID,
    db: DbDep,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> CommentListOut:
    return CommunityService(db).list_comments(route_id=route_id, cursor=cursor, limit=limit)


@_routes_router.post(
    "/{route_id}/comments",
    summary="发表评论",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
)
def create_comment(
    route_id: uuid.UUID,
    payload: CommentCreate,
    user: CurrentUserDep,
    db: DbDep,
    idempotency_key: IdempotencyKey = None,
) -> CommentOut:
    return CommunityService(db).create_comment(route_id=route_id, body=payload.body, user=user)


@_comments_router.post("/{comment_id}/pin", summary="作者置顶评论", response_model=CommentOut)
def pin_comment(
    comment_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbDep,
    idempotency_key: IdempotencyKey = None,
) -> CommentOut:
    return CommunityService(db).pin_comment(comment_id=comment_id, user=user)


# ===== 收藏 =====


@_routes_router.post(
    "/{route_id}/favorite",
    summary="收藏（幂等）",
    status_code=status.HTTP_204_NO_CONTENT,
)
def add_favorite(
    route_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    CommunityService(db).add_favorite(route_id=route_id, user=user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@_routes_router.delete(
    "/{route_id}/favorite",
    summary="取消收藏（幂等）",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_favorite(
    route_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbDep,
) -> Response:
    CommunityService(db).remove_favorite(route_id=route_id, user=user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
