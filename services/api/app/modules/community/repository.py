# 社区互动 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.modules.community.models import Comment, CommentStatus, Favorite

# ⚠️ 已知的跨模块 import models：routes 模块没有公开的「已发布路线」读服务
# （RoutesService 只有草稿自己的 CRUD，发布 RT-05 也还没落地），而社区必须能按
# 已发布态列路线。当前约定「route_versions.route_id 非空 = 已发布」（发布时才回填，
# 见 routes/models.py 的注释）。等 routes 模块提供公开读服务或 publications 表落地，
# 把下面「已发布路线」的三段查询换成 service 调用，删掉这个 import。
from app.modules.routes.models import RouteDraft, RouteVersion

# 注释型（置顶、时间倒序）游标的位置；置顶状态不是时间，core 的两字段游标装不下。
CommentPosition = tuple[bool, datetime, uuid.UUID]
# 路线列表的位置（发布时间 + route_id）。
RoutePosition = tuple[datetime, uuid.UUID]


class CommunityRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------- 评论 ----------

    def add_comment(self, comment: Comment) -> Comment:
        self.db.add(comment)
        return comment

    def get_comment(self, comment_id: uuid.UUID) -> Comment | None:
        return self.db.get(Comment, comment_id)

    def list_comments(
        self,
        *,
        route_id: uuid.UUID,
        after: CommentPosition | None,
        limit: int,
    ) -> list[Comment]:
        """置顶在前，其余按时间倒序；游标条件与排序键一一对应，不漏不重。"""
        stmt = select(Comment).where(
            Comment.route_id == route_id,
            Comment.status == CommentStatus.VISIBLE,
        )
        if after is not None:
            pinned, created_at, comment_id = after
            same_rank = and_(
                Comment.pinned.is_(pinned),
                or_(
                    Comment.created_at < created_at,
                    and_(
                        Comment.created_at == created_at,
                        Comment.id < comment_id,
                    ),
                ),
            )
            # 布尔列不能用 < 比较，改用「同档 + 更低档」表达置顶降序：
            # 只有 True/False 两档，游标是 True 时后面还剩同档 True 行和全部 False 行。
            if pinned:
                stmt = stmt.where(or_(same_rank, Comment.pinned.is_(False)))
            else:
                stmt = stmt.where(same_rank)
        stmt = stmt.order_by(
            Comment.pinned.desc(),
            Comment.created_at.desc(),
            Comment.id.desc(),
        )
        return list(self.db.execute(stmt.limit(limit)).scalars())

    def comment_counts(self, route_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not route_ids:
            return {}
        stmt = (
            select(Comment.route_id, func.count())
            .where(
                Comment.route_id.in_(route_ids),
                Comment.status == CommentStatus.VISIBLE,
            )
            .group_by(Comment.route_id)
        )
        return {route_id: int(total) for route_id, total in self.db.execute(stmt)}

    def unpin_others(self, *, route_id: uuid.UUID, keep_id: uuid.UUID) -> None:
        """取消该路线其他置顶项。路由「每路线最多一条置顶」靠部分唯一索引兜底，
        先批量清掉再置顶，避免单条写事务撞唯一约束。"""
        self.db.execute(
            update(Comment)
            .where(
                Comment.route_id == route_id,
                Comment.id != keep_id,
                Comment.pinned.is_(True),
            )
            .values(pinned=False)
        )

    # ---------- 收藏 ----------

    def get_favorite(self, *, user_id: uuid.UUID, route_id: uuid.UUID) -> Favorite | None:
        stmt = select(Favorite).where(
            Favorite.user_id == user_id,
            Favorite.route_id == route_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def add_favorite(self, favorite: Favorite) -> Favorite:
        self.db.add(favorite)
        return favorite

    def delete_favorite(self, favorite: Favorite) -> None:
        self.db.delete(favorite)

    def favorite_counts(self, route_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not route_ids:
            return {}
        stmt = (
            select(Favorite.route_id, func.count())
            .where(Favorite.route_id.in_(route_ids))
            .group_by(Favorite.route_id)
        )
        return {route_id: int(total) for route_id, total in self.db.execute(stmt)}

    # ---------- 已发布路线（临时读 routes 模块的表，原因见文件头） ----------

    def latest_published_positions(
        self,
        *,
        after: RoutePosition | None,
        limit: int,
        descending: bool,
    ) -> list[RoutePosition]:
        """每个已发布 route 的「最新版本时间」，按时间排序分页。

        先只取 (route_id, max(created_at))：GROUP BY 一行一个 route，
        分页步长与返回条数一致，不会像 join 版本明细那样一页缩水。
        """
        latest = (
            select(
                RouteVersion.route_id.label("route_id"),
                func.max(RouteVersion.created_at).label("last_at"),
            )
            .where(RouteVersion.route_id.is_not(None))
            .group_by(RouteVersion.route_id)
            .subquery()
        )
        stmt = select(latest.c.route_id, latest.c.last_at)
        if after is not None:
            at, route_id = after
            if descending:
                stmt = stmt.where(
                    or_(
                        latest.c.last_at < at,
                        and_(latest.c.last_at == at, latest.c.route_id < route_id),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        latest.c.last_at > at,
                        and_(latest.c.last_at == at, latest.c.route_id > route_id),
                    )
                )
        if descending:
            stmt = stmt.order_by(latest.c.last_at.desc(), latest.c.route_id.desc())
        else:
            stmt = stmt.order_by(latest.c.last_at.asc(), latest.c.route_id.asc())
        rows = self.db.execute(stmt.limit(limit))
        return [(route_id, last_at) for route_id, last_at in rows]

    def latest_versions(self, route_ids: list[uuid.UUID]) -> dict[uuid.UUID, RouteVersion]:
        """批量取每條路线的最新版本行（内容取 version.snapshot）。"""
        if not route_ids:
            return {}
        stmt = (
            select(RouteVersion)
            .where(RouteVersion.route_id.in_(route_ids))
            .order_by(RouteVersion.created_at.desc(), RouteVersion.id.desc())
        )
        result: dict[uuid.UUID, RouteVersion] = {}
        for version in self.db.execute(stmt).scalars():
            if version.route_id is not None:
                # 同一 route 的第一条就是最新（SQL 已按时间倒序）。
                result.setdefault(version.route_id, version)
        return result

    def latest_version(self, route_id: uuid.UUID) -> RouteVersion | None:
        stmt = (
            select(RouteVersion)
            .where(RouteVersion.route_id == route_id)
            .order_by(RouteVersion.created_at.desc(), RouteVersion.id.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def versions_of(self, route_id: uuid.UUID) -> list[RouteVersion]:
        stmt = (
            select(RouteVersion)
            .where(RouteVersion.route_id == route_id)
            .order_by(RouteVersion.created_at.desc(), RouteVersion.version_no.desc())
        )
        return list(self.db.execute(stmt).scalars())

    def draft_owners(self, draft_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
        """版本 → 草稿 → 作者。作者身份属于 identity，这里只回裸 ID。"""
        if not draft_ids:
            return {}
        stmt = select(RouteDraft.id, RouteDraft.owner_id).where(RouteDraft.id.in_(draft_ids))
        return {draft_id: owner_id for draft_id, owner_id in self.db.execute(stmt)}

    def snapshot_of(self, version: RouteVersion) -> dict[str, Any]:
        """快照是 JSON 列；旧数据/手工塞的值可能不是 dict，统一兜成空对象。"""
        value = version.snapshot
        return value if isinstance(value, dict) else {}
