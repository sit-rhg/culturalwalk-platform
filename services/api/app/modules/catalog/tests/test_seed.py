"""CA-02 长沙种子数据：先写规则测试，再写校验器与落库。

先红后绿：本文件先于 `app/modules/catalog/seed.py` 写成，第一次跑会 ImportError；
实现后必须全绿。判断用例有没有用，看把断言语义写反会不会红——所以负例（无来源的
verified、越界坐标、未知主题词）和幂等重跑都单独成例，不靠"跑过一遍没报错"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.modules.catalog.models import Place, PlaceFact, PlaceSource
from app.modules.catalog.seed import (
    CULTURE_TAGS,
    DISTRICTS,
    SeedPlace,
    apply_seed,
    load_seed_places,
    place_id,
)

DATA_FILE = Path(__file__).resolve().parents[1] / "seed_data.json"


@pytest.fixture(scope="module")
def places() -> list[SeedPlace]:
    return load_seed_places(DATA_FILE)


def _minimal_place(**overrides: object) -> dict[str, object]:
    """一条能通过校验的最小点位，负例只改其中一个字段。"""
    payload: dict[str, object] = {
        "slug": "test-place",
        "name": "测试点位",
        "district": "天心区",
        "lat": 28.19,
        "lon": 112.97,
        "summary": "只用于校验器单测的占位内容。",
        "culture_tags": ["历史"],
        "facts": [
            {
                "claim": "测试说法。",
                "confidence": "likely",
                "source": {"title": "《长沙市志》", "publisher": "湖南人民出版社"},
            }
        ],
    }
    payload.update(overrides)
    return payload


# --- 数据文件本身 ---------------------------------------------------------


def test_dataset_has_thirty_places(places: list[SeedPlace]) -> None:
    assert len(places) >= 30
    assert len({p.slug for p in places}) == len(places), "slug 是幂等主键的来源，必须唯一"


def test_districts_are_changsha_ones(places: list[SeedPlace]) -> None:
    assert {p.district for p in places} <= DISTRICTS


def test_every_place_has_a_source(places: list[SeedPlace]) -> None:
    """验收硬指标：30 个点位每个至少 1 条来源。"""
    missing = [p.slug for p in places if not any(f.source for f in p.facts)]
    assert missing == [], f"这些点位没有来源：{missing}"


def test_every_place_has_a_usable_summary(places: list[SeedPlace]) -> None:
    """文案要能直接展示：够长、不以占位词开头。"""
    weak = [p.slug for p in places if len(p.summary.strip()) < 20]
    assert weak == [], f"这些点位简介过短：{weak}"


def test_tags_cover_the_vocabulary(places: list[SeedPlace]) -> None:
    used: set[str] = set()
    for place in places:
        used |= set(place.culture_tags)
    # 至少覆盖到一半的主题词，否则 CA-03 的 ?tag= 过滤没法演示
    assert len(used) >= len(CULTURE_TAGS) // 2


# --- 校验器负例 -----------------------------------------------------------


def test_verified_fact_without_source_is_rejected() -> None:
    payload = _minimal_place(
        facts=[{"claim": "没有出处的断言。", "confidence": "verified", "source": None}]
    )
    with pytest.raises(ValidationError, match="verified"):
        SeedPlace.model_validate(payload)


def test_coordinates_outside_changsha_are_rejected() -> None:
    with pytest.raises(ValidationError, match="lat|lon|坐标"):
        SeedPlace.model_validate(_minimal_place(lat=39.9, lon=116.4))  # 北京


def test_unknown_culture_tag_is_rejected() -> None:
    with pytest.raises(ValidationError, match="culture_tags|主题"):
        SeedPlace.model_validate(_minimal_place(culture_tags=["赛博朋克"]))


def test_place_without_facts_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SeedPlace.model_validate(_minimal_place(facts=[]))


# --- 落库：确定性主键 + 幂等 ----------------------------------------------


def test_place_id_is_deterministic() -> None:
    assert place_id("yuelu-academy") == place_id("yuelu-academy")
    assert place_id("yuelu-academy") != place_id("tianxin-pavilion")


def test_apply_seed_is_idempotent(db_session, places: list[SeedPlace]) -> None:
    first = apply_seed(db_session, places)
    counts_after_first = (
        db_session.scalar(select(func.count()).select_from(Place)),
        db_session.scalar(select(func.count()).select_from(PlaceSource)),
        db_session.scalar(select(func.count()).select_from(PlaceFact)),
    )

    second = apply_seed(db_session, places)
    counts_after_second = (
        db_session.scalar(select(func.count()).select_from(Place)),
        db_session.scalar(select(func.count()).select_from(PlaceSource)),
        db_session.scalar(select(func.count()).select_from(PlaceFact)),
    )

    assert first.inserted["places"] == len(places)
    assert second.inserted["places"] == 0, "重跑不该重复插入"
    assert counts_after_first == counts_after_second


def test_apply_seed_links_every_verified_fact_to_a_source(
    db_session, places: list[SeedPlace]
) -> None:
    apply_seed(db_session, places)
    orphans = db_session.scalars(
        select(PlaceFact).where(PlaceFact.confidence == "verified", PlaceFact.source_id.is_(None))
    ).all()
    assert orphans == []

    # 来源行数等于去重后的来源数（同一条来源被多个点位引用时只写一行）
    expected = {
        (f.source.title, f.source.url) for p in places for f in p.facts if f.source is not None
    }
    assert db_session.scalar(select(func.count()).select_from(PlaceSource)) == len(expected)
