# 社区互动 · DTO
"""请求/响应模型。字段与 docs/api/openapi.yaml 保持一致，改 yaml 再改这里。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 跨模块只借 DTO（社区读路线时要把地点详情与费用摘要原样嵌进响应）：
# 类型定义留在各自模块，改契约时只改一处，社区不用抄一份字段。
from app.modules.catalog.schemas import PlaceDetailOut
from app.modules.routes.schemas import Audience, CostSummaryOut


class AuthorOut(BaseModel):
    """评论/路线作者；账号被删时 username 为空，前端降级成「已注销用户」。"""

    id: uuid.UUID | None = None
    username: str | None = None


class RouteSummaryOut(BaseModel):
    """openapi.yaml#RouteSummary。"""

    id: uuid.UUID
    title: str
    summary: str | None = None
    cover_url: str | None = None
    # 数据模型里没有 channel 字段（routes 表尚未落地），当前全部按用户投稿返回 "user"。
    channel: Literal["official", "user"] = "user"
    author: AuthorOut
    theme_tags: list[str] = Field(default_factory=list)
    audience: list[Audience] = Field(default_factory=list)
    duration_minutes: int | None = None
    cost: CostSummaryOut
    comment_count: int = 0
    favorite_count: int = 0


class RouteDayItemOut(BaseModel):
    position: int
    start_time: str | None = None
    stay_minutes: int
    transport: str | None = None
    place: PlaceDetailOut
    price_min: int | None = None
    price_max: int | None = None
    # 快照里没有可信度字段（它挂在 place_facts 上），拿不到就不编：返回 null。
    confidence: Literal["verified", "likely", "unverified"] | None = None


class RouteDayOut(BaseModel):
    day_index: int
    items: list[RouteDayItemOut] = Field(default_factory=list)


class RouteDetailOut(RouteSummaryOut):
    """openapi.yaml#RouteDetail（RouteSummary + 版本与行程）。"""

    version_id: uuid.UUID
    content_hash: str
    published_at: datetime
    days: list[RouteDayOut] = Field(default_factory=list)
    is_favorited: bool = False


class RouteListOut(BaseModel):
    items: list[RouteSummaryOut]
    next_cursor: str | None = None


class RouteVersionOut(BaseModel):
    id: uuid.UUID
    version_no: int
    content_hash: str
    created_at: datetime
    created_by: uuid.UUID | None = None


class RouteVersionListOut(BaseModel):
    items: list[RouteVersionOut]


class CommentOut(BaseModel):
    """openapi.yaml#Comment。"""

    id: uuid.UUID
    route_id: uuid.UUID
    body: str
    pinned: bool = False
    author: AuthorOut
    created_at: datetime


class CommentListOut(BaseModel):
    items: list[CommentOut]
    next_cursor: str | None = None


class CommentCreate(BaseModel):
    """请求体：只收 body，多余字段直接 422（不静默忽略拼错的字段）。"""

    model_config = ConfigDict(extra="forbid")

    # 契约的 1..500 只是硬边界；「全为空白」在 service 里再做一次 strip 校验。
    body: str = Field(min_length=1, max_length=500)
