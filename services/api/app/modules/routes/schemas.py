# 路线与版本 · DTO
"""请求/响应模型。字段与 docs/api/openapi.yaml 保持一致，改 yaml 再改这里。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.routes.models import DraftState


class RouteDraftCreate(BaseModel):
    """POST /route-drafts 的请求体（契约 RouteDraftCreate）。

    extra="forbid"：字段名拼错要直接报错，而不是悄悄忽略后建出一份少字段的草稿。
    title 额外要求非空：契约只写了 maxLength，但空标题的草稿在列表里无法辨认。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=60)
    summary: str | None = Field(default=None, max_length=300)
    theme_tags: list[str] = Field(default_factory=list)
    duration_minutes: int | None = None


class RouteDraftPatch(BaseModel):
    """PATCH /route-drafts/{id} 的请求体（契约 RouteDraftPatch，全部可选）。

    只改带上来的字段：`exclude_unset=True` 才能区分「没传」和「显式传 null」，
    否则前端只改标题也会把简介、主题一起抹掉。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=60)
    summary: str | None = Field(default=None, max_length=300)
    theme_tags: list[str] | None = None
    duration_minutes: int | None = None

    @model_validator(mode="after")
    def _reject_explicit_null(self) -> RouteDraftPatch:
        # summary / duration_minutes 可以显式清空，title / theme_tags 不行：
        # 一份没有标题或没有主题列表的草稿没有意义。
        for name in ("title", "theme_tags"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} 不能为 null")
        return self


class RouteDraftOut(BaseModel):
    """契约 RouteDraft：列表与创建/更新都返回这个形状。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    route_id: uuid.UUID | None = None
    title: str
    summary: str | None = None
    state: DraftState
    lock_version: int
    base_version_id: uuid.UUID | None = None
    updated_at: datetime

    @field_validator("updated_at", mode="before")
    @classmethod
    def _assume_utc(cls, value: object) -> object:
        # SQLite 不存时区，读出来是 naive；契约要求 date-time，输出前补成 UTC，
        # 否则前端在不同时区会各自按本地时间解释同一个时间戳。
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class RouteDraftListOut(BaseModel):
    items: list[RouteDraftOut]
    next_cursor: str | None = None


class RouteNodeOut(BaseModel):
    """详情里的节点。

    RT-01 只实现草稿本身，节点表要到 RT-02 才建，所以现在这条永远是空的；
    place 暂时用 dict 透传，等 catalog 的 Place schema 合入后换成真类型。
    """

    position: int
    place: dict[str, Any]
    stay_minutes: int
    transport: str
    note: str | None = None
    price_min: int | None = None
    price_max: int | None = None


class CostSummaryOut(BaseModel):
    min_total: int = 0
    max_total: int = 0
    currency: str = "CNY"
    unknown_count: int = 0


class RouteDraftDetailOut(RouteDraftOut):
    nodes: list[RouteNodeOut] = Field(default_factory=list)
    cost: CostSummaryOut = Field(default_factory=CostSummaryOut)
