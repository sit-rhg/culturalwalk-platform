# 路线与版本 · ORM 模型
"""表结构以 docs/02-数据模型与状态机.md §2.3 为准。

迁移只由 P3 一人生成：alembic revision --autogenerate -m "<动词>_<对象>"
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def _utcnow() -> datetime:
    """时间戳由应用生成，不用数据库的 CURRENT_TIMESTAMP。

    SQLite 的 CURRENT_TIMESTAMP 只精确到秒，而游标分页按 (created_at, id)
    定位：同一秒创建的多条草稿如果时间被截断，翻页时会整页重复。
    应用侧时间带微秒，SQLite 与 PG 行为一致。
    """
    return datetime.now(UTC)


class DraftState(StrEnum):
    """草稿状态机，见 docs/02 §3.3。

    SUBMITTED_LOCKED / APPROVED_LOCKED 期间作者不能改内容；要改只能 fork
    出新草稿（RT-03 之后才有的能力）。这里只把状态存下来，流转规则由
    service 判断——状态机写在模型里，别的地方就永远不知道谁在改状态。
    """

    EDITING = "EDITING"
    SUBMITTED_LOCKED = "SUBMITTED_LOCKED"
    APPROVED_LOCKED = "APPROVED_LOCKED"
    ARCHIVED = "ARCHIVED"


def _enum_values(py_enum: type[StrEnum]) -> list[str]:
    """落库的是 value（与契约 enum 完全一致），不是 Python 成员名。"""
    return [member.value for member in py_enum]


def _stored_enum(py_enum: type[StrEnum], *, length: int) -> Enum:
    """枚举一律 VARCHAR + CHECK，不用 PG 原生 ENUM：加状态不用写迁移，SQLite 也能测。"""
    return Enum(
        py_enum,
        native_enum=False,
        length=length,
        create_constraint=True,
        validate_strings=True,
        values_callable=_enum_values,
    )


class RouteDraft(Base):
    """一条路线的编辑草稿：发布前的工作区。"""

    __tablename__ = "route_drafts"
    __table_args__ = (
        # 幂等（docs/03 §1.2）：同一个作者拿同一个 Idempotency-Key 只能建出一份草稿。
        # 后到的并发请求会撞这个约束，回滚后重查拿先落库的那份。
        UniqueConstraint("owner_id", "idempotency_key", name="uq_route_drafts_owner_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # users 表属于 identity 模块且尚未建；跨模块只存裸 ID，等 ID-01 落地再补外键。
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    # 草稿在发布前不属于任何 route；发布（RT-05）时才回填。
    route_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(String(60), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # 契约的 RouteDraft 响应里不暴露主题/时长，但创建时收下了——发布时复制到 route。
    theme_tags: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list
    )
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[DraftState] = mapped_column(
        _stored_enum(DraftState, length=20), nullable=False, default=DraftState.EDITING
    )
    # 乐观锁：每次 PATCH 成功 +1；客户端用 If-Match 带回来，不匹配 412。
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    base_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # 幂等（docs/03 §1.2）。POST 用一个 key 建草稿；PATCH 的 key 单独存，
    # 否则第二次 PATCH 会把创建时的 key 覆盖掉，创建请求重试就查不到原草稿。
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patch_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patch_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # PUT /{id}/nodes 的幂等键单独一列：它和 PATCH 是两类写操作，
    # 混用同一列会让「先 PATCH 再 PUT」的重放判断互相覆盖。
    nodes_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nodes_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    # 节点按 position 升序读；删除草稿时级联删节点（DB 上也有 ON DELETE CASCADE）。
    nodes: Mapped[list[RouteNode]] = relationship(
        back_populates="draft",
        cascade="all, delete-orphan",
        order_by="RouteNode.position",
        passive_deletes=True,
    )


class TransportMode(StrEnum):
    """节点之间的交通方式，取值与 openapi 的 RouteNodeInput.transport 一致。"""

    WALK = "walk"
    METRO = "metro"
    BUS = "bus"
    TAXI = "taxi"
    BIKE = "bike"


class RouteNode(Base):
    """草稿里的一个停靠点。

    整表替换语义（PUT /route-drafts/{id}/nodes）：客户端每次提交完整顺序，
    服务端按数组下标重排 1..N。这样「调整顺序」不需要额外的 move 接口，
    也不会出现移动过程中 position 唯一约束互相打架的问题。
    """

    __tablename__ = "route_nodes"
    __table_args__ = (
        # 同一草稿里位置唯一：整表替换保证写进来的 1..N 不重复，
        # 这条约束用来兜住「有人绕过 replace_nodes 直接插行」的 bug。
        # 它建的 (draft_id, position) 索引同时覆盖详情查询的过滤与排序，
        # 不再单独建一个重复索引（写放大没有收益）。
        UniqueConstraint("draft_id", "position", name="uq_route_nodes_draft_position"),
        CheckConstraint("position >= 1", name="ck_route_nodes_position"),
        # 与契约 RouteNodeInput.stay_minutes 的 10..480 对齐：应用层校验之外再兜一层，
        # 免得绕过 API 的脚本写进一个「停留 0 分钟」的节点。
        CheckConstraint(
            "stay_minutes >= 10 AND stay_minutes <= 480",
            name="ck_route_nodes_stay_minutes",
        ),
        CheckConstraint("price_min IS NULL OR price_min >= 0", name="ck_route_nodes_price_min"),
        CheckConstraint("price_max IS NULL OR price_max >= 0", name="ck_route_nodes_price_max"),
        CheckConstraint(
            "price_min IS NULL OR price_max IS NULL OR price_max >= price_min",
            name="ck_route_nodes_price_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("route_drafts.id", ondelete="CASCADE"), nullable=False
    )
    # 地点属于 catalog 模块：跨模块只存裸 ID，不建物理外键（docs/02 §1）。
    # 存在性/是否已发布由 catalog 的 service 在写入前校验。
    place_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # 1 起连续；由服务端按提交数组的下标重排，客户端不能自己指定。
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    stay_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    transport: Mapped[TransportMode | None] = mapped_column(
        _stored_enum(TransportMode, length=12), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 节点引用的媒体资产只存 id（跨模块不建关联表/外键，docs/02 §1）。
    # 是否已扫描 clean、是否本人资产，由 media 模块在写入前核验。
    media_asset_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    draft: Mapped[RouteDraft] = relationship(back_populates="nodes")


class RouteVersion(Base):
    """草稿冻结出的不可变版本（docs/02 §2.3）。

    提交审核、审批、发布引用的都是这里的 `version_id + content_hash`；
    冻结之后本行不再接受任何写入（要改内容只能 fork 新草稿）。快照存整份
    JSON，而不是引用草稿行：草稿随时会被作者改，版本必须能独立重放。
    """

    __tablename__ = "route_versions"
    __table_args__ = (
        # 同一份草稿的版本号递增；并发冻结靠它兜住，撞了重算一次。
        UniqueConstraint("draft_id", "version_no", name="uq_route_versions_draft_version_no"),
        CheckConstraint("version_no >= 1", name="ck_route_versions_version_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 发布（RT-05）前草稿没有 route_id，所以这里可空；发布时回填。
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    # 版本来自哪份草稿。ON DELETE RESTRICT：被冻结过的草稿不允许物理删除，
    # 否则版本会变成孤儿；归档用 state=ARCHIVED，不动行。
    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("route_drafts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 整条路线的快照。PG 落 JSONB（可查询、可建 GIN），SQLite 单测退化成 JSON。
    snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    # sha256(canonical_json(snapshot))，见 service._content_hash。
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 谁冻结的；跨模块裸 ID（users 表尚未建）。
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
