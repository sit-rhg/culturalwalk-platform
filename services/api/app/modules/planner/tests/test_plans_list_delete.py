# PL-08：我的行程列表与删除行程的接口测试
"""钉住两个端点的对外行为：列表只列自己的并游标翻页，删除只删自己的并留下审计。

planner 的 tests 目录没有 conftest，根 conftest 又会种 identity 的账号；这里只需要
一张行程表，所以自带一套夹具，独立的内存库更快也更隔离。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.core.deps import create_access_token
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.planner.models import Itinerary, PlanState

API = "/api/v1/plans"

# 用 naive 时间造数据：SQLite 不保留 tzinfo，统一成朴素时间，比较行为才和真实返回一致
_BASE = datetime(2026, 1, 1)


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


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def auth() -> tuple[dict[str, str], str]:
    user_id = str(uuid.uuid4())
    token = create_access_token(user_id=user_id, username="feral0702", role="user")
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())}
    return headers, user_id


def _plan(
    db: Session,
    *,
    user_id: uuid.UUID,
    state: PlanState = PlanState.READY,
    created_at: datetime,
) -> Itinerary:
    itinerary = Itinerary(
        user_id=user_id,
        city="长沙",
        days=2,
        people=2,
        budget_total=150000,
        preferences={},
        state=state,
        created_at=created_at,
    )
    db.add(itinerary)
    db.flush()
    return itinerary


def test_我的行程列表只含本人且按创建时间倒序游标翻页(
    client: TestClient, session_factory: sessionmaker[Session], auth: tuple[dict[str, str], str]
) -> None:
    headers, user_id = auth
    me = uuid.UUID(user_id)
    db = session_factory()
    mine = [
        _plan(db, user_id=me, state=state, created_at=_BASE + timedelta(minutes=index))
        for index, state in enumerate((PlanState.DRAFT, PlanState.READY, PlanState.FAILED))
    ]
    _plan(db, user_id=uuid.uuid4(), created_at=_BASE + timedelta(minutes=30))
    deleted = _plan(db, user_id=me, created_at=_BASE + timedelta(minutes=40))
    deleted.deleted_at = _BASE + timedelta(days=1)
    db.commit()
    db.close()

    first = client.get(f"{API}?limit=2", headers=headers)
    assert first.status_code == 200
    body = first.json()
    assert [item["id"] for item in body["items"]] == [str(mine[2].id), str(mine[1].id)]
    assert [item["state"] for item in body["items"]] == ["FAILED", "READY"]
    assert body["next_cursor"]

    second = client.get(f"{API}?limit=2&cursor={body['next_cursor']}", headers=headers)
    assert second.status_code == 200
    tail = second.json()
    assert [item["id"] for item in tail["items"]] == [str(mine[0].id)]
    assert tail["next_cursor"] is None


def test_未登录看列表返回401(client: TestClient) -> None:
    assert client.get(API).status_code == 401


def test_坏游标返回400且错误码为INVALID_CURSOR(
    client: TestClient, auth: tuple[dict[str, str], str]
) -> None:
    headers, _ = auth
    resp = client.get(f"{API}?cursor=not-a-cursor", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_CURSOR"


def test_删除自己的行程会软删并写一条审计(
    client: TestClient, session_factory: sessionmaker[Session], auth: tuple[dict[str, str], str]
) -> None:
    headers, user_id = auth
    db = session_factory()
    plan = _plan(db, user_id=uuid.UUID(user_id), created_at=_BASE)
    db.commit()
    plan_id = plan.id
    db.close()

    resp = client.delete(f"{API}/{plan_id}", headers=headers)
    assert resp.status_code == 204
    assert resp.content == b""

    db = session_factory()
    stored = db.get(Itinerary, plan_id)
    assert stored is not None
    assert stored.deleted_at is not None
    log = db.scalars(select(AuditLog).where(AuditLog.action == "plan.delete")).one()
    assert log.object_type == "itinerary"
    assert log.object_id == str(plan_id)
    assert log.actor_id == user_id
    assert log.actor_username == "feral0702"
    assert log.before_hash
    db.close()

    # 行程已经不可见：详情 404，重复删除也是 404
    assert client.get(f"{API}/{plan_id}", headers=headers).status_code == 404
    assert client.delete(f"{API}/{plan_id}", headers=headers).status_code == 404


def test_删除别人的行程返回404且不动数据(
    client: TestClient, session_factory: sessionmaker[Session], auth: tuple[dict[str, str], str]
) -> None:
    headers, _ = auth
    db = session_factory()
    plan = _plan(db, user_id=uuid.uuid4(), created_at=_BASE)
    db.commit()
    plan_id = plan.id
    db.close()

    assert client.delete(f"{API}/{plan_id}", headers=headers).status_code == 404

    db = session_factory()
    stored = db.get(Itinerary, plan_id)
    assert stored is not None
    assert stored.deleted_at is None
    db.close()


def test_删除不存在的行程返回404(client: TestClient, auth: tuple[dict[str, str], str]) -> None:
    headers, _ = auth
    resp = client.delete(f"{API}/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_未登录删除行程返回401(client: TestClient) -> None:
    assert client.delete(f"{API}/{uuid.uuid4()}").status_code == 401
