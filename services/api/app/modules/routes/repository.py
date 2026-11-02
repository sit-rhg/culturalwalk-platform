# 路线与版本 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.modules.routes.models import RouteDraft


class RoutesRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, draft: RouteDraft) -> RouteDraft:
        """落一行。调用方负责 commit（通常和业务写入同一个事务）。"""
        self.db.add(draft)
        self.db.flush()
        return draft

    def get(self, draft_id: uuid.UUID) -> RouteDraft | None:
        return self.db.get(RouteDraft, draft_id)

    def get_by_owner_and_key(
        self, *, owner_id: uuid.UUID, idempotency_key: str
    ) -> RouteDraft | None:
        stmt = select(RouteDraft).where(
            RouteDraft.owner_id == owner_id,
            RouteDraft.idempotency_key == idempotency_key,
        )
        return self.db.scalars(stmt).first()

    def list_by_owner(
        self,
        *,
        owner_id: uuid.UUID,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[RouteDraft]:
        """按 (created_at, id) 倒序取一页；游标定位到"上一页最后一条"之后。"""
        stmt = select(RouteDraft).where(RouteDraft.owner_id == owner_id)
        if after is not None:
            created_at, row_id = after
            # 同一秒创建多条时只比 created_at 会漏数据/重复，必须带上 id 兜底。
            stmt = stmt.where(
                or_(
                    RouteDraft.created_at < created_at,
                    and_(RouteDraft.created_at == created_at, RouteDraft.id < row_id),
                )
            )
        stmt = stmt.order_by(RouteDraft.created_at.desc(), RouteDraft.id.desc()).limit(limit)
        return list(self.db.scalars(stmt))

    def patch_cas(
        self,
        *,
        draft_id: uuid.UUID,
        expected_version: int,
        values: dict[str, Any],
    ) -> int:
        """带 lock_version 条件更新（CAS）：返回 0 表示期间被别人改过。

        不用"先读版本再写"的两步：两个请求同时通过检查时，后写的那条会
        覆盖前一条的修改，而 If-Match 的初衷正是阻止这种静默覆盖。
        """
        stmt = (
            update(RouteDraft)
            .where(
                RouteDraft.id == draft_id,
                RouteDraft.lock_version == expected_version,
            )
            .values(**values, lock_version=RouteDraft.lock_version + 1)
        )
        result = self.db.execute(stmt)
        # session.execute 的静态类型是 Result，只有 UPDATE/DELETE 才带 rowcount。
        return cast(CursorResult[Any], result).rowcount or 0

    def delete(self, draft: RouteDraft) -> None:
        self.db.delete(draft)
        self.db.flush()
