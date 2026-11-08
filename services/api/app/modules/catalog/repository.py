# 地点与文化资料 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session, lazyload

from app.modules.catalog.models import (
    IdempotencyRecord,
    Place,
    PlaceFact,
    PlaceSource,
    PlaceStatus,
)


def _escape_like(value: str) -> str:
    """q 是用户输入，% 和 _ 必须当普通字符，否则一次查询能把整张表捞出来。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class CatalogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_places(
        self,
        *,
        q: str | None,
        district: str | None,
        tag: str | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Place]:
        """按 (created_at, id) 倒序取一页；after 为上一页最后一条的位置。"""
        # 列表用不到 facts：把 selectin 拉回默认 lazy，省掉那次额外查询；
        # 详情接口走 db.get()，facts/source 照常按 relationship 配置加载。
        #
        # 公开读接口只给已发布：草稿点位不能出现在列表里（必读1 §5 第 9 条）。
        stmt = (
            select(Place).options(lazyload(Place.facts)).where(Place.status == PlaceStatus.VERIFIED)
        )
        if q:
            pattern = f"%{_escape_like(q)}%"
            stmt = stmt.where(
                or_(
                    Place.name.ilike(pattern, escape="\\"),
                    Place.alias.ilike(pattern, escape="\\"),
                    Place.summary.ilike(pattern, escape="\\"),
                )
            )
        if district:
            stmt = stmt.where(Place.district == district)
        if tag:
            stmt = stmt.where(self._tag_condition(tag))
        if after is not None:
            created_at, place_id = after
            # 不用行值比较，保证 SQLite（本地/CI 测试）与 PostgreSQL 行为一致
            stmt = stmt.where(
                or_(
                    Place.created_at < created_at,
                    and_(Place.created_at == created_at, Place.id < place_id),
                )
            )
        stmt = stmt.order_by(Place.created_at.desc(), Place.id.desc()).limit(limit)
        return list(self.db.scalars(stmt))

    def get_place(self, place_id: uuid.UUID) -> Place | None:
        return self.db.get(Place, place_id)

    def get_fact(self, fact_id: uuid.UUID) -> PlaceFact | None:
        return self.db.get(PlaceFact, fact_id)

    def get_idempotency(
        self, *, actor_id: uuid.UUID, scope: str, key: str
    ) -> IdempotencyRecord | None:
        """按 (发起人, 端点, key) 找首次记录；key 是客户端给的，不能跨人复用。"""
        stmt = select(IdempotencyRecord).where(
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.key == key,
        )
        return self.db.scalars(stmt).first()

    def add_place(self, place: Place) -> Place:
        self.db.add(place)
        self.db.flush()
        return place

    def add_source(self, source: PlaceSource) -> PlaceSource:
        self.db.add(source)
        self.db.flush()
        return source

    def add_fact(self, fact: PlaceFact) -> PlaceFact:
        self.db.add(fact)
        self.db.flush()
        return fact

    def _tag_condition(self, tag: str):
        """`?tag=` 的方言分支：两边存的都是 JSON 数组，但过滤语法不通用。

        - PostgreSQL：culture_tags 是 jsonb，用 `@>` 走 models 里建好的 GIN 索引；
        - SQLite：没有 jsonb，用 json_each 展开数组（Python 自带 SQLite 已编译 JSON1）。

        注意不能写 `Place.culture_tags.contains([tag])`：SQLAlchemy 会生成
        `LIKE '%' || :param::JSONB || '%'`，psycopg 在绑定时直接报 InvalidTextRepresentation。
        """
        if self.db.get_bind().dialect.name == "postgresql":
            return text("places.culture_tags @> CAST(:tags AS jsonb)").bindparams(
                tags=json.dumps([tag], ensure_ascii=False)
            )
        return text(
            "EXISTS (SELECT 1 FROM json_each(places.culture_tags) WHERE json_each.value = :tag)"
        ).bindparams(tag=tag)
