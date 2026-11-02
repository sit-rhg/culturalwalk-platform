# 地点与文化资料 · ORM 模型
"""表结构以 docs/02-数据模型与状态机.md §2.2 为准。

接口字段以 docs/api/openapi.yaml 的 Place / PlaceDetail / PlaceFact / Source 为准。
两者不一致时以契约为准（README「契约唯一真源」），表结构只负责把契约装得下。

迁移只由 P3 一人生成：alembic revision --autogenerate -m "<动词>_<对象>"
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class PlaceStatus(StrEnum):
    """地点的收录状态。draft 只在编辑端可见，verified 才允许被排进路线。

    取值与 openapi.yaml 的 `Place.status` enum 完全一致（小写），前端不用做大小写转换。
    """

    DRAFT = "draft"
    VERIFIED = "verified"


class FactConfidence(StrEnum):
    """一条说法的可信度，决定前端是写成结论还是标「待核实」。

    verified   —— 有可查来源，可以直接当事实写；
    likely     —— 多方旁证但没有一手出处，文案要留余地；
    unverified —— 查不到，前端**必须**标「待核实」，不能伪装成确定结论。
    """

    VERIFIED = "verified"
    LIKELY = "likely"
    UNVERIFIED = "unverified"


def _enum_values(py_enum: type[StrEnum]) -> list[str]:
    """让数据库里存枚举的 value（小写英文），而不是 Python 成员名。"""
    return [member.value for member in py_enum]


def _stored_enum(py_enum: type[StrEnum], *, length: int) -> Enum:
    """枚举一律存成 VARCHAR + CHECK 约束，不用 PG 原生 ENUM。

    理由与 planner 模块一致：原生 `CREATE TYPE` 每加一个取值就要写迁移，
    而 CHECK 既能挡脏值，又能让单测在 SQLite 上跑完（SQLite 没有 ENUM 类型）。
    模块之间不许 import 别人的 models，所以这个 10 行的小工具在本模块内自带一份。
    """
    return Enum(
        py_enum,
        native_enum=False,
        length=length,
        create_constraint=True,
        validate_strings=True,
        values_callable=_enum_values,
    )


class Place(Base):
    """一个文化点位：坐标、区、主题标签，以及它被收录的可信状态。"""

    __tablename__ = "places"
    __table_args__ = (
        # CA-03 要按 `?tag=` 过滤主题；JSONB 上的 GIN 索引让 `culture_tags @> '["历史"]'` 走索引。
        # 单测跑在 SQLite 上：那里没有 jsonb/GIN，建出来是普通索引，不影响用例。
        Index("ix_places_culture_tags", "culture_tags", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 别名/俗称：文档和口述里常出现"岳麓山""岳麓山风景名胜区"两种叫法，搜索要能命中。
    alias: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 坐标用 Numeric 而不是 Float：经纬度是标识符不是测量值，float 会有末位误差。
    # 9 位有效数字、6 位小数 ≈ 0.1 米精度，够文旅场景用。
    lat: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    lon: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    # 坐标系显式落库：国内点位混用 WGS84(CRS84) 和 GCJ-02 会导致地图整体偏移。
    crs: Mapped[str] = mapped_column(String(16), nullable=False, default="OGC:CRS84")
    district: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 主题标签数组。PG 用 JSONB 才能对 `?tag=` 做 GIN 索引；SQLite 侧退化成 JSON，
    # 只为了单测能 create_all，两边读出来都是 list[str]。
    culture_tags: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list
    )
    open_hours: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 开放时间会变（装修、季节调整），来源确认时间必须跟着存，前端才能提示"信息可能过期"。
    open_hours_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[PlaceStatus] = mapped_column(
        _stored_enum(PlaceStatus, length=16), nullable=False, default=PlaceStatus.DRAFT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    facts: Mapped[list[PlaceFact]] = relationship(
        back_populates="place",
        cascade="all, delete-orphan",
        order_by="PlaceFact.created_at",
        lazy="selectin",
    )


class PlaceSource(Base):
    """一条来源：文献、官网、地方志、口述记录。

    独立成表而不是塞进 facts 的文本字段，是因为同一条来源会被很多点位引用，
    而且「查得到出处」是这个产品的核心卖点，来源本身值得单独维护与统计。
    """

    __tablename__ = "place_sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 口述来源没有 URL，因此允许为空；有 URL 的写法见 openapi 的 Source.url。
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 访问时间：网页会改版，答辩时被问到"这个说法哪来的"要能说清是哪一版。
    accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    license: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlaceFact(Base):
    """一条可核验的说法（"始建于北宋开宝九年"），挂在点位下，并能追到来源。"""

    __tablename__ = "place_facts"
    __table_args__ = (
        # 产品红线：标成 verified 的说法必须有出处，否则就是把模型编的内容当事实写。
        # 校验在 service 里先做（返回 422，带字段级说明），这条 CHECK 只是最后一道兜底，
        # 防止脚本/种子数据绕过接口直接写库。
        CheckConstraint(
            "NOT (confidence = 'verified' AND source_id IS NULL)",
            name="ck_place_facts_verified_needs_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    place_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), nullable=False, index=True
    )
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[FactConfidence] = mapped_column(
        _stored_enum(FactConfidence, length=16),
        nullable=False,
        default=FactConfidence.UNVERIFIED,
    )
    # 一个说法一条主来源：openapi 的 `PlaceFactInput.source` 就是单个对象。
    # 读接口返回的 `sources` 是数组，当前长度为 0 或 1；将来要多来源再拆中间表。
    #
    # SET NULL 只对 confidence 为 unverified / likely 的说法生效（来源删了，说法留下、降级展示）。
    # 若来源还被 verified 的说法引用着，置空会撞下面的 CHECK，数据库直接拒绝删除——
    # 这是有意的 fail-closed：删来源的接口（CA-03）必须先把这些说法降到 unverified，
    # 或者返回 409 让编辑先处理，不能留下「已核实但没出处」的行。
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("place_sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    place: Mapped[Place] = relationship(back_populates="facts")
    source: Mapped[PlaceSource | None] = relationship(lazy="joined")
