# 地点与文化资料 · 业务规则
"""本模块唯一允许写业务判断的地方。router 不许写 if/else 业务逻辑。

拥有数据：Place, PlaceFact, PlaceSource
明确不做：路线编排、发布
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.modules.audit import service as audit
from app.modules.catalog.models import (
    FactConfidence,
    IdempotencyRecord,
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
    SourceIn,
    SourceOut,
)

IDEMPOTENCY_SCOPE_PLACE = "place.create"
IDEMPOTENCY_SCOPE_FACT = "place.fact.create"


def _request_fingerprint(payload: object) -> str:
    """请求体指纹：同 key 不同体是客户端错误，不能当重放把别的结果返回去。

    用规范化 JSON（键排序、UTF-8）算 sha256，避免 dict 顺序影响判定。
    """
    canonical = json.dumps(
        payload.model_dump(mode="json"),  # type: ignore[attr-defined]
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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

    def place_names(self, place_ids: Iterable[str]) -> dict[str, str]:
        """把快照里的 place_id 批量翻成地名，审核页的版本对照才看得出换了哪个点。

        非法 id 和查不到的 id 直接略过，前端会退回显示截断的 id。
        """
        parsed: list[uuid.UUID] = []
        for raw in place_ids:
            try:
                parsed.append(uuid.UUID(str(raw)))
            except ValueError:
                continue
        return self.repo.place_names(parsed)

    # ---------- 幂等 ----------

    def _start_idempotent(
        self,
        *,
        actor: CurrentUser,
        scope: str,
        key: str | None,
        payload: object,
        object_type: str,
        load: Callable[[IdempotencyRecord], Any | None],
    ) -> tuple[IdempotencyRecord | None, Any | None]:
        """返回 (待回填的预占记录, 重放结果)，两者至多一个非 None。

        预占必须发生在创建对象之前：两个并发同 key 请求都通过「查不到」之后，
        只能由 (actor_id, scope, key) 的唯一约束裁决，败者回退到读已提交的记录。
        预占行在事务提交前对别人不可见，所以不会把半成品响应返回给任何人。
        """
        if not key:
            return None, None
        # CurrentUser.id 来自 JWT 的 sub，是字符串；列是 Uuid，查询前要转回来。
        actor_id = uuid.UUID(str(actor.id))
        request_hash = _request_fingerprint(payload)
        record = self.repo.get_idempotency(actor_id=actor_id, scope=scope, key=key)
        if record is not None:
            return None, self._replay(record, request_hash=request_hash, load=load)
        try:
            with self.db.begin_nested():
                record = IdempotencyRecord(
                    actor_id=actor_id,
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    object_type=object_type,
                )
                self.db.add(record)
        except IntegrityError:
            # 并发同 key：胜者已经提交，读到它的结果再按指纹判断是重放还是冲突。
            record = self.repo.get_idempotency(actor_id=actor_id, scope=scope, key=key)
            if record is None:
                raise
            return None, self._replay(record, request_hash=request_hash, load=load)
        return record, None

    def _replay(
        self,
        record: IdempotencyRecord,
        *,
        request_hash: str,
        load: Callable[[IdempotencyRecord], Any | None],
    ) -> Any:
        if record.request_hash != request_hash:
            raise Conflict(
                "这个 Idempotency-Key 已经用于另一个请求体，请换一个新的",
                code="IDEMPOTENCY_KEY_REUSED",
            )
        if record.object_id is None:
            # 预占行只有在事务提交后才可见，正常路径不会走到这里；
            # 真到了说明上一次提交把半成品留下了，宁可让客户端重试也不要返回空对象。
            raise Conflict("上一次请求仍在处理中，请稍后重试", code="IDEMPOTENCY_IN_PROGRESS")
        obj = load(record)
        if obj is None:
            raise Conflict(
                "幂等键对应的对象已被删除，请换一个新的 Idempotency-Key",
                code="IDEMPOTENCY_KEY_REUSED",
            )
        return obj

    def _finish_idempotent(self, record: IdempotencyRecord | None, object_id: uuid.UUID) -> None:
        if record is None:
            return
        record.object_id = object_id
        self.db.flush()

    # ---------- 写 ----------

    def create_place(
        self,
        payload: PlaceCreateIn,
        *,
        actor: CurrentUser,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Place:
        """新增地点，落在 draft：编辑器补完来源、核对坐标后才由审核流程置 verified。

        坐标用 Decimal 而不是 float 落库：经纬度是标识符，float 的末位误差会让点位漂移。
        带 Idempotency-Key 时先查首次记录：命中直接返回同一个 Place，不再写第二行。
        """
        record, replayed = self._start_idempotent(
            actor=actor,
            scope=IDEMPOTENCY_SCOPE_PLACE,
            key=idempotency_key,
            payload=payload,
            object_type="place",
            load=lambda item: self.repo.get_place(item.object_id) if item.object_id else None,
        )
        if replayed is not None:
            return replayed

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
        self._finish_idempotent(record, place.id)
        return place

    def add_fact(
        self,
        place_id: uuid.UUID,
        payload: PlaceFactCreateIn,
        *,
        actor: CurrentUser,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> PlaceFactOut:
        """给地点补一条文化资料，同时落它的来源。

        来源必填由 schema 保证（422）；这里再判一次地点存在，避免给不存在的点位
        挂一条永远读不到的 fact。幂等重放返回同一个 fact，不会重复挂来源。
        """
        record, replayed = self._start_idempotent(
            actor=actor,
            scope=IDEMPOTENCY_SCOPE_FACT,
            key=idempotency_key,
            payload=payload,
            object_type="place_fact",
            load=lambda item: self.repo.get_fact(item.object_id) if item.object_id else None,
        )
        if replayed is not None:
            return _fact_out(replayed)

        place = self.repo.get_place(place_id)
        if place is None:
            raise NotFound("地点不存在", code="PLACE_NOT_FOUND")

        source, created = _source_for(self, payload.source)
        # 产品红线仍然在：标成「已核实」却没给来源，就是把模型编的内容当事实写。
        # schema 不再强制（契约里 source 可选），所以这条规则必须留在这里，
        # 而且给字段级说明，前端能直接指着 source 输入框提示（D6）。
        if payload.confidence == "verified" and source is None:
            raise ValidationFailed(
                "已核实的说法必须带来源",
                details=[{"field": "source", "reason": "confidence=verified 时必须提供来源"}],
            )

        fact = PlaceFact(
            place_id=place.id,
            claim=payload.claim,
            confidence=FactConfidence(payload.confidence),
            source=source,
        )
        # 没有来源的 fact 照样入库：它是内部备忘/待补资料，读接口不会把它放出去。
        if created and source is not None:
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
                    "source_url": source.url if source is not None else None,
                }
            ),
            request_id=request_id,
        )
        self._finish_idempotent(record, fact.id)
        return _fact_out(fact)


def _source_for(
    service: CatalogService, incoming: SourceIn | None
) -> tuple[PlaceSource | None, bool]:
    """来源去重 + 补齐溯源字段（D7/D8）。返回 (来源, 是否新建)。

    同一本书/同一个链接被补了第二条 fact 时不再新插一行，否则「这条来源支撑
    了多少条说法」永远算不出来；accessed_at 记「这次核对是哪一天」（网页会改版），
    license 留着以后判断能不能转载。
    """
    if incoming is None:
        return None, False
    title_text = (incoming.title or "").strip()
    url_text = (incoming.url or "").strip()
    if not (title_text or url_text or (incoming.publisher or "").strip() or incoming.license):
        # 传了个空对象等于没填，别凭空造一行「未命名来源」。
        return None, False
    # place_sources.title 只有 255 列宽，而 url 允许 2048：直接把长链接当标题
    # 写库会在真实数据库上截断报错，所以只在标题长度内取用（去重键也用它）。
    title = (title_text or url_text or "未命名来源")[:255]
    existing = service.repo.find_source(title=title, url=url_text or None)
    if existing is not None:
        # 老行缺授权信息时用新填的补上；已有值不覆盖（不拿新说法改历史记录）。
        if incoming.license and not existing.license:
            existing.license = incoming.license
        return existing, False
    return (
        PlaceSource(
            title=title,
            url=url_text or None,
            publisher=incoming.publisher,
            accessed_at=datetime.now(UTC),
            license=incoming.license,
        ),
        True,
    )


def _fact_out(fact: PlaceFact) -> PlaceFactOut:
    """fact → 契约形状：库里是单条 source，读接口是数组。"""
    return PlaceFactOut(
        id=fact.id,
        claim=fact.claim,
        confidence=str(fact.confidence),
        sources=[SourceOut.model_validate(fact.source)] if fact.source is not None else [],
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
        crs=place.crs,
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
