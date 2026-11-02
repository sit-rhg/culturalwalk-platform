"""RT-03 补测：版本快照在真 Postgres 上必须是 JSONB，且版本号唯一约束生效。

为什么单独一个文件：
- 内存 SQLite 上 `snapshot` 退化成 TEXT，`->>`、JSONB 包含查询这些只有真方言能验；
- `(draft_id, version_no)` 的唯一约束在 PG 上的报错路径值得钉住（并发冻结靠它兜底）；
- 没有 Postgres 的机器自动 skip，CI 的 backend job 带 PG service 会跑起来。

用例自建带随机后缀的数据、跑完删干净，不和库里已有的种子数据互相干扰。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from typing import cast

import pytest
from sqlalchemy import Table, create_engine, delete, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import Base
from app.modules.routes.models import RouteDraft, RouteNode, RouteVersion
from app.modules.routes.service import RoutesService


def _routes_tables() -> list[Table]:
    return [
        cast(Table, RouteDraft.__table__),
        cast(Table, RouteNode.__table__),
        cast(Table, RouteVersion.__table__),
    ]


@pytest.fixture()
def pg_engine() -> Iterator[Engine]:
    """真 Postgres 引擎；连不上就 skip，不让本机没装库的人卡住。"""
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("需要 Postgres：当前 DATABASE_URL 不是 postgresql")

    engine = create_engine(settings.database_url, future=True, connect_args={"connect_timeout": 2})
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 — 连不上就是 skip，不区分具体异常
        engine.dispose()
        pytest.skip(f"连不上 Postgres：{exc}")

    created_here = not inspect(engine).has_table("route_drafts")
    if created_here:
        Base.metadata.create_all(engine, tables=_routes_tables())
    try:
        yield engine
    finally:
        if created_here:
            Base.metadata.drop_all(engine, tables=_routes_tables())
        engine.dispose()


def test_snapshot_is_jsonb_and_version_no_is_unique(pg_engine: Engine) -> None:
    owner_id = uuid.uuid4()
    draft_id: uuid.UUID
    version_id: uuid.UUID
    version_no: int

    with Session(pg_engine) as db:
        draft = RouteDraft(owner_id=owner_id, title="PG 冻结冒烟", summary="只在测试库里")
        db.add(draft)
        db.flush()
        draft_id = draft.id
        db.add(
            RouteNode(
                draft_id=draft.id,
                place_id=uuid.uuid4(),
                position=1,
                stay_minutes=45,
                transport="walk",
                note="第一站",
                price_min=1200,
                price_max=1800,
            )
        )
        db.flush()

        version = RoutesService(db).freeze_version(draft_id=draft.id, actor_id=owner_id)
        db.commit()
        version_id, version_no = version.id, version.version_no
        snapshot = dict(version.snapshot)
        stored_hash = version.content_hash

    try:
        # 1) 落的是 JSONB：`->>` 能直接取标量（TEXT 列上这个运算符不存在）。
        with Session(pg_engine) as db:
            title = db.execute(
                text("select snapshot->>'title' from route_versions where id = :id"),
                {"id": str(version_id)},
            ).scalar_one()
            assert title == "PG 冻结冒烟"

        # 2) content_hash 就是 sha256(canonical_json(snapshot))，可以离线重算。
        canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        assert stored_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        # 3) 同一草稿的版本号唯一：并发冻结撞车时必须被数据库挡住。
        with Session(pg_engine) as db:
            db.add(
                RouteVersion(
                    draft_id=draft_id,
                    version_no=version_no,
                    snapshot={"title": "重复版本号"},
                    content_hash="0" * 64,
                )
            )
            with pytest.raises(IntegrityError):
                db.flush()
            db.rollback()
    finally:
        with Session(pg_engine) as db:
            db.execute(delete(RouteVersion).where(RouteVersion.draft_id == draft_id))
            db.execute(delete(RouteNode).where(RouteNode.draft_id == draft_id))
            db.execute(delete(RouteDraft).where(RouteDraft.id == draft_id))
            db.commit()
