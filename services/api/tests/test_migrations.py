"""D10：迁移必须真的能跑，而不是只在测试里 `create_all` 一遍。

为什么单开一个文件：其余 DB 测试都用 `Base.metadata.create_all` 建表，
模型的 Column 定义和迁移脚本里的 DDL 是两套东西——迁移写错了（漏列、
漏索引、顺序反了）测试照样全绿，只有部署到真库才会炸。这里对着一个空库
跑真实的 `alembic upgrade head`，再用 `compare_metadata` 把迁移产物和模型
逐表比对，任何漂移都会在这里现形。

不覆盖 PG 专属语法（JSONB / GIN / 部分索引）——那些由 CI 的 Postgres 步骤跑；
这里只要保证「在 SQLite 上从零建到 head」是可行的，因为本机没有 PG。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core import config as app_config
from app.core.db import Base
from app.modules.planner import models as planner_models  # noqa: F401  注册表定义

API_DIR = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = API_DIR / "migrations"

# 这些表是本文件关心的「planner 及被它直接引用」的子集：
# 全库比对会把其它模块还没写迁移的表也算进来，那些不归本测试管。
_PLANNER_TABLES = {
    "itineraries",
    "itinerary_days",
    "itinerary_items",
    "plan_runs",
    "agent_steps",
    "plan_jobs",
}


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture()
def upgraded_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """在临时文件库上跑一次 `alembic upgrade head`，返回连上去的 engine。"""
    url = f"sqlite:///{(tmp_path / 'migrations.db').as_posix()}"
    # env.py 里 `config.set_main_option(..., settings.database_url)` 会覆盖 ini，
    # 所以必须改 settings 本身，只设环境变量在这儿不生效。
    monkeypatch.setattr(app_config.settings, "database_url", url)

    command.upgrade(_config(url), "head")

    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_head_creates_planner_tables(upgraded_engine: Engine) -> None:
    tables = set(inspect(upgraded_engine).get_table_names())
    missing = _PLANNER_TABLES - tables
    assert not missing, f"迁移后缺少表：{sorted(missing)}"
    assert "alembic_version" in tables


def test_upgrade_head_reaches_script_head(upgraded_engine: Engine) -> None:
    head = ScriptDirectory.from_config(_config("sqlite://")).get_current_head()
    with upgraded_engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    assert current == head


def test_agent_step_index_comes_from_migration(upgraded_engine: Engine) -> None:
    """0017 建的唯一索引必须在真库里存在（只写模型不写迁移时这里会红）。"""
    indexes = inspect(upgraded_engine).get_indexes("agent_steps")
    unique = {index["name"] for index in indexes if index.get("unique")}
    assert "uq_agent_steps_run_step" in unique
    target = next(index for index in indexes if index["name"] == "uq_agent_steps_run_step")
    assert target["column_names"] == ["run_id", "step_index"]


def test_migration_matches_planner_models(upgraded_engine: Engine) -> None:
    """迁移产物与模型定义比对：漏列、类型不符、默认值漂移都会报出来。"""
    with upgraded_engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        diff = compare_metadata(context, Base.metadata)
    # compare_metadata 会把整库都算进来；这里只保留本文件负责的表，
    # 其它模块未写迁移的表不归本测试管。
    relevant = [item for item in diff if _touches_planner(item)]
    assert relevant == [], f"迁移与模型不一致：{relevant}"


def _touches_planner(item: tuple[object, ...]) -> bool:
    kind = item[0]
    if kind in {"add_table", "remove_table"}:
        table = item[1]
        return getattr(table, "name", None) in _PLANNER_TABLES
    # add_column / modify_* / add_index ... 的第 3 个元素都是表名
    if len(item) > 2 and isinstance(item[2], str):
        return item[2] in _PLANNER_TABLES
    return False


def test_seeded_plan_job_survives_roundtrip(upgraded_engine: Engine) -> None:
    """迁移出来的表真的能写读一条数据（列可空性、默认值、时区都对得上）。"""
    from app.modules.planner.models import JobState, PlanJob

    with Session(upgraded_engine) as session:
        job = PlanJob(
            run_id=uuid.uuid4(),
            itinerary_id=uuid.uuid4(),
            state=JobState.PENDING,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        assert job.state is JobState.PENDING
        assert job.attempts == 0
        assert job.created_at is not None
