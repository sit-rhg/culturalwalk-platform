# 身份与会话 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.modules.identity.models import Session as LoginSession
from app.modules.identity.models import User

# ⚠️ 已知的跨模块 import models，AGENTS.md 原则上禁止；这里是唯一一处，理由与 community 相同：
# routes 没有「按作者列已发布路线」的公开读服务，而 /users/{id}/profile 必须返回
# published_routes。这里只读 moderation/publications 与 routes 的两张表（不写任何数据）；
# 等 routes 提供公开读服务后换成 service 调用。
from app.modules.moderation.models import Publication, PublicationState
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
        """某作者「当前处于发布态」的路线，每条只取最新版本，按发布时间倒序。

        「已发布」以 publications.state = PUBLISHED 为准，**不能**用
        route_versions.route_id 非空代替：发布时回填的 route_id 在下线（unpublish
        只改 publications.state）之后不会清空，按 route_id 判断会让已下线的路线
        继续挂在公开主页上。

        同一 route 可能有多次发布记录（下线再发），先按发布时间倒序扫一遍，
        每个 route 第一次出现的那版就是当前对外展示的版本。
        """
        stmt = (
            select(Publication.route_id, RouteVersion, RouteDraft.title)
            .join(RouteVersion, RouteVersion.id == Publication.version_id)
            .join(RouteDraft, RouteDraft.id == RouteVersion.draft_id)
            .where(
                Publication.state == PublicationState.PUBLISHED,
                RouteDraft.owner_id == owner_id,
            )
            .order_by(
                Publication.published_at.desc().nulls_last(),
                RouteVersion.created_at.desc(),
                RouteVersion.id.desc(),
            )
        )
        latest: dict[uuid.UUID, PublishedRoute] = {}
        for route_id, version, draft_title in self.db.execute(stmt):
            snapshot = version.snapshot if isinstance(version.snapshot, dict) else {}
            latest.setdefault(
                route_id,
                PublishedRoute(
                    route_id=route_id,
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

    def revoke_if_active(self, session_id: uuid.UUID, when: datetime) -> bool:
        """条件更新式撤销：撤销与"判断还没撤销"在同一条 SQL 里完成。

        轮换 refresh 时若沿用"读出来 → 判 revoked_at 为空 → 写回"，
        两个并发请求可能都读到空、都写回、各自签发，最后留下两条有效会话。
        这里把判断下沉到 WHERE revoked_at IS NULL：只有真正改到行的那次返回 True，
        另一个请求 rowcount=0 直接 401，于是"同一枚 refresh 只能换一次"从
        "通常成立"变成"一定成立"（PG 的行锁 + 语句级原子性保证）。
        synchronize_session=False 是故意的：调用方拿到 False 就该结束，不该再用旧对象。
        """
        stmt = (
            update(LoginSession)
            .where(LoginSession.id == session_id, LoginSession.revoked_at.is_(None))
            .values(revoked_at=when)
            .execution_options(synchronize_session=False)
        )
        # Session.execute 的静态类型是 Result，rowcount 只在 CursorResult 上有；
        # 这条语句就是 DML，运行时拿到的必然是 CursorResult。
        result = cast(CursorResult[Any], self.db.execute(stmt))
        self.db.flush()
        return result.rowcount == 1

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
