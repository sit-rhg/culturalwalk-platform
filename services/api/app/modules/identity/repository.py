# 身份与会话 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.identity.models import Session as LoginSession
from app.modules.identity.models import User

# ⚠️ 已知的跨模块 import models，AGENTS.md 原则上禁止；这里是唯一一处，理由与 community 相同：
# routes 没有「按作者列已发布路线」的公开读服务，而 /users/{id}/profile 必须返回
# published_routes。契约与社区列表都按「route_versions.route_id 非空 = 已发布」判定，
# 这里只读 routes 的两张表（不改任何数据）；等 routes 提供公开读服务后换成 service 调用。
from app.modules.routes.models import RouteDraft, RouteVersion


@dataclass(frozen=True)
class PublishedRoute:
    """主页要的一条已发布路线，只带渲染卡片需要的字段。

    service 拿到的是这个，而不是 RouteVersion：跨模块的 ORM 实体止步于 repository，
    业务层不用知道 routes 的表长什么样。
    """

    route_id: uuid.UUID
    draft_title: str
    snapshot: dict[str, Any]


class IdentityRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_user_by_username(self, username: str) -> User | None:
        """按小写比较，正好命中 uq_users_username_lower 这个函数唯一索引。"""
        stmt = select(User).where(func.lower(User.username) == username.lower())
        return self.db.scalar(stmt)

    def get_user(self, user_id: uuid.UUID) -> User | None:
        return self.db.get(User, user_id)

    def published_routes(self, *, owner_id: uuid.UUID) -> list[PublishedRoute]:
        """某作者已发布的路线，每条只取最新版本，按发布时间倒序。

        发布时 route_id 会回填到该草稿已有的所有版本上，所以同一 route 会有多行；
        先按时间倒序扫一遍，每个 route 第一次出现的就是最新版本。
        """
        stmt = (
            select(RouteVersion, RouteDraft.title)
            .join(RouteDraft, RouteDraft.id == RouteVersion.draft_id)
            .where(
                RouteVersion.route_id.is_not(None),
                RouteDraft.owner_id == owner_id,
            )
            .order_by(RouteVersion.created_at.desc(), RouteVersion.id.desc())
        )
        latest: dict[uuid.UUID, PublishedRoute] = {}
        for version, draft_title in self.db.execute(stmt):
            if version.route_id is None:  # where 已过滤，仅让类型收敛
                continue
            snapshot = version.snapshot if isinstance(version.snapshot, dict) else {}
            latest.setdefault(
                version.route_id,
                PublishedRoute(
                    route_id=version.route_id,
                    draft_title=draft_title,
                    snapshot=snapshot,
                ),
            )
        return list(latest.values())

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
