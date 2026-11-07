# CulturalWalk API · 数据库会话
"""SQLAlchemy 2.0 同步会话。所有模块共用 Base，迁移由 P3 统一生成。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


engine = create_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    """请求级会话：提交在 `create_app` 的 commit_session 中间件里、响应发出之前完成。

    以前这里是 `yield db` 之后再 `commit()`。FastAPI 的 yield 依赖 teardown 跑在
    响应**之后**：commit 失败时用户已经收到 200，数据却没落库，日志里只剩一条
    静默丢数据的异常。会话挂到 `request.state.db`，由中间件在响应前提交；
    这里只负责异常路径回滚和关连接。
    """
    db = SessionLocal()
    request.state.db = db
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        # 走到这里还没提交，说明这次请求没成功（失败请求也在这一步回滚）。
        if db.in_transaction():
            db.rollback()
        db.close()


# router 里统一写 `db: DbDep`，不许自己在端点里 new Session。
DbDep = Annotated[Session, Depends(get_db)]
