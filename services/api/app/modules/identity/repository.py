# 身份与会话 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.modules.identity.models import Session as LoginSession
from app.modules.identity.models import User
from app.modules.identity.schemas import RouteSummaryOut


class IdentityRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_user_by_username(self, username: str) -> User | None:
        """按小写比较，正好命中 uq_users_username_lower 这个函数唯一索引。"""
        stmt = select(User).where(func.lower(User.username) == username.lower())
        return self.db.scalar(stmt)

    def get_user(self, user_id: uuid.UUID) -> User | None:
        return self.db.get(User, user_id)

    def list_published_routes(self, user_id: uuid.UUID) -> list[RouteSummaryOut]:
        """某个作者的已发布路线摘要。

        上游表还没落地：routes 模块的 routes / route_versions 要等 RT-01/RT-02
        （现在 app/modules/routes/models.py 整段还是注释）。所以这里先返回空列表，
        但签名、出参 DTO 和"只查已发布"的口径已经定死：表落地后只替换本函数体，
        不改调用方、不改契约。别为了让它有返回值去 join 别的模块的表。
        """
        return []

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
