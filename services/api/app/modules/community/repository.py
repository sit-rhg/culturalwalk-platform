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
# （RoutesService 只有草稿自己的 CRUD），而社区必须能按已发布态列路线。
# 判据是 moderation 的 publications.state == PUBLISHED，并且内容取发布记录
# 绑定的 version_id——**不能**用 route_versions.route_id 非空：下线只改
# publications.state，route_id 会一直留在版本行上；同一 route 也可能有多条
# 版本，最新那条未必是已发布的那条。等 routes 提供公开读服务再把查询收口。
from app.modules.moderation.models import Publication, PublicationState
from app.modules.routes.models import RouteDraft, RouteVersion

# 注释型（置顶、时间倒序）游标的位置；置顶状态不是时间，core 的两字段游标装不下。
CommentPosition = tuple[bool, datetime, uuid.UUID]
# 路线列表游标的位置（发布时间 + route_id）与查询返回的一行（route_id + 发布时间）。
RoutePosition = tuple[datetime, uuid.UUID]
PublishedPosition = tuple[uuid.UUID, datetime]


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

    # ---------- 已发布路线（跨模块读 publications + routes 的表，原因见文件头） ----------

    def latest_published_positions(
        self,
        *,
        after: RoutePosition | None,
        limit: int,
        descending: bool,
    ) -> list[PublishedPosition]:
        """每个「当前处于发布态」的 route 及发布版本的创建时间，按时间分页。

        只看 publications.state = PUBLISHED 且内容取发布记录绑定的那个版本：
        下线只改状态、不搬 route_id，用 route_id 判断会把已下线路线继续放出来。
        """
        latest = (
            select(
                Publication.route_id.label("route_id"),
                RouteVersion.created_at.label("last_at"),
            )
            .join(RouteVersion, RouteVersion.id == Publication.version_id)
            .where(Publication.state == PublicationState.PUBLISHED)
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
        """批量取每条路线「发布记录绑定的」版本行（内容取 version.snapshot）。"""
        if not route_ids:
            return {}
        stmt = (
            select(RouteVersion)
            .join(Publication, Publication.version_id == RouteVersion.id)
            .where(
                RouteVersion.route_id.in_(route_ids),
                Publication.state == PublicationState.PUBLISHED,
            )
        )
        return {
            version.route_id: version
            for version in self.db.execute(stmt).scalars()
            if version.route_id is not None
        }

    def latest_version(self, route_id: uuid.UUID) -> RouteVersion | None:
        stmt = (
            select(RouteVersion)
            .join(Publication, Publication.version_id == RouteVersion.id)
            .where(
                RouteVersion.route_id == route_id,
                Publication.state == PublicationState.PUBLISHED,
            )
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
