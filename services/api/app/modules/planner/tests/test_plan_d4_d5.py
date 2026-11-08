# WP-04 第二批：D3/D4/D5 的回归用例
"""三条缺陷的共同点是「参数收下了，但没人用」或者「约束只写在文档里」。

- D3：查不到点位的行程项，契约与实现必须都允许 place=null；
- D4：Idempotency-Key 必须按契约是 uuid；start_point / include_lodging 要真的影响排班；
- D5：租约要续、步骤序号要唯一。

能写成纯函数断言的就不连库；需要链路的用 SQLite 内存库 + worker 代跑。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.core.deps import create_access_token
from app.main import create_app
from app.modules.planner.agents.base import Candidate, PlanContext
from app.modules.planner.agents.planner import PlannerAgent
from app.modules.planner.models import AgentStep, Itinerary, JobState, PlanRun, PlanState
from app.modules.planner.orchestrator import Orchestrator
from app.modules.planner.pricing import summarize
from app.modules.planner.service import PlannerService

API = "/api/v1/plans"


# ---------------------------------------------------------------- D4：幂等键


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _client(session_factory: sessionmaker[Session]) -> TestClient:
    def override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _auth() -> dict[str, str]:
    token = create_access_token(user_id=str(uuid.uuid4()), username="t", role="user")
    return {"Authorization": f"Bearer {token}"}


def _payload() -> dict[str, object]:
    return {"city": "长沙", "days": 2, "people": 2, "budget_total": 100000}


def test_non_uuid_idempotency_key_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    """契约写的是 format: uuid，实现只限了 8~64 字符——手搓 key 会撞车，必须 422。"""
    with _client(session_factory) as client:
        response = client.post(
            API,
            json=_payload(),
            headers={**_auth(), "Idempotency-Key": "order-1-please"},
        )
    assert response.status_code == 422, response.text
    detail = response.json()["error"]["details"][0]
    assert detail["field"] == "Idempotency-Key"
    assert detail["reason"] == "需要一个 uuid"


def test_uuid_idempotency_key_is_normalised(
    session_factory: sessionmaker[Session],
) -> None:
    """同一把 key 的大小写写法算同一把：第二次必须拿到同一张行程。"""
    key = uuid.uuid4()
    headers = _auth()  # 同一个人提交两次，才谈得上幂等
    with _client(session_factory) as client:
        first = client.post(
            API, json=_payload(), headers={**headers, "Idempotency-Key": str(key).upper()}
        )
        second = client.post(API, json=_payload(), headers={**headers, "Idempotency-Key": str(key)})
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]


# ---------------------------------------------------------------- D4：起点与住宿


def _context(**preferences: object) -> PlanContext:
    return PlanContext(
        city="长沙",
        days=1,
        people=2,
        budget_total=100000,
        preferences=dict(preferences),
        candidates=[
            Candidate(place_id=str(uuid.uuid4()), name="远山", lat=28.40, lon=113.20),
            Candidate(place_id=str(uuid.uuid4()), name="中站", lat=28.25, lon=113.00),
            Candidate(place_id=str(uuid.uuid4()), name="近巷", lat=28.181, lon=112.941),
        ],
    )


def test_start_point_orders_first_day_by_distance() -> None:
    """给了起点，第一天的前几个候选按离起点远近排——以前这个字段只存不读。"""
    ctx = _context(start_point={"lat": 28.180, "lon": 112.940, "label": "住处"})
    ctx = PlannerAgent().run(ctx)
    names = [entry["name"] for entry in ctx.schedule]
    assert names[:3] == ["近巷", "中站", "远山"]


def test_without_start_point_keeps_candidate_order() -> None:
    """没给起点时保持原顺序：确定性优先，别让"附近"变成一个随机排序。"""
    ctx = PlannerAgent().run(_context())
    assert [entry["name"] for entry in ctx.schedule] == ["远山", "中站", "近巷"]


def test_include_lodging_drops_evening_slot() -> None:
    """含住宿时晚上留给酒店：观光时段里不再出现 evening。"""
    ctx = PlannerAgent().run(_context(include_lodging=True))
    assert {entry["part"] for entry in ctx.schedule} <= {"morning", "afternoon"}
    assert len(ctx.schedule) == 2


def test_string_false_is_not_lodging() -> None:
    """`include_lodging: "false"` 不该被当成 true——前端传字符串是常见事故。"""
    ctx = PlannerAgent().run(_context(include_lodging="false"))
    assert {entry["part"] for entry in ctx.schedule} == {"morning", "afternoon", "evening"}


# ---------------------------------------------------------------- D5：租约与唯一约束


def test_orchestrator_calls_on_step_for_each_agent() -> None:
    """四个岗位每跑完一个就回调一次：worker 靠它续租，不续就会被别人重领。"""
    seen: list[str] = []
    orchestrator = Orchestrator(on_step=lambda record: seen.append(record.agent.value))
    result = orchestrator.run(_context())
    assert result.ok
    assert seen == ["researcher", "planner", "critic", "verifier"]


def test_renew_lease_extends_expiry(session_factory: sessionmaker[Session]) -> None:
    """续租把 lease_until 往后推：任务跑到一半时，别的 worker 不该把它抢走。"""
    session = session_factory()
    try:
        itinerary = Itinerary(
            user_id=uuid.uuid4(),
            city="长沙",
            days=1,
            people=1,
            budget_total=0,
            preferences={},
            state=PlanState.GENERATING,
        )
        session.add(itinerary)
        session.flush()
        run = PlanRun(itinerary_id=itinerary.id, state=PlanState.GENERATING, input_hash="h" * 64)
        session.add(run)
        session.flush()
        service = PlannerService(session)
        service.enqueue(itinerary, run)
        claimed = service.claim_job(worker="test")
        assert claimed is not None and claimed.state == JobState.RUNNING
        before = claimed.lease_until
        assert before is not None

        # 模拟"跑了两分钟"：续租之后到期时间必须比原来更晚
        later = datetime.now(UTC) + timedelta(minutes=5)
        service.renew_lease(claimed, now=later)
        assert claimed.lease_until is not None
        assert claimed.lease_until > before
        assert claimed.lease_until == later + timedelta(seconds=120)
    finally:
        session.close()


def test_agent_steps_run_step_is_unique() -> None:
    """(run_id, step_index) 唯一索引必须真的建在表上，只在注释里写不算。"""
    indexes = {index.name for index in AgentStep.__table__.indexes}
    assert "uq_agent_steps_run_step" in indexes
    unique = [
        index for index in AgentStep.__table__.indexes if index.name == "uq_agent_steps_run_step"
    ]
    assert unique and unique[0].unique


def test_unique_index_is_created_on_sqlite(session_factory: sessionmaker[Session]) -> None:
    """create_all 之后 SQLite 上也能查到这条唯一索引（迁移与模型一起钉）。"""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        indexes = {index["name"]: index for index in inspect(engine).get_indexes("agent_steps")}
    finally:
        engine.dispose()
    assert indexes["uq_agent_steps_run_step"]["unique"] == 1
    assert indexes["uq_agent_steps_run_step"]["column_names"] == ["run_id", "step_index"]


# ---------------------------------------------------------------- 汇总乘人数


def test_summarize_multiplies_headcount() -> None:
    """票价是每人一张：两个人去要乘 2，否则预算校验永远不触发（D1 的尾巴）。"""

    class _Item:
        def __init__(self, low: int, high: int) -> None:
            self.price_min = low
            self.price_max = high

    one = summarize([_Item(3000, 5000)])
    two = summarize([_Item(3000, 5000)], people=2)
    assert (one.min_total, one.max_total) == (3000, 5000)
    assert (two.min_total, two.max_total) == (6000, 10000)
