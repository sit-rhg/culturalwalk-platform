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

    def add(self, asset: MediaAsset) -> MediaAsset:
        self.db.add(asset)
        self.db.flush()
        return asset

    def get(self, media_id: uuid.UUID) -> MediaAsset | None:
        """默认只返回未删除的资产：软删之后对读接口等价于不存在。"""
        stmt = select(MediaAsset).where(
            MediaAsset.id == media_id,
            MediaAsset.deleted_at.is_(None),
        )
        return self.db.scalar(stmt)

    def get_by_idempotency_key(
        self, *, owner_id: uuid.UUID, idempotency_key: str
    ) -> MediaAsset | None:
        """同一上传者 + 同一幂等键只对应一条资产；重放时直接返回它。"""
        stmt = select(MediaAsset).where(
            MediaAsset.owner_id == owner_id,
            MediaAsset.idempotency_key == idempotency_key,
            MediaAsset.deleted_at.is_(None),
        )
        return self.db.scalar(stmt)

    def list_scanning(self, limit: int) -> list[MediaAsset]:
        """待扫描队列：已确认上传（scanning）且仍在隔离区的资产，先到先扫。

        按 (created_at, id) 排序而不是只按 created_at：SQLite 的时间戳可能同秒，
        只按时间会让多次轮询拿到的顺序不稳定。
        """
        stmt = (
            select(MediaAsset)
            .where(
                MediaAsset.scan_state == ScanState.SCANNING,
                MediaAsset.zone == MediaZone.QUARANTINE,
            )
            .order_by(MediaAsset.created_at.asc(), MediaAsset.id.asc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))
