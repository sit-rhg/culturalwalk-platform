# 媒体资源 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.media.models import MediaAsset


class MediaRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, asset: MediaAsset) -> MediaAsset:
        self.db.add(asset)
        self.db.flush()
        return asset

    def get(self, media_id: uuid.UUID) -> MediaAsset | None:
        return self.db.get(MediaAsset, media_id)

    def get_by_idempotency_key(
        self, *, owner_id: uuid.UUID, idempotency_key: str
    ) -> MediaAsset | None:
        """同一上传者 + 同一幂等键只对应一条资产；重放时直接返回它。"""
        stmt = select(MediaAsset).where(
            MediaAsset.owner_id == owner_id,
            MediaAsset.idempotency_key == idempotency_key,
        )
        return self.db.scalar(stmt)
