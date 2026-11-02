# 地点与文化资料 · 业务规则
"""本模块唯一允许写业务判断的地方。router 不许写 if/else 业务逻辑。

拥有数据：Place, PlaceFact, PlaceSource
明确不做：路线编排、发布
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.errors import NotFound
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.modules.audit import service as audit
from app.modules.catalog.models import (
    FactConfidence,
    Place,
    PlaceFact,
    PlaceSource,
    PlaceStatus,
)
from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.schemas import (
    PlaceCreateIn,
    PlaceDetailOut,
    PlaceFactCreateIn,
    PlaceFactOut,
    SourceOut,
)


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

    # ---------- 写 ----------

    def create_place(
        self, payload: PlaceCreateIn, *, actor: CurrentUser, request_id: str | None = None
    ) -> Place:
        """新增地点，落在 draft：编辑器补完来源、核对坐标后才由审核流程置 verified。

        坐标用 Decimal 而不是 float 落库：经纬度是标识符，float 的末位误差会让点位漂移。
        """
        place = Place(
            name=payload.name,
            district=_clean(payload.district),
            lat=Decimal(str(payload.lat)),
            lon=Decimal(str(payload.lon)),
            crs=payload.crs,
            summary=_clean(payload.summary),
            culture_tags=list(payload.culture_tags),
            status=PlaceStatus.DRAFT,
        )
        self.repo.add_place(place)
        audit.write(
            self.db,
            action="place.create",
            object_type="place",
            object_id=str(place.id),
            actor_id=actor.id,
            actor_username=actor.username,
            after_hash=audit.AuditService.digest(payload.model_dump()),
            request_id=request_id,
        )
        return place

    def add_fact(
        self,
        place_id: uuid.UUID,
        payload: PlaceFactCreateIn,
        *,
        actor: CurrentUser,
        request_id: str | None = None,
    ) -> PlaceFactOut:
        """给地点补一条文化资料，同时落它的来源。

        来源必填由 schema 保证（422）；这里再判一次地点存在，避免给不存在的点位
        挂一条永远读不到的 fact。
        """
        place = self.repo.get_place(place_id)
        if place is None:
            raise NotFound("地点不存在", code="PLACE_NOT_FOUND")

        source = PlaceSource(
            title=payload.source.title,
            url=payload.source.url,
            publisher=payload.source.publisher,
        )
        fact = PlaceFact(
            place_id=place.id,
            claim=payload.claim,
            confidence=FactConfidence(payload.confidence),
            source=source,
        )
        self.repo.add_source(source)
        self.repo.add_fact(fact)
        audit.write(
            self.db,
            action="place.fact.create",
            object_type="place_fact",
            object_id=str(fact.id),
            actor_id=actor.id,
            actor_username=actor.username,
            after_hash=audit.AuditService.digest(
                {
                    "place_id": str(place.id),
                    "claim": payload.claim,
                    "confidence": payload.confidence,
                    "source_url": payload.source.url,
                }
            ),
            request_id=request_id,
        )
        return PlaceFactOut(
            id=fact.id,
            claim=fact.claim,
            confidence=fact.confidence.value,
            sources=[SourceOut.model_validate(source)],
        )


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
