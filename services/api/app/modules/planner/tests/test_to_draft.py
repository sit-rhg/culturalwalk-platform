# PL-07：行程转路线草稿的测试
"""分三层钉住这张卡的验收「生成草稿且节点顺序与来源保留」：

1. 映射层（`draft.py` 的纯函数）：顺序、来源、价格、时长怎么搬——不连库；
2. service 层：权限、状态门禁、空行程，以及"port 收到的正是映射出的那份"；
3. 接口层：201 的形状、缺幂等头 422、未登录 401、别人的行程 403、没生成完 409。

第 4 组单独钉"跨模块没就绪时的行为"：本分支上 routes 还是空壳（RT-01 在 main 上），
必须报一个能看懂的 409，而不是 500 或静默建出一份空草稿。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.core.deps import create_access_token
from app.core.errors import Conflict, Forbidden, NotFound
from app.main import create_app
from app.modules.planner import service as planner_service
from app.modules.planner.draft import (
    DraftNode,
    DraftSpec,
    RoutesDraftPort,
    draft_duration_minutes,
    draft_nodes,
    draft_spec,
    draft_themes,
    draft_title,
    routes_idempotency_key,
)
from app.modules.planner.models import Itinerary, ItineraryDay, ItineraryItem, PlanState
from app.modules.planner.service import PlannerService

API = "/api/v1/plans"


# ---------------------------------------------------------------- 假 port


class FakePort:
    """把收到的草稿原样记下来，测试据此判断"搬"得对不对。"""

    def __init__(self) -> None:
        self.created: list[tuple[uuid.UUID, DraftSpec, str]] = []
        self.nodes_written: list[tuple[uuid.UUID, Sequence[DraftNode]]] = []
        self.draft_id = uuid.uuid4()

    def create(self, *, owner_id: uuid.UUID, spec: DraftSpec, idempotency_key: str) -> uuid.UUID:
        self.created.append((owner_id, spec, idempotency_key))
        return self.draft_id

    def replace_nodes(
        self,
        *,
        draft_id: uuid.UUID,
        owner_id: uuid.UUID,
        nodes: Sequence[DraftNode],
        idempotency_key: str,
    ) -> None:
        self.nodes_written.append((draft_id, tuple(nodes)))


# ---------------------------------------------------------------- 落库夹具


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        # 内存库每个连接一个独立库，StaticPool 让它们共用，测试建的行程才在
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def _plan(
    db: Session,
    *,
    user_id: uuid.UUID | None = None,
    state: PlanState = PlanState.READY,
    days: int = 2,
) -> Itinerary:
    itinerary = Itinerary(
        user_id=user_id or uuid.uuid4(),
        city="长沙",
        days=days,
        people=2,
        budget_total=150000,
        preferences={"themes": ["老街", " 老街", "夜宵", ""]},
        state=state,
    )
    db.add(itinerary)
    db.flush()
    return itinerary


def _item(
    db: Session,
    day: ItineraryDay,
    *,
    order: int,
    place_id: uuid.UUID | None,
    stay: int = 60,
    price: tuple[int | None, int | None] = (None, None),
    sources: list[str] | None = None,
    transport: str | None = "metro",
) -> ItineraryItem:
    item = ItineraryItem(
        day_id=day.id,
        order=order,
        place_id=place_id,
        stay_minutes=stay,
        transport=transport,
        price_min=price[0],
        price_max=price[1],
        confidence="unverified",
        source_ids=sources or [],
        note="待核实",
    )
    db.add(item)
    db.flush()
    return item


def _day(db: Session, itinerary: Itinerary, index: int) -> ItineraryDay:
    day = ItineraryDay(itinerary_id=itinerary.id, day_index=index, summary=f"第 {index} 天")
    db.add(day)
    db.flush()
    return day


def _filled_plan(db: Session, *, user_id: uuid.UUID | None = None) -> Itinerary:
    """两天三点位：第 2 天故意先写 order=2 再写 order=1，用来验证按 order 排。"""
    itinerary = _plan(db, user_id=user_id)
    first, second = _day(db, itinerary, 1), _day(db, itinerary, 2)
    _item(db, first, order=1, place_id=uuid.uuid4(), stay=120, price=(6000, 6000))
    late = _item(
        db,
        second,
        order=2,
        place_id=uuid.uuid4(),
        stay=45,
        sources=["fact-1", "fact-2"],
        transport="walk",
    )
    early = _item(db, second, order=1, place_id=uuid.uuid4(), stay=90)
    # 没有点位的"自由活动"不搬进草稿
    _item(db, second, order=3, place_id=None, stay=30)
    assert late.order == 2 and early.order == 1
    return itinerary


# ---------------------------------------------------------------- 映射层


def test_title_uses_city_and_days(db: Session) -> None:
    assert draft_title(_plan(db, days=3)) == "长沙 3 天文化路线"


def test_title_is_cut_to_contract_length(db: Session) -> None:
    itinerary = _plan(db)
    itinerary.city = "长" * 80
    assert len(draft_title(itinerary)) == 60


def test_title_falls_back_when_city_missing(db: Session) -> None:
    itinerary = _plan(db)
    itinerary.city = "   "
    assert draft_title(itinerary) == "未命名城市 2 天文化路线"


def test_themes_drop_blanks_and_duplicates(db: Session) -> None:
    assert draft_themes(_plan(db)) == ("老街", "夜宵")


def test_nodes_keep_day_order_then_item_order(db: Session) -> None:
    itinerary = _filled_plan(db)
    nodes = draft_nodes(PlannerService(db).repo.days_for_itinerary(itinerary.id))

    assert len(nodes) == 3, "没有点位的项不搬"
    assert [node.stay_minutes for node in nodes] == [120, 90, 45]


def test_nodes_keep_sources_and_prices(db: Session) -> None:
    itinerary = _filled_plan(db)
    nodes = draft_nodes(PlannerService(db).repo.days_for_itinerary(itinerary.id))

    assert nodes[0].price_min == 6000 and nodes[0].price_max == 6000
    assert nodes[1].price_min is None, "没价格的项保持空，不写成 0"
    assert nodes[2].source_ids == ("fact-1", "fact-2")
    assert nodes[2].transport == "walk"


def test_nodes_clamp_stay_and_drop_unknown_transport(db: Session) -> None:
    itinerary = _plan(db)
    day = _day(db, itinerary, 1)
    _item(db, day, order=1, place_id=uuid.uuid4(), stay=1, transport="rocket")
    _item(db, day, order=2, place_id=uuid.uuid4(), stay=9999)

    nodes = draft_nodes([day])

    assert nodes[0].stay_minutes == 10, "低于下限夹到 10 分钟"
    assert nodes[0].transport is None, "契约外的交通方式不往外传"
    assert nodes[1].stay_minutes == 480


def test_duration_counts_stay_plus_legs() -> None:
    nodes = (
        DraftNode(place_id=uuid.uuid4(), stay_minutes=60),
        DraftNode(place_id=uuid.uuid4(), stay_minutes=30),
    )
    assert draft_duration_minutes(nodes) == 60 + 30 + 15


def test_duration_is_zero_without_nodes() -> None:
    assert draft_duration_minutes(()) == 0


def test_summary_counts_pending_prices(db: Session) -> None:
    itinerary = _filled_plan(db)
    spec = draft_spec(itinerary, PlannerService(db).repo.days_for_itinerary(itinerary.id))

    assert "长沙 2 天" in spec.summary
    assert "3 个点位" in spec.summary
    assert "2 个点位价格待核实" in spec.summary, "只有一项有价格，另两项要计数"
    assert len(spec.summary) <= 300


def test_idempotency_key_is_derived_from_plan() -> None:
    plan_id = uuid.uuid4()
    assert routes_idempotency_key(plan_id) == routes_idempotency_key(plan_id)
    assert routes_idempotency_key(plan_id).endswith(str(plan_id))
    assert len(routes_idempotency_key(plan_id)) <= 64


# ---------------------------------------------------------------- service 层


def test_to_draft_creates_draft_with_ordered_nodes(db: Session) -> None:
    itinerary = _filled_plan(db)
    port = FakePort()

    draft_id = PlannerService(db).to_draft(
        user_id=itinerary.user_id, plan_id=itinerary.id, idempotency_key="k" * 8, port=port
    )

    assert draft_id == port.draft_id
    owner_id, spec, key = port.created[0]
    assert owner_id == itinerary.user_id
    assert key == routes_idempotency_key(itinerary.id)
    assert [node.stay_minutes for node in spec.nodes] == [120, 90, 45], "顺序必须原样"
    assert spec.nodes[2].source_ids == ("fact-1", "fact-2")

    written_id, written_nodes = port.nodes_written[0]
    assert written_id == draft_id
    assert len(written_nodes) == 3


def test_to_draft_rejects_unknown_plan(db: Session) -> None:
    with pytest.raises(NotFound):
        PlannerService(db).to_draft(
            user_id=uuid.uuid4(), plan_id=uuid.uuid4(), idempotency_key="k" * 8, port=FakePort()
        )


def test_to_draft_rejects_other_owner(db: Session) -> None:
    itinerary = _filled_plan(db)
    with pytest.raises(Forbidden):
        PlannerService(db).to_draft(
            user_id=uuid.uuid4(),
            plan_id=itinerary.id,
            idempotency_key="k" * 8,
            port=FakePort(),
        )


@pytest.mark.parametrize("state", [PlanState.DRAFT, PlanState.GENERATING, PlanState.FAILED])
def test_to_draft_requires_ready_state(db: Session, state: PlanState) -> None:
    itinerary = _plan(db, state=state)
    with pytest.raises(Conflict) as excinfo:
        PlannerService(db).to_draft(
            user_id=itinerary.user_id,
            plan_id=itinerary.id,
            idempotency_key="k" * 8,
            port=FakePort(),
        )
    assert excinfo.value.code == "PLAN_NOT_READY"


def test_to_draft_rejects_plan_without_places(db: Session) -> None:
    itinerary = _plan(db)
    day = _day(db, itinerary, 1)
    _item(db, day, order=1, place_id=None)

    with pytest.raises(Conflict) as excinfo:
        PlannerService(db).to_draft(
            user_id=itinerary.user_id,
            plan_id=itinerary.id,
            idempotency_key="k" * 8,
            port=FakePort(),
        )
    assert excinfo.value.code == "PLAN_EMPTY"


# ---------------------------------------------------------------- 接口层


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


def _headers(user_id: uuid.UUID, *, key: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": (
            f"Bearer {create_access_token(user_id=str(user_id), username='t', role='user')}"
        )
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def test_api_creates_draft(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = session_factory()
    itinerary = _filled_plan(session)
    session.commit()
    session.close()

    port = FakePort()
    monkeypatch.setattr(planner_service, "routes_draft_port", lambda db: port)

    with _client(session_factory) as client:
        response = client.post(
            f"{API}/{itinerary.id}/to-draft",
            headers=_headers(itinerary.user_id, key="idem-1234"),
        )

    assert response.status_code == 201, response.text
    assert response.json() == {"draft_id": str(port.draft_id)}
    assert port.created and len(port.created[0][1].nodes) == 3


def test_api_requires_idempotency_key(session_factory: sessionmaker[Session]) -> None:
    session = session_factory()
    itinerary = _filled_plan(session)
    session.commit()
    session.close()

    with _client(session_factory) as client:
        response = client.post(
            f"{API}/{itinerary.id}/to-draft", headers=_headers(itinerary.user_id)
        )

    assert response.status_code == 422, "POST 必须带 Idempotency-Key（契约硬规则）"


def test_api_requires_login(session_factory: sessionmaker[Session]) -> None:
    with _client(session_factory) as client:
        response = client.post(
            f"{API}/{uuid.uuid4()}/to-draft", headers={"Idempotency-Key": "idem-1234"}
        )

    assert response.status_code == 401


def test_api_hides_missing_plan(session_factory: sessionmaker[Session]) -> None:
    with _client(session_factory) as client:
        response = client.post(
            f"{API}/{uuid.uuid4()}/to-draft",
            headers=_headers(uuid.uuid4(), key="idem-1234"),
        )

    assert response.status_code == 404


def test_api_forbids_other_users_plan(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = session_factory()
    itinerary = _filled_plan(session)
    session.commit()
    session.close()

    monkeypatch.setattr(planner_service, "routes_draft_port", lambda db: FakePort())

    with _client(session_factory) as client:
        response = client.post(
            f"{API}/{itinerary.id}/to-draft",
            headers=_headers(uuid.uuid4(), key="idem-1234"),
        )

    assert response.status_code == 403


def test_api_conflicts_when_still_generating(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = session_factory()
    itinerary = _plan(session, state=PlanState.GENERATING)
    session.commit()
    session.close()

    monkeypatch.setattr(planner_service, "routes_draft_port", lambda db: FakePort())

    with _client(session_factory) as client:
        response = client.post(
            f"{API}/{itinerary.id}/to-draft",
            headers=_headers(itinerary.user_id, key="idem-1234"),
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PLAN_NOT_READY"


# ---------------------------------------------------------------- 跨模块没就绪


class _StubRoutesService:
    """模拟"routes 还没提供该能力"的 service：只有 db，没有 create / replace_nodes。"""

    def __init__(self, db: object) -> None:
        self.db = db


def test_port_reports_conflict_when_routes_service_has_no_create(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """routes 侧没提供 create 时，必须报 409 ROUTES_UNAVAILABLE，而不是 500。

    用桩替换 service 来复现"能力缺失"，不依赖当前分支上 routes 实现了多少。
    """
    monkeypatch.setattr(RoutesDraftPort, "_service", lambda self: _StubRoutesService(self.db))

    with pytest.raises(Conflict) as excinfo:
        RoutesDraftPort(db).create(
            owner_id=uuid.uuid4(),
            spec=DraftSpec(
                title="长沙 1 天文化路线",
                summary="测试",
                theme_tags=(),
                duration_minutes=0,
                nodes=(),
            ),
            idempotency_key="plan:x",
        )
    assert excinfo.value.code == "ROUTES_UNAVAILABLE"


def test_port_silently_skips_nodes_when_routes_cannot_write_them(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """routes 还没提供 replace_nodes 时静默跳过，不能让整次转草稿失败。"""
    monkeypatch.setattr(RoutesDraftPort, "_service", lambda self: _StubRoutesService(self.db))

    RoutesDraftPort(db).replace_nodes(
        draft_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        nodes=(DraftNode(place_id=uuid.uuid4(), stay_minutes=30),),
        idempotency_key="plan:x:nodes",
    )


def test_real_port_creates_a_real_draft(db: Session) -> None:
    """routes 就绪后，真实 port 必须真的建出一份草稿（回归：RT-01 合入后链路仍然是通的）。"""
    from app.modules.identity.models import ROLE_USER, STATUS_ACTIVE, User
    from app.modules.identity.service import hash_password

    user = User(
        id=uuid.uuid4(),
        username="port-user",
        password_hash=hash_password("password123"),
        role=ROLE_USER,
        status=STATUS_ACTIVE,
    )
    db.add(user)
    db.commit()

    draft_id = RoutesDraftPort(db).create(
        owner_id=user.id,
        spec=DraftSpec(
            title="长沙 1 天文化路线",
            summary="测试",
            theme_tags=(),
            duration_minutes=0,
            nodes=(),
        ),
        idempotency_key="plan:x:real",
    )
    assert isinstance(draft_id, uuid.UUID)
