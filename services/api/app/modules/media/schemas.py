# 媒体资源 · DTO
"""请求/响应模型。字段与 docs/api/openapi.yaml 保持一致，改 yaml 再改这里。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.media.models import MediaZone, ScanState

AllowedMime = Literal["image/jpeg", "image/png", "image/webp"]


class MediaIntentIn(BaseModel):
    """申请直传：只描述「要传什么」，不携带文件本体。"""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    mime: AllowedMime
    size: int = Field(gt=0)


class MediaIntentOut(BaseModel):
    """上传凭据。`upload_url` 只对这一个 key 有效，过期即废。"""

    asset_id: uuid.UUID
    upload_url: str
    fields: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime


class MediaAssetOut(BaseModel):
    """对外只暴露三种状态字段 + 可读 URL；quarantine 阶段没有 URL。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    zone: MediaZone
    scan_state: ScanState
    url: str | None = None
    width: int | None = None
    height: int | None = None
