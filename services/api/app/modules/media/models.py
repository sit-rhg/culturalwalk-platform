# 媒体资源 · ORM 模型
"""表结构以 docs/02-数据模型与状态机.md 与 docs/…开发范式.md §11.1.1 为准。

命名对照：契约里叫 `zone`/`scan_state`，架构文档里叫隔离区/清洁区与扫描状态。
这里落库的是契约的枚举值（小写），前端与文档都按这一套。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _utcnow() -> datetime:
    """应用侧时间戳：SQLite 的 CURRENT_TIMESTAMP 只到秒，跨库行为要一致。"""
    return datetime.now(UTC)


class MediaZone(StrEnum):
    QUARANTINE = "quarantine"
    CLEAN = "clean"


class ScanState(StrEnum):
    PENDING = "pending"
    SCANNING = "scanning"
    CLEAN = "clean"
    REJECTED = "rejected"


def _enum_values(py_enum: type[StrEnum]) -> list[str]:
    return [member.value for member in py_enum]


def _stored_enum(py_enum: type[StrEnum], *, length: int) -> Enum:
    return Enum(
        py_enum,
        native_enum=False,
        length=length,
        create_constraint=True,
        validate_strings=True,
        values_callable=_enum_values,
    )


class MediaAsset(Base):
    """一次上传的资产。扫描通过前 `zone=quarantine`，对外不可读。"""

    __tablename__ = "media_assets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 跨模块只存 ID，不建物理外键（identity 的表在另一个模块里）
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    object_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    mime: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    zone: Mapped[MediaZone] = mapped_column(
        _stored_enum(MediaZone, length=12), default=MediaZone.QUARANTINE, nullable=False
    )
    scan_state: Mapped[ScanState] = mapped_column(
        _stored_enum(ScanState, length=12), default=ScanState.PENDING, nullable=False
    )
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    # POST /media/intent 的幂等键；重放靠 (owner_id, idempotency_key) 定位
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_media_assets_owner_idempotency"),
        CheckConstraint("size >= 0", name="ck_media_assets_size"),
    )
