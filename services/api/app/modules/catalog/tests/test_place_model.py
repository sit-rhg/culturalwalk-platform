"""CA-01 地点与来源模型的用例。

不连真实 Postgres：表结构、约束、级联行为都在 conftest 的 SQLite 内存库上验证；
「迁移能在 PG 上跑通」由 CI 的 `alembic upgrade head → downgrade base → upgrade head` 负责。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Table, exc
from sqlalchemy.orm import Session

from app.modules.catalog.models import (
    FactConfidence,
    Place,
    PlaceFact,
    PlaceSource,
    PlaceStatus,
)


def make_place(**overrides: object) -> Place:
    """一个最小可用的长沙点位，用例只覆盖自己关心的字段。"""
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "name": "岳麓书院",
        "lat": Decimal("28.186000"),
        "lon": Decimal("112.952000"),
        "district": "岳麓区",
        "culture_tags": ["历史", "建筑"],
    }
    fields.update(overrides)
    return Place(**fields)


def make_source(**overrides: object) -> PlaceSource:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "title": "《岳麓书院志》",
        "publisher": "湖南人民出版社",
    }
    fields.update(overrides)
    return PlaceSource(**fields)


# --------------------------------------------------------------------------
# 表结构
# --------------------------------------------------------------------------
def test_tables_are_declared() -> None:
    assert {"places", "place_sources", "place_facts"} <= set(Place.metadata.tables)


def test_places_carries_everything_the_contract_needs() -> None:
    """openapi 的 Place / PlaceDetail 要的字段必须都有位置放。"""
    cols = set(Place.__table__.c.keys())
    assert {
        "id",
        "name",
        "alias",
        "lat",
        "lon",
        "crs",
        "district",
        "summary",
        "culture_tags",
        "open_hours",
        "open_hours_verified_at",
        "status",
    } <= cols


def test_place_status_defaults_to_draft() -> None:
    assert Place.__table__.c.status.default.arg is PlaceStatus.DRAFT


def test_fact_confidence_defaults_to_unverified() -> None:
    """没写来源的说法默认就是「待核实」，不能默认成确定结论。"""
    assert PlaceFact.__table__.c.confidence.default.arg is FactConfidence.UNVERIFIED


def test_enums_stored_as_value_not_name() -> None:
    # `__table__.c.x.type` 的静态类型是通用的 TypeEngine，只有运行期才是 Enum。
    # 本用例断言的恰恰是运行期类型，所以这里放宽静态检查，别把断言弱化成只查列名。
    status_type = cast(Any, Place.__table__.c.status.type)
    assert isinstance(status_type, SAEnum)
    assert status_type.native_enum is False, "要存 VARCHAR，不然加状态就得写迁移"
    assert status_type.enums == ["draft", "verified"]
    assert cast(Any, PlaceFact.__table__.c.confidence.type).enums == [
        "verified",
        "likely",
        "unverified",
    ]


def test_no_cross_module_foreign_keys() -> None:
    # users 表属于 identity 模块：按约定跨模块只存 ID、不建物理外键。
    assert not [fk for column in Place.__table__.c for fk in column.foreign_keys]
    # 模块内部的父子关系才建外键
    assert {fk.target_fullname for fk in PlaceFact.__table__.c.place_id.foreign_keys} == {
        "places.id"
    }
    assert {fk.target_fullname for fk in PlaceFact.__table__.c.source_id.foreign_keys} == {
        "place_sources.id"
    }


def test_source_fk_degrades_instead_of_deleting_the_claim() -> None:
    """来源被删时只把出处置空，说法本身留下（从此按「待核实」展示）。"""
    (fk,) = PlaceFact.__table__.c.source_id.foreign_keys
    assert fk.ondelete == "SET NULL"


def test_culture_tags_use_a_gin_index_on_postgres() -> None:
    """CA-03 的 `?tag=` 会变成 `culture_tags @> '["历史"]'`，B-tree 用不上。"""
    table = cast(Table, Place.__table__)
    (index,) = [idx for idx in table.indexes if idx.name == "ix_places_culture_tags"]
    assert index.dialect_options["postgresql"]["using"] == "gin"


# --------------------------------------------------------------------------
# 验收：一条地点可挂多条来源
# --------------------------------------------------------------------------
def test_one_place_can_carry_multiple_sources(db_session: Session) -> None:
    place = make_place()
    book = make_source(title="《岳麓书院志》", url=None)
    site = make_source(
        title="岳麓书院官网 · 书院简介",
        url="https://example.org/yuelu",
        publisher="岳麓书院",
    )

    db_session.add_all([place, book, site])
    db_session.add_all(
        [
            PlaceFact(
                id=uuid.uuid4(),
                place=place,
                claim="岳麓书院始建于北宋开宝九年（976 年）",
                confidence=FactConfidence.VERIFIED,
                source=book,
            ),
            PlaceFact(
                id=uuid.uuid4(),
                place=place,
                claim="书院现存的讲堂为清代重建",
                confidence=FactConfidence.VERIFIED,
                source=site,
            ),
        ]
    )
    db_session.commit()
    db_session.expire_all()

    loaded = db_session.get(Place, place.id)
    assert loaded is not None
    assert len(loaded.facts) == 2, "两条说法都要挂在这个点位上"
    # 这正是「一条地点可挂多条来源」：两个说法指向两个不同的来源对象
    titles = {fact.source.title for fact in loaded.facts if fact.source is not None}
    assert titles == {
        "《岳麓书院志》",
        "岳麓书院官网 · 书院简介",
    }


def test_fact_can_live_without_a_source(db_session: Session) -> None:
    """查不到出处的说法允许入库，但只能标 unverified / likely。"""
    place = make_place()
    db_session.add(place)
    db_session.add(
        PlaceFact(
            id=uuid.uuid4(),
            place_id=place.id,
            claim="本地老人说这里曾是书院的藏书楼",
            confidence=FactConfidence.LIKELY,
        )
    )
    db_session.commit()

    (fact,) = db_session.query(PlaceFact).all()
    assert fact.source_id is None
    assert fact.confidence is FactConfidence.LIKELY


# --------------------------------------------------------------------------
# 负例：违反红线要当场失败，而不是悄悄写进去
# --------------------------------------------------------------------------
def test_verified_fact_without_source_is_rejected(db_session: Session) -> None:
    """产品红线：标注成 verified 就必须有出处，否则等于把编的内容当事实。"""
    place = make_place()
    db_session.add(place)
    db_session.add(
        PlaceFact(
            id=uuid.uuid4(),
            place_id=place.id,
            claim="书院建于唐代",
            confidence=FactConfidence.VERIFIED,
            source_id=None,
        )
    )
    with pytest.raises(exc.IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_unknown_confidence_is_rejected(db_session: Session) -> None:
    """枚举值写错要当场炸，不能悄悄存成非法字符串。"""
    place = make_place()
    db_session.add(place)
    db_session.add(
        PlaceFact(
            id=uuid.uuid4(),
            place_id=place.id,
            claim="随便写",
            # 故意塞一个非法枚举值：这里就是要造出脏数据，所以显式绕过静态检查
            confidence=cast(Any, "probably"),
        )
    )
    with pytest.raises(exc.StatementError) as excinfo:
        # validate_strings=True：绑参到 SQL 时才校验，脏值进不了库
        db_session.commit()
    assert "factconfidence" in str(excinfo.value), "报错要说清是哪个枚举的取值非法"
    db_session.rollback()


def test_place_requires_coordinates(db_session: Session) -> None:
    """坐标是筛选与地图的硬前提，缺了就不该能存进去。"""
    db_session.add(Place(id=uuid.uuid4(), name="没有坐标的点位"))
    with pytest.raises(exc.IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_deleting_place_removes_its_facts(db_session: Session) -> None:
    """删点位要把说法一起带走，否则会留下指向空气的孤儿行。"""
    place = make_place()
    db_session.add(place)
    db_session.add(
        PlaceFact(
            id=uuid.uuid4(),
            place=place,
            claim="一条会被级联删除的说法",
            confidence=FactConfidence.LIKELY,
        )
    )
    db_session.commit()

    db_session.delete(place)
    db_session.commit()

    assert db_session.query(PlaceFact).count() == 0
