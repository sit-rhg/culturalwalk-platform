# 媒体资源 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.media.models import MediaAsset, MediaZone, ScanState


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

    def get_many(self, asset_ids: list[uuid.UUID]) -> list[MediaAsset]:
        """批量取资产：routes 一次 PUT 会带多个 id，逐个 get 会打 N 次库。"""
        if not asset_ids:
            return []
        stmt = select(MediaAsset).where(MediaAsset.id.in_(asset_ids))
        return list(self.db.scalars(stmt).all())

    def list_pending_scan(self, limit: int) -> list[MediaAsset]:
        """已经确认上传、等待扫描的资产（最老的先处理，避免饿死）。"""
        stmt = (
            select(MediaAsset)
            .where(
                MediaAsset.scan_state == ScanState.SCANNING,
                MediaAsset.zone == MediaZone.QUARANTINE,
            )
            .order_by(MediaAsset.created_at, MediaAsset.id)
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())
