# 身份与会话 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.identity.models import Session as LoginSession
from app.modules.identity.models import User
from app.modules.moderation.models import Publication, PublicationState
from app.modules.routes.models import RouteDraft, RouteVersion


class IdentityRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_user_by_username(self, username: str) -> User | None:
        """按小写比较，正好命中 uq_users_username_lower 这个函数唯一索引。"""
        stmt = select(User).where(func.lower(User.username) == username.lower())
        return self.db.scalar(stmt)

    def get_user(self, user_id: uuid.UUID) -> User | None:
        return self.db.get(User, user_id)

    def list_published_versions(
        self, user_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, RouteVersion, datetime | None]]:
        """某个作者「当前处于发布态」的路线版本，一行一条路线。

        三个容易做错的地方：
        1. 「已发布」只看 publications.state = PUBLISHED。不能用
           route_versions.route_id 非空代替：发布时回填的 route_id 在下线后不会被
           清空，用它当判据会让已下线的路线继续出现在主页上。
        2. 发布接口保证同一 route 同时最多一条 PUBLISHED，但历史行可能还留着
           UNPUBLISHED / ARCHIVED，所以 state 条件必须写进 where。
        3. 跨模块直接读 moderation/routes 的 model：这两个模块都在顶层 import
           identity.service，identity 反向 import 它们的 service 会成环。
           等它们提供公开读服务后，把这段换成 service 调用并删掉这两个 import。
        """
        stmt = (
            select(Publication.route_id, RouteVersion, Publication.published_at)
            .join(RouteVersion, RouteVersion.id == Publication.version_id)
            .join(RouteDraft, RouteDraft.id == RouteVersion.draft_id)
            .where(
                Publication.state == PublicationState.PUBLISHED,
                RouteDraft.owner_id == user_id,
            )
            .order_by(
                Publication.published_at.desc().nulls_last(),
                Publication.route_id.desc(),
            )
        )
        return [(row[0], row[1], row[2]) for row in self.db.execute(stmt)]

    def add_user(self, user: User) -> User:
        self.db.add(user)
        self.db.flush()
        return user

    def add_session(self, session: LoginSession) -> LoginSession:
        self.db.add(session)
        self.db.flush()
        return session

    def get_session(self, session_id: uuid.UUID) -> LoginSession | None:
        return self.db.get(LoginSession, session_id)

    def revoke(self, session: LoginSession, when: datetime) -> None:
        session.revoked_at = when
        self.db.flush()

    def revoke_all_for_user(self, user_id: uuid.UUID, when: datetime) -> int:
        """只动未撤销的会话，重复调用是幂等的（已撤销的时间戳不被覆盖）。"""
        stmt = select(LoginSession).where(
            LoginSession.user_id == user_id, LoginSession.revoked_at.is_(None)
        )
        sessions = list(self.db.scalars(stmt))
        for row in sessions:
            row.revoked_at = when
        self.db.flush()
        return len(sessions)
