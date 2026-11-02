# 地点与文化资料 · DTO
"""请求/响应模型。字段与 docs/api/openapi.yaml 保持一致，改 yaml 再改这里。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SourceOut(BaseModel):
    """openapi.yaml#Source：口述来源没有 URL，所以 url/publisher 可空。"""

    model_config = ConfigDict(from_attributes=True)

    title: str
    url: str | None = None
    publisher: str | None = None


class PlaceOut(BaseModel):
    """openapi.yaml#Place 的 8 个字段，多一个都不返回。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    district: str | None = None
    lat: float
    lon: float
    summary: str | None = None
    culture_tags: list[str] = Field(default_factory=list)
    status: str


class PlaceListOut(BaseModel):
    """游标分页外壳：{"items": [...], "next_cursor": null}。"""

    items: list[PlaceOut]
    next_cursor: str | None = None


class PlaceFactOut(BaseModel):
    """一条说法 + 它的来源数组（当前 0 或 1 条，见 models.PlaceFact.source_id）。"""

    id: uuid.UUID
    claim: str
    confidence: str
    sources: list[SourceOut] = Field(default_factory=list)


class PlaceDetailOut(PlaceOut):
    """Place + 详情字段；facts 按 created_at 升序，和录入顺序一致。"""

    facts: list[PlaceFactOut] = Field(default_factory=list)
    open_hours: str | None = None
    open_hours_verified_at: datetime | None = None

    @field_validator("open_hours_verified_at")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        # SQLite 会丢掉 tzinfo；出参一律补齐 UTC，保证前端拿到带时区的 ISO-8601
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
