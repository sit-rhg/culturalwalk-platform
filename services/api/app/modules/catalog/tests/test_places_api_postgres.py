"""CA-03 补测：`?tag=` 的 PostgreSQL 分支必须在真 PG 上验。

为什么单独一个文件：
- 内存 SQLite 上走的是 `json_each` 分支，PG 上走的是 `culture_tags @> CAST(:tags AS jsonb)`；
  两条 SQL 不可能被同一套用例同时覆盖。jsonb 包含运算符、GIN 索引这些只有真方言能验。
- 没有 Postgres 的机器上自动 skip；CI 的 backend job 带 PG service，会自动跑起来。

用例自建带随机后缀的数据、跑完删干净，不和库里已有的种子数据互相干扰。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, delete, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.db import Base, get_db
from app.main import create_app
from app.modules.catalog.models import (
    FactConfidence,
    Place,
    PlaceFact,
    PlaceSource,
    PlaceStatus,
)


def _catalog_tables() -> list[Table]:
    return [
        cast(Table, Place.__table__),
        cast(Table, PlaceSource.__table__),
        cast(Table, PlaceFact.__table__),
    ]


@pytest.fixture()
def pg_engine() -> Iterator[Engine]:
    """真 Postgres 引擎；连不上就 skip，不让本机没装库的人卡住。"""
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("需要 Postgres：当前 DATABASE_URL 不是 postgresql")

    engine = create_engine(settings.database_url, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 — 连不上就是 skip，不区分具体异常
        engine.dispose()
        pytest.skip(f"连不上 Postgres：{exc}")

    created_here = not inspect(engine).has_table("places")
    if created_here:
        Base.metadata.create_all(engine, tables=_catalog_tables())
    try:
        yield engine
    finally:
        if created_here:
            Base.metadata.drop_all(engine, tables=_catalog_tables())
        engine.dispose()


@pytest.fixture()
def pg_client(pg_engine: Engine) -> Iterator[TestClient]:
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
    app = create_app()

    def override_get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_tag_filter_pagination_and_detail_on_postgres(
    pg_client: TestClient, pg_engine: Engine
) -> None:
    suffix = uuid.uuid4().hex[:8]
    district = f"测试区{suffix}"
    tag = f"测试主题{suffix}"
    source_title = f"PG 冒烟来源{suffix}"
    base = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

    with Session(pg_engine) as db:
        places = [
            Place(
                name=f"PG 冒烟{index}",
                district=district,
                culture_tags=tags,
                lat=Decimal("28.100000"),
                lon=Decimal("112.900000"),
                status=PlaceStatus.VERIFIED,
                created_at=base + timedelta(minutes=index),
            )
            for index, tags in enumerate([[tag], [tag, "历史"], ["历史"]])
        ]
        source = PlaceSource(title=source_title, url="https://example.com/smoke", publisher="冒烟")
        db.add_all(places)
        db.add(source)
        db.flush()
        db.add(
            PlaceFact(
                place_id=places[0].id,
                claim="冒烟说法：只为验通读接口",
                confidence=FactConfidence.VERIFIED,
                source_id=source.id,
            )
        )
        db.commit()
        place_ids = [place.id for place in places]

    try:
        listed = pg_client.get("/api/v1/places", params={"district": district})
        assert listed.status_code == 200, listed.text
        assert [item["name"] for item in listed.json()["items"]] == [
            "PG 冒烟2",
            "PG 冒烟1",
            "PG 冒烟0",
        ]

        tagged = pg_client.get("/api/v1/places", params={"district": district, "tag": tag})
        assert sorted(item["name"] for item in tagged.json()["items"]) == ["PG 冒烟0", "PG 冒烟1"]

        # jsonb 的 `@>` 是数组元素包含，不是字符串子串：截掉一个字不该命中
        partial = pg_client.get("/api/v1/places", params={"district": district, "tag": tag[:-1]})
        assert partial.json()["items"] == []

        detail = pg_client.get(f"/api/v1/places/{place_ids[0]}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["facts"][0]["sources"][0]["title"] == source_title

        first_page = pg_client.get("/api/v1/places", params={"district": district, "limit": 2})
        assert len(first_page.json()["items"]) == 2
        cursor = first_page.json()["next_cursor"]
        assert cursor

        second_page = pg_client.get(
            "/api/v1/places", params={"district": district, "limit": 2, "cursor": cursor}
        )
        assert [item["name"] for item in second_page.json()["items"]] == ["PG 冒烟0"]
        assert second_page.json()["next_cursor"] is None
    finally:
        with Session(pg_engine) as db:
            db.execute(delete(Place).where(Place.district == district))
            db.execute(delete(PlaceSource).where(PlaceSource.title == source_title))
            db.commit()
