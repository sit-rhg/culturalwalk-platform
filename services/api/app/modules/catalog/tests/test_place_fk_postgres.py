"""CA-01 补测：外键的 ON DELETE 行为必须在真 Postgres 上验。

为什么单独一个文件：
- `conftest.db_session` 跑在 SQLite 上，而 SQLite 默认 `PRAGMA foreign_keys=OFF`：
  删父行时子行的外键**原样留着**（指向一条已经不存在的行），`ON DELETE SET NULL` / `CASCADE`
  一次都不会触发。这种地方写“行为断言”，要么只能退化成看元数据（假绿），
  要么因为“外键没被清空”这种跟约束无关的原因变红——两种都说明不了约束到底对不对。
  实测证据：同一场景在 SQLite 上跑完，source_id 仍是那个已删除来源的 UUID。
- 所以这几条行为只能拿真方言验。没有 Postgres 的机器上自动 skip；
  CI 的 backend job 带 PG service，会自动跑起来。

这个文件还钉住了一条**约束之间的配合**（CA-01 复查时才发现）：
`source_id ON DELETE SET NULL` 和 `CHECK (NOT (verified AND source_id IS NULL))` 会互相打架——
来源被删时若说法是 verified，置空会撞 CHECK。所以删除来源的语义是 fail-closed：
要么先把说法降级，要么整条删除被数据库拒绝。删来源的接口（CA-03）必须按这个来写。

注意：这里**故意走 `DELETE` 语句而不是 ORM 的 `session.delete()`**——
ORM 删父对象时会自己把子对象的 FK 置空，那样测到的是 ORM 的行为，不是数据库约束的行为。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, func, inspect, select, text, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import Base
from app.modules.catalog.models import FactConfidence, Place, PlaceFact, PlaceSource

CATALOG_TABLES = (Place.__table__, PlaceSource.__table__, PlaceFact.__table__)


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

    # 库已经迁移过就直接用；没迁移过（比如 CI 里 pytest 先于 alembic 跑）就临时建表，
    # 用完删掉，免得挡了后面的 `alembic upgrade head`。
    created_here = not inspect(engine).has_table("places")
    if created_here:
        Base.metadata.create_all(engine, tables=list(CATALOG_TABLES))
    try:
        yield engine
    finally:
        if created_here:
            Base.metadata.drop_all(engine, tables=list(CATALOG_TABLES))
        engine.dispose()


def _seed(
    session: Session, confidence: FactConfidence = FactConfidence.VERIFIED
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """造一个点位 + 一条来源 + 一条挂在该来源上的说法；返回 (place_id, source_id, fact_id)。"""
    place = Place(
        id=uuid.uuid4(),
        name="岳麓书院",
        lat=Decimal("28.186000"),
        lon=Decimal("112.952000"),
        district="岳麓区",
        culture_tags=["历史", "建筑"],
    )
    source = PlaceSource(id=uuid.uuid4(), title="《岳麓书院志》", publisher="湖南人民出版社")
    session.add_all([place, source])
    session.flush()

    fact = PlaceFact(
        id=uuid.uuid4(),
        place_id=place.id,
        claim="岳麓书院始建于北宋开宝九年（976 年）",
        confidence=confidence,
        source_id=source.id,
    )
    session.add(fact)
    session.flush()
    return place.id, source.id, fact.id


def test_deleting_source_nulls_a_likely_fact(pg_engine: Engine) -> None:
    """没把握的说法：来源没了就把出处置空，说法留下，前端按「待核实」显示。"""
    with Session(pg_engine) as session:
        _, source_id, fact_id = _seed(session, FactConfidence.LIKELY)

        session.execute(delete(PlaceSource).where(PlaceSource.id == source_id))
        session.flush()
        session.expire_all()

        reloaded = session.get(PlaceFact, fact_id)
        assert reloaded is not None, "来源被删，说法不该跟着消失"
        assert reloaded.source_id is None, "外键必须是 ON DELETE SET NULL，不是 CASCADE"
        session.rollback()


def test_verified_fact_makes_source_deletion_fail(pg_engine: Engine) -> None:
    """已核实的说法不许变成「无出处的已核实」：直接删来源必须被数据库拒绝。

    这不是 bug，是 fail-closed。删来源的接口得先把这些说法降级，或者返回 409。
    """
    with Session(pg_engine) as session:
        _, source_id, _ = _seed(session, FactConfidence.VERIFIED)

        with pytest.raises(sa_exc.IntegrityError) as excinfo:
            session.execute(delete(PlaceSource).where(PlaceSource.id == source_id))
            session.flush()

        assert "ck_place_facts_verified_needs_source" in str(excinfo.value)
        session.rollback()


def test_downgrade_facts_then_delete_source_succeeds(pg_engine: Engine) -> None:
    """正规流程：先把引用它的说法降级为 unverified，再删来源。"""
    with Session(pg_engine) as session:
        _, source_id, fact_id = _seed(session, FactConfidence.VERIFIED)

        session.execute(
            update(PlaceFact)
            .where(PlaceFact.source_id == source_id)
            .values(confidence=FactConfidence.UNVERIFIED)
        )
        session.execute(delete(PlaceSource).where(PlaceSource.id == source_id))
        session.flush()
        session.expire_all()

        reloaded = session.get(PlaceFact, fact_id)
        assert reloaded is not None
        assert reloaded.source_id is None
        assert reloaded.confidence is FactConfidence.UNVERIFIED
        session.rollback()


def test_deleting_place_cascades_its_facts(pg_engine: Engine) -> None:
    """点位删了，挂在上面的说法就是孤儿，必须一起走。"""
    with Session(pg_engine) as session:
        place_id, _, _ = _seed(session)

        session.execute(delete(Place).where(Place.id == place_id))
        session.flush()

        left = session.scalar(
            select(func.count()).select_from(PlaceFact).where(PlaceFact.place_id == place_id)
        )
        assert left == 0, "外键必须是 ON DELETE CASCADE"
        session.rollback()
