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

    def add(self, asset: MediaAsset) -> None:
        self.db.add(asset)

    def get(self, asset_id: uuid.UUID) -> MediaAsset | None:
        return self.db.get(MediaAsset, asset_id)

    def get_by_owner_and_key(
        self, *, owner_id: uuid.UUID, idempotency_key: str
    ) -> MediaAsset | None:
        stmt = select(MediaAsset).where(
            MediaAsset.owner_id == owner_id,
            MediaAsset.idempotency_key == idempotency_key,
        )
        return self.db.scalars(stmt).first()
