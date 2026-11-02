# 媒体资源 · ORM 模型
"""表结构以 docs/02-数据模型与状态机.md §2.4 为准：media_assets。

生命周期：quarantine(pending) --complete--> scanning --扫描通过--> clean --publish--> published。
对外只暴露契约里的 zone / scan_state；published 用 published_at 标记（见 service.lifecycle）。

迁移只由 P3 一人生成：alembic revision --autogenerate -m "<动词>_<对象>"
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class MediaZone(StrEnum):
    """对象存储里的隔离区 / 公开区。只有 clean 的媒体才能给出公开 URL。"""

    QUARANTINE = "quarantine"
    CLEAN = "clean"


class ScanState(StrEnum):
    """扫描流水线状态，取值与 openapi.yaml#MediaAsset.scan_state 一致。"""

    PENDING = "pending"
    SCANNING = "scanning"
    CLEAN = "clean"
    REJECTED = "rejected"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum_values(py_enum: type[StrEnum]) -> list[str]:
    return [member.value for member in py_enum]


def _stored_enum(py_enum: type[StrEnum], *, length: int) -> Enum:
    """枚举存 VARCHAR + CHECK（不用 PG 原生 ENUM），理由同 catalog：SQLite 单测能跑。"""
    return Enum(
        py_enum,
        native_enum=False,
        length=length,
        create_constraint=True,
        validate_strings=True,
        values_callable=_enum_values,
    )


class MediaAsset(Base):
    """一次上传的媒体资产。

    owner_id 指向 identity 模块的 users.id，但不建跨模块物理外键（模块边界只认 ID）。
    """

    __tablename__ = "media_assets"
    __table_args__ = (
        # 幂等（docs/03 §1.2）：同一上传者 + 同一 Idempotency-Key 只能落一条资产。
        UniqueConstraint("owner_id", "idempotency_key", name="uq_media_assets_owner_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    # 对象存储里的 key；桶内路径，不拼域名（域名随环境变）。
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    zone: Mapped[MediaZone] = mapped_column(
        _stored_enum(MediaZone, length=16), nullable=False, default=MediaZone.QUARANTINE
    )
    scan_state: Mapped[ScanState] = mapped_column(
        _stored_enum(ScanState, length=16), nullable=False, default=ScanState.PENDING
    )
    # 公开 URL 只在 clean 之后才写；quarantine 期间保持空。
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 预签名上传地址的过期时间：前端/客户端必须在此之前 PUT/POST 完。
    upload_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # published 状态：扫描 clean 之后、被发布引用时才写入（生命周期第四段）。
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 同一个上传者用同一个 key 重试时，返回首次那条资产。
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
