# 地点与文化资料 · DTO
"""请求/响应模型。字段与 docs/api/openapi.yaml 保持一致，改 yaml 再改这里。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _required_text(value: str) -> str:
    """必填文本字段：trim 后不能为空，否则等同于没填。"""
    text = value.strip()
    if not text:
        raise ValueError("不能为空")
    return text


class SourceOut(BaseModel):
    """openapi.yaml#Source：口述来源没有 URL，所以 url/publisher 可空。

    accessed_at / license 是溯源信息（D8）：网页会改版，答辩时被问「这个说法
    哪来的、哪一版、能不能转」要能答上来，所以读接口也要带出去。
    """

    model_config = ConfigDict(from_attributes=True)

    title: str
    url: str | None = None
    publisher: str | None = None
    accessed_at: datetime | None = None
    license: str | None = None


class PlaceOut(BaseModel):
    """openapi.yaml#Place 的字段，多一个都不返回。

    crs 必须回显：GCJ-02 的点位如果按 WGS84 画，会整体偏 300-500 米，
    而调用方只有拿到这一列才知道要不要做偏移纠正。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    district: str | None = None
    lat: float
    lon: float
    summary: str | None = None
    culture_tags: list[str] = Field(default_factory=list)
    status: str
    crs: str = "OGC:CRS84"


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


CultureTag = Annotated[str, Field(min_length=1, max_length=32)]


class SourceIn(BaseModel):
    """录入一条来源（契约 Source，所有字段可选）。

    契约里 title/url/publisher 全可不填：口述来源没有网址，档案也可能只剩
    一个书名。**但「能不能标 verified」由业务规则管**——见 service.add_fact：
    标成 verified 却没有任何来源，仍然会被拒（D6）。
    """

    title: str | None = Field(default=None, max_length=255)
    url: str | None = Field(default=None, max_length=2048)
    publisher: str | None = Field(default=None, max_length=128)
    # 授权/使用条款（如 CC-BY、公共领域）：引用别人整理的资料时要能说清能不能转载（D8）。
    license: str | None = Field(default=None, max_length=64)

    @field_validator("title")
    @classmethod
    def _title_blank_to_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        """有网址就只认 http/https 且有主机名：ftp:、javascript:、裸域名都不算来源。"""
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("来源链接必须是 http(s) URL")
        return text

    @field_validator("publisher", "license")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class PlaceCreateIn(BaseModel):
    """POST /places 的请求体（契约 PlaceInput + 坐标系 crs）。"""

    name: str = Field(min_length=1, max_length=128)
    district: str | None = Field(default=None, max_length=64)
    lat: float = Field(ge=-90, le=90, description="纬度 WGS84/CRS84，-90~90")
    lon: float = Field(ge=-180, le=180, description="经度 WGS84/CRS84，-180~180")
    summary: str | None = None
    culture_tags: list[CultureTag] = Field(default_factory=list, max_length=20)
    # 契约漏了坐标系；国内点位混用 WGS84 与 GCJ-02 会整体偏移，必须落库。
    crs: Literal["OGC:CRS84", "WGS84", "GCJ-02"] = "OGC:CRS84"

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("summary")
    @classmethod
    def _summary_blank_to_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("culture_tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        """去空白、去重、保序：重复标签只会让过滤结果变脏。"""
        return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))


class PlaceFactCreateIn(BaseModel):
    """POST /places/{place_id}/facts 的请求体（契约 PlaceFactInput）。

    source 在契约里可选，这里也放开：没有来源的旧资料、口述记录照样能入库，
    只是会被标成 unverified 且不出现在公开详情里。**verified 必须带来源**这条
    产品红线改由 service 判（有字段级说明的 422），不再靠 schema 一刀切（D6）。
    """

    claim: str = Field(min_length=1, max_length=2000)
    confidence: Literal["verified", "likely", "unverified"]
    source: SourceIn | None = None

    @field_validator("claim")
    @classmethod
    def _claim_not_blank(cls, value: str) -> str:
        return _required_text(value)


class PlaceStatusUpdateIn(BaseModel):
    """PATCH /places/{place_id} 的请求体（D9）。

    只放开「收录状态」一个字段：草稿点位补完资料后要能发布，
    发布错了要能撤回；改名字、改坐标属于另一件事（还涉及审核与历史版本），
    不在这张卡里顺手做。
    """

    status: Literal["draft", "verified"]
