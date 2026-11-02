# 地点与文化资料 · 业务规则
"""本模块唯一允许写业务判断的地方。router 不许写 if/else 业务逻辑。

拥有数据：Place, PlaceFact, PlaceSource
明确不做：路线编排、发布
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.modules.catalog.models import Place, PlaceStatus
from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.schemas import PlaceDetailOut, PlaceFactOut, SourceOut


def _clean(value: str | None) -> str | None:
    """空串/纯空格等于没传：前端把输入框内容直接拼进 query 时很常见。"""
    if value is None:
        return None
    return value.strip() or None


class CatalogService:
    """依赖注入的官方入口；外部模块只能通过它访问本模块能力。"""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = CatalogRepository(db)

    def list_places(
        self,
        *,
        q: str | None = None,
        district: str | None = None,
        tag: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> tuple[list[Place], str | None]:
        """公开列表：关键词（名称/别名/简介）+ 区 + 主题，三个条件同时生效。"""
        limit = max(1, min(limit, MAX_LIMIT))
        after = decode_cursor(cursor) if cursor else None
        rows = self.repo.list_places(
            q=_clean(q),
            district=_clean(district),
            tag=_clean(tag),
            after=after,
            limit=limit + 1,  # 多取一条判断还有没有下一页
        )
        items = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more else None
        return items, next_cursor

    def get_place(self, place_id: uuid.UUID) -> PlaceDetailOut:
        place = self.repo.get_place(place_id)
        # 草稿按"不存在"处理：404 而不是 200 + status=draft，连存在性都不泄露（必读1 §5 第 9 条）。
        if place is None or place.status != PlaceStatus.VERIFIED:
            raise NotFound("地点不存在", code="PLACE_NOT_FOUND")
        return _detail_out(place)


def _detail_out(place: Place) -> PlaceDetailOut:
    """事实 → 来源的展开在这里做：契约里 PlaceFact.sources 是数组，库里存的是单条 source_id。

    只回显挂了来源的 fact：必读1「数据要求」与 docs/01 §2.2 都写明
    「没有来源的 fact 不进公开内容」，所以内部备忘类说法连 sources=[] 都不该出现在这里。
    """
    visible_facts = [fact for fact in place.facts if fact.source is not None]
    return PlaceDetailOut(
        id=place.id,
        name=place.name,
        district=place.district,
        lat=float(place.lat),
        lon=float(place.lon),
        summary=place.summary,
        culture_tags=list(place.culture_tags or []),
        status=str(place.status),
        facts=[
            PlaceFactOut(
                id=fact.id,
                claim=fact.claim,
                confidence=str(fact.confidence),
                sources=[SourceOut.model_validate(fact.source)],
            )
            for fact in visible_facts
        ],
        open_hours=place.open_hours,
        open_hours_verified_at=place.open_hours_verified_at,
    )
