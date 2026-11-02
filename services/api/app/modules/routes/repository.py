# 路线与版本 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.modules.routes.models import RouteDraft, RouteNode, RouteVersion


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

    # ===== 节点 =====
    def list_nodes(self, draft_id: uuid.UUID) -> list[RouteNode]:
        """按 position 升序；(draft_id, position) 有联合索引，过滤和排序一次走完。"""
        stmt = select(RouteNode).where(RouteNode.draft_id == draft_id).order_by(RouteNode.position)
        return list(self.db.scalars(stmt))

    def replace_nodes(self, draft_id: uuid.UUID, nodes: list[RouteNode]) -> None:
        """整表替换：先删后插。

        不用逐条 diff：顺序本身就是客户端提交的完整语义，diff 出来的中间状态
        （比如两条互换）反而要处理 position 撞车。一次 DELETE + 一批 INSERT
        在同一个事务里，读到的要么是旧顺序、要么是新顺序。
        """
        self.db.execute(delete(RouteNode).where(RouteNode.draft_id == draft_id))
        self.db.add_all(nodes)
        self.db.flush()

    # ===== 版本 =====
    def has_versions(self, draft_id: uuid.UUID) -> bool:
        """草稿是否冻结过版本：冻结过的草稿不能物理删除（版本不可变、无孤儿）。"""
        stmt = select(RouteVersion.id).where(RouteVersion.draft_id == draft_id).limit(1)
        return self.db.scalars(stmt).first() is not None

    def next_version_no(self, draft_id: uuid.UUID) -> int:
        """下一个版本号。并发冻结会拿到同一个号，由唯一约束兜底后重算。"""
        stmt = select(func.max(RouteVersion.version_no)).where(RouteVersion.draft_id == draft_id)
        current = self.db.scalar(stmt)
        return (current or 0) + 1

    def add_version(self, version: RouteVersion) -> RouteVersion:
        self.db.add(version)
        self.db.flush()
        return version
