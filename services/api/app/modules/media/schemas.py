# 媒体资源 · DTO
"""请求/响应模型。字段与 docs/api/openapi.yaml 保持一致，改 yaml 再改这里。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings

ALLOWED_MIME = ("image/jpeg", "image/png", "image/webp")


class MediaIntentIn(BaseModel):
    """POST /media/intent 的请求体。"""

    filename: str = Field(min_length=1, max_length=255, description="原始文件名（只取 basename）")
    mime: Literal["image/jpeg", "image/png", "image/webp"]
    size: int = Field(gt=0, le=settings.media_max_bytes, description="字节数")

    @field_validator("filename")
    @classmethod
    def _basename_only(cls, value: str) -> str:
        """路径/Windows 反斜杠都砍掉：文件名只用来展示和拼 object_key，不能带目录穿越。"""
        name = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
        if not name or name in {".", ".."}:
            raise ValueError("文件名不能为空")
        return name


class MediaIntentOut(BaseModel):
    """openapi.yaml#MediaIntent：上传地址 + 表单字段 + 过期时间。"""

    asset_id: uuid.UUID
    upload_url: str
    fields: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _assume_utc(cls, value: datetime) -> datetime:
        # SQLite 会丢 tzinfo；出参一律补 UTC
        return value if value.tzinfo else value.replace(tzinfo=UTC)


class MediaAssetOut(BaseModel):
    """openapi.yaml#MediaAsset：clean 之前 url 一定为 null。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    zone: str
    scan_state: str
    url: str | None = None
    width: int | None = None
    height: int | None = None
