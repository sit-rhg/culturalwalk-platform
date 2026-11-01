# CA-02 长沙种子数据 · 校验与落库
"""把 `seed_data.json` 里的点位写进 places / place_sources / place_facts 三张表。

分成“数据 + 规则”两半：内容全在 JSON 里（方便非程序员改、diff 干净），这个模块只负责
把内容变成合法的行——字段、坐标系、主题词、来源红线都在这里挡一次，脚本和接口共用同一套
枚举与约束，不另造一套。

两条设计取舍：
1. **主键由 slug 派生（uuid5）**，所以重复执行是幂等的：第二次跑会整行跳过，既不会复制数据，
   也不会覆盖编辑在后台改过的内容。想刷新内容就先用 `DELETE` 清掉再灌，或将来加 `--update`。
2. **verified 必须有来源**——这条在数据库里是 CHECK 约束（ck_place_facts_verified_needs_source），
   这里先挡一遍，是为了让 `--check` 在不开库的情况下也能把问题报在第几个点位上。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.catalog.models import (
    FactConfidence,
    Place,
    PlaceFact,
    PlaceSource,
    PlaceStatus,
)

# docs/02 §2.2 给的是开放词表（`历史`/`非遗`/`民俗`/`建筑`/`文学`/`红色`…）。
# CA-03 要按 `?tag=` 过滤，词表不收口前端就得写死一堆同义词，所以这里定成受控集合：
# 新增主题要改这里 + 迁移 CHECK（若有）+ 前端筛选器，三处一起改。
CULTURE_TAGS: frozenset[str] = frozenset(
    {"历史", "建筑", "文学", "红色", "非遗", "民俗", "美食", "宗教", "自然", "工业", "教育"}
)

# 长沙市域：6 区 1 县 2 市（2023 年后含浏阳、宁乡）。
DISTRICTS: frozenset[str] = frozenset(
    {"芙蓉区", "天心区", "开福区", "岳麓区", "雨花区", "望城区", "长沙县", "浏阳市", "宁乡市"}
)

# 坐标兜底范围：宁可宽一点（漏判），也不要因为边界把一个合法点位拦在门外。
# 真正的校验靠区县字段 + 人工复核，这里只挡住「把北京写成长沙」这类粗错。
LAT_RANGE = (Decimal("27.50"), Decimal("28.72"))
LON_RANGE = (Decimal("111.85"), Decimal("114.28"))

_NAMESPACE = uuid.NAMESPACE_URL


def place_id(slug: str) -> uuid.UUID:
    """slug → 稳定的地点主键。同一个 slug 永远得到同一个 UUID。"""
    return uuid.uuid5(_NAMESPACE, f"culturalwalk:place:{slug}")


def source_id(title: str, url: str | None) -> uuid.UUID:
    """来源按「标题 + URL」去重：同一条来源被多个点位引用时只写一行。"""
    return uuid.uuid5(_NAMESPACE, f"culturalwalk:source:{title}|{url or ''}")


def fact_id(slug: str, index: int) -> uuid.UUID:
    """说法按「点位 slug + 序号」派生；序号变了 id 就变，属可接受的代价。"""
    return uuid.uuid5(_NAMESPACE, f"culturalwalk:fact:{slug}:{index}")


class SeedSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=255)
    url: str | None = None
    publisher: str | None = Field(default=None, max_length=128)


class SeedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    claim: str = Field(min_length=1)
    confidence: FactConfidence = FactConfidence.UNVERIFIED
    source: SeedSource | None = None

    @model_validator(mode="after")
    def _verified_needs_source(self) -> SeedFact:
        # 与数据库 CHECK 同一条红线：verified 是把说法当事实写，必须能指到出处。
        if self.confidence is FactConfidence.VERIFIED and self.source is None:
            raise ValueError("confidence=verified 的说法必须带 source（数据库 CHECK 也会拒绝）")
        return self


class SeedPlace(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,60}$")
    name: str = Field(min_length=1, max_length=128)
    alias: str | None = Field(default=None, max_length=128)
    district: str
    lat: Decimal
    lon: Decimal
    summary: str = Field(min_length=10)
    culture_tags: list[str] = Field(min_length=1)
    status: PlaceStatus = PlaceStatus.VERIFIED
    facts: list[SeedFact] = Field(min_length=1)
    open_hours: str | None = None
    open_hours_verified_at: datetime | None = None

    @field_validator("lat", "lon", mode="before")
    @classmethod
    def _to_decimal(cls, value: Any) -> Decimal:
        # JSON 里是 float，直接交给 Decimal 会带二进制尾巴，先用 str 转一道。
        return Decimal(str(value))

    @field_validator("district")
    @classmethod
    def _known_district(cls, value: str) -> str:
        if value not in DISTRICTS:
            raise ValueError(f"district 必须是长沙的区县，收到：{value}")
        return value

    @field_validator("culture_tags")
    @classmethod
    def _known_tags(cls, value: list[str]) -> list[str]:
        unknown = [tag for tag in value if tag not in CULTURE_TAGS]
        if unknown:
            raise ValueError(f"culture_tags 里有未登记的主题：{unknown}")
        return list(dict.fromkeys(value))  # 去重但保持书写顺序，展示时顺序稳定

    @model_validator(mode="after")
    def _inside_changsha(self) -> SeedPlace:
        if not (LAT_RANGE[0] <= self.lat <= LAT_RANGE[1]):
            raise ValueError(f"lat={self.lat} 不在长沙纬度范围内 {LAT_RANGE}")
        if not (LON_RANGE[0] <= self.lon <= LON_RANGE[1]):
            raise ValueError(f"lon={self.lon} 不在长沙经度范围内 {LON_RANGE}")
        return self


@dataclass(slots=True)
class SeedReport:
    """一次灌库的结果。`inserted` 只统计本次真正写入的行，重跑时应当全是 0。"""

    inserted: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {"inserted": dict(self.inserted), "totals": dict(self.totals)}

    def format(self) -> str:
        parts = [
            f"{table}: 新增 {self.inserted.get(table, 0)} / 共 {count}"
            for table, count in self.totals.items()
        ]
        return "；".join(parts)


def load_seed_places(path: str | Path) -> list[SeedPlace]:
    """读 JSON 并逐条校验。任何一条不合格都整体失败，不写半截数据。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw["places"] if isinstance(raw, dict) else raw

    places = [SeedPlace.model_validate(item) for item in items]
    slugs = [place.slug for place in places]
    duplicated = {slug for slug in slugs if slugs.count(slug) > 1}
    if duplicated:
        raise ValueError(f"slug 重复：{sorted(duplicated)}")
    return places


def _existing_ids[T: Place | PlaceSource | PlaceFact](
    session: Session, model: type[T], ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """一次查完已存在的行，避免逐条 SELECT。三类表都有 id 主键，所以用一个类型参数收口。"""
    if not ids:
        return set()
    return set(session.scalars(select(model.id).where(model.id.in_(ids))).all())


def apply_seed(session: Session, places: list[SeedPlace]) -> SeedReport:
    """把已校验的点位写库。只插入不存在的行，调用方负责 commit/rollback。"""
    sources: dict[uuid.UUID, tuple[SeedPlace, SeedSource]] = {}
    for place in places:
        for fact in place.facts:
            if fact.source is not None:
                sid = source_id(fact.source.title, fact.source.url)
                sources.setdefault(sid, (place, fact.source))

    report = SeedReport()

    # 1. 来源（不依赖别的表，先写）
    existing = _existing_ids(session, PlaceSource, list(sources))
    new_sources = [
        PlaceSource(
            id=sid,
            title=src.title,
            url=src.url,
            publisher=src.publisher,
            accessed_at=datetime.now().astimezone(),
        )
        for sid, (_place, src) in sources.items()
        if sid not in existing
    ]
    session.add_all(new_sources)
    session.flush()
    report.inserted["sources"] = len(new_sources)
    report.totals["sources"] = len(sources)

    # 2. 点位
    existing = _existing_ids(session, Place, [place_id(p.slug) for p in places])
    new_places = [
        Place(
            id=place_id(p.slug),
            name=p.name,
            alias=p.alias,
            lat=p.lat,
            lon=p.lon,
            district=p.district,
            summary=p.summary,
            culture_tags=list(p.culture_tags),
            status=p.status,
            open_hours=p.open_hours,
            open_hours_verified_at=p.open_hours_verified_at,
        )
        for p in places
        if place_id(p.slug) not in existing
    ]
    session.add_all(new_places)
    session.flush()
    report.inserted["places"] = len(new_places)
    report.totals["places"] = len(places)

    # 3. 说法
    fact_rows = [
        PlaceFact(
            id=fact_id(place.slug, index),
            place_id=place_id(place.slug),
            claim=fact.claim,
            confidence=fact.confidence,
            source_id=(
                source_id(fact.source.title, fact.source.url) if fact.source is not None else None
            ),
        )
        for place in places
        for index, fact in enumerate(place.facts)
    ]
    existing = _existing_ids(session, PlaceFact, [row.id for row in fact_rows])
    new_facts = [row for row in fact_rows if row.id not in existing]
    session.add_all(new_facts)
    session.flush()
    report.inserted["facts"] = len(new_facts)
    report.totals["facts"] = len(fact_rows)

    return report
