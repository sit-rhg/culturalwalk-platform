# 社区互动 · ORM 模型
"""表结构以 docs/02-数据模型与状态机.md 为准。

迁移只由 P3 一人生成：alembic revision --autogenerate -m "<动词>_<对象>"
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _utcnow() -> datetime:
    """时间戳由应用生成，不用数据库的 CURRENT_TIMESTAMP。

    评论分页游标按 (created_at, id) 定位，SQLite 的 CURRENT_TIMESTAMP 只有秒精度，
    同一秒内发的两条评论会被截成同一位置，翻页时整页重复。应用侧时间带微秒。
    """
    return datetime.now(UTC)


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


class CommentStatus(StrEnum):
    """评论可见性：被审核隐藏的评论保留行，只改状态，不物理删除（留证据）。"""

    VISIBLE = "visible"
    HIDDEN = "hidden"


class Comment(Base):
    """一条路线评论。

    route_id 指向 routes 模块的路线（当前由 route_versions.route_id 代表「已发布」，
    见 repository.py 的说明）。跨模块只存裸 ID，不建物理外键，与 notifications 一致。
    """

    __tablename__ = "comments"
    __table_args__ = (
        Index("ix_comments_route_created", "route_id", "created_at"),
        # 每条路线最多一条置顶（docs/02 §2.5）。部分唯一索引只在 pinned 为真时生效，
        # 普通评论可以无限多条；PG 与 SQLite 都支持部分索引，测试库也能兜住并发置顶。
        Index(
            "uq_comments_route_pinned",
            "route_id",
            unique=True,
            sqlite_where=text("pinned = 1"),
            postgresql_where=text("pinned"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    route_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    # 契约上限 500 字；列宽与契约对齐，PG 上超出直接拒绝，应用层先校验给出字段级错误。
    body: Mapped[str] = mapped_column(String(500), nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[CommentStatus] = mapped_column(
        _stored_enum(CommentStatus, length=10), nullable=False, default=CommentStatus.VISIBLE
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Favorite(Base):
    """用户收藏一条路线；同一对 (user_id, route_id) 只能有一行，重复收藏由唯一约束兜底。"""

    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "route_id", name="uq_favorites_user_route"),
        Index("ix_favorites_route", "route_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    route_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


# Text 目前没用上，但评论正文未来可能放开到长文；保留 import 会被 ruff 判未使用，
# 所以这里不 import，列宽用 String(500) 与契约一致。
__all__ = ["Comment", "CommentStatus", "Favorite"]
