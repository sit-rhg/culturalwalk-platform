# PL-06：费用估算与「待核实」标注的测试
"""两层各钉一遍：

1. `pricing.py` 是纯函数，用字符串喂进去就能判断规则对不对（不连库）；
2. `persist_plan` / `detail` 用 SQLite 内存库跑，确认落下去的价格、可信度、
   来源和展示文案确实是算出来的那份，而不是恰好看起来对。

验收口径（docs/05-演示脚本与验收.md:32）：**所有 confidence != verified 的
条目都必须出现「待核实」**——所以每个"没有可信来源"的用例都要断言这一条。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.core.deps import create_access_token
from app.main import create_app
from app.modules.planner.agents.base import Candidate, PlaceSource, PlanContext
from app.modules.planner.models import Itinerary, PlanState
from app.modules.planner.place_facts import ClaimView, PlaceFactsSource, PlaceView
from app.modules.planner.pricing import (
    REASON_LIKELY,
    REASON_PLACE_MISSING,
    REASON_UNKNOWN,
    REASON_UNTRUSTED,
    REASON_VERIFIED,
    CostTotal,
    PriceQuote,
    parse_price_cents,
    quote_from_claims,
    summarize,
)
from app.modules.planner.service import PlannerService

API = "/api/v1/plans"
DAY = 24 * 60 * 60


# ---------------------------------------------------------------- 纯函数部分


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("免费开放", (0, 0)),
        ("门票 60 元", (6000, 6000)),
        ("成人票 30–60 元", (3000, 6000)),
        ("票价 60 到 30 元", (3000, 6000)),
        ("人均 8.5 元", (850, 850)),
    ],
)
def test_parse_price_cents_recognises_common_wording(text: str, expected: tuple[int, int]) -> None:
    assert parse_price_cents(text) == expected


def test_parse_price_cents_returns_none_without_number() -> None:
    assert parse_price_cents("建议傍晚去，人少") is None


def test_parse_price_cents_reads_free_before_numbers() -> None:
    """「免费开放（原价 60 元）」不能被认成 60 元——那会把免费景点写成收费。"""
    assert parse_price_cents("2019 年起免费开放（原价 60 元）") == (0, 0)


def test_quote_uses_verified_claim_first() -> None:
    quote = quote_from_claims(
        [
            ClaimView(claim="门票 20 元", confidence="unverified"),
            ClaimView(
                claim="门票 60 元", confidence="verified", source_urls=("https://a.example",)
            ),
        ]
    )
    assert quote == PriceQuote(
        min_cents=6000,
        max_cents=6000,
        confidence="verified",
        source_urls=("https://a.example",),
        reason=REASON_VERIFIED,
    )
    assert quote.pending is False


def test_quote_prefers_likely_over_unverified() -> None:
    quote = quote_from_claims(
        [
            ClaimView(claim="门票 30 元", confidence="unverified"),
            ClaimView(claim="门票 60 元", confidence="likely"),
        ]
    )
    assert (quote.min_cents, quote.confidence, quote.reason) == (6000, "likely", REASON_LIKELY)


def test_quote_keeps_number_but_marks_untrusted() -> None:
    """有数字但来源不可信：数字照给、可信度降级、文案说清楚——藏起来反而更难判断量级。"""
    quote = quote_from_claims([ClaimView(claim="门票 40 元", confidence="unverified")])
    assert (quote.min_cents, quote.confidence, quote.reason) == (
        4000,
        "unverified",
        REASON_UNTRUSTED,
    )
    assert quote.pending is True


def test_quote_without_any_claim_is_pending() -> None:
    quote = quote_from_claims([])
    assert quote == PriceQuote(
        min_cents=None,
        max_cents=None,
        confidence="unverified",
        reason=REASON_UNKNOWN,
    )
    assert quote.known is False and quote.pending is True


class _Item:
    def __init__(self, price_min: int | None, price_max: int | None) -> None:
        self.price_min = price_min
        self.price_max = price_max


def test_summarize_counts_unknown_without_pretending_zero() -> None:
    """缺失项单独计数：把缺价当 0 会让用户以为这一天不花钱。"""
    total = summarize([_Item(1000, 2000), _Item(500, 500), _Item(None, None)])
    assert total == CostTotal(min_total=1500, max_total=2500, currency="CNY", unknown_count=1)


def test_summarize_of_empty_plan() -> None:
    assert summarize([]) == CostTotal(min_total=0, max_total=0, unknown_count=0)


# ---------------------------------------------------------------- 落库与详情


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        # 内存库默认每个连接一个独立的库，StaticPool 让它们共用一个，建的表才在
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


def _make_itinerary(db: Session) -> Itinerary:
    itinerary = Itinerary(
        user_id=uuid.uuid4(),
        city="长沙",
        days=2,
        people=2,
        budget_total=100000,
        preferences={},
        state=PlanState.GENERATING,
    )
    db.add(itinerary)
    db.flush()
    return itinerary


def _schedule(day_index: int, place_id: str) -> list[dict[str, object]]:
    return [
        {
            "day_index": day_index,
            "order": 1,
            "part": "morning",
            "start_time": "09:00",
            "place_id": place_id,
            "name": "岳麓山",
            "stay_minutes": 120,
            "transport": "metro",
        }
    ]


def _context(schedule: list[dict[str, object]]) -> PlanContext:
    return PlanContext(
        city="长沙",
        days=2,
        people=2,
        budget_total=100000,
        schedule=list(schedule),
    )


def test_persist_plan_writes_days_items_and_prices(db: Session) -> None:
    itinerary = _make_itinerary(db)
    place_id = uuid.uuid4()
    places = {
        place_id: PlaceView(
            id=place_id,
            name="岳麓山",
            district="岳麓区",
            claims=(
                ClaimView(
                    claim="门票 60 元", confidence="verified", source_urls=("https://a.example",)
                ),
            ),
        )
    }

    PlannerService(db).persist_plan(
        itinerary,
        _context(_schedule(1, str(place_id)) + _schedule(2, str(place_id))),
        places=places.get,
    )
    db.flush()

    days = PlannerService(db).repo.days_for_itinerary(itinerary.id)
    assert [day.day_index for day in days] == [1, 2]
    item = days[0].items[0]
    assert (item.order, item.stay_minutes, item.transport) == (1, 120, "metro")
    assert (item.price_min, item.price_max) == (6000, 6000)
    assert (item.confidence, item.note) == ("verified", REASON_VERIFIED)
    assert item.source_ids == ["https://a.example"]
    assert item.start_time is not None and item.start_time.hour == 9


def test_persist_plan_marks_missing_place_as_pending(db: Session) -> None:
    """点位查不到（资料缺失 / 还没发布）时必须标待核实，不能让这一项"看起来免费"。"""
    itinerary = _make_itinerary(db)
    PlannerService(db).persist_plan(
        itinerary,
        _context(_schedule(1, str(uuid.uuid4()))),
        places=lambda _place_id: None,
    )
    db.flush()

    item = PlannerService(db).repo.days_for_itinerary(itinerary.id)[0].items[0]
    assert (item.price_min, item.price_max, item.confidence) == (None, None, "unverified")
    assert item.note == REASON_PLACE_MISSING


def test_persist_plan_replaces_previous_schedule(db: Session) -> None:
    """重新生成时先清后写：旧的第 3 天不能留在新生成的两天行程里。"""
    itinerary = _make_itinerary(db)
    service = PlannerService(db)
    places = lambda _place_id: None  # noqa: E731 - 单一用途的桩，不值得起名字

    first = _schedule(1, str(uuid.uuid4())) + _schedule(2, str(uuid.uuid4())) + _schedule(3, "")
    service.persist_plan(itinerary, _context(first), places=places)
    db.flush()
    assert len(service.repo.days_for_itinerary(itinerary.id)) == 3

    service.persist_plan(itinerary, _context(_schedule(1, str(uuid.uuid4()))), places=places)
    db.flush()
    days = service.repo.days_for_itinerary(itinerary.id)
    assert [day.day_index for day in days] == [1]
    assert len(days[0].items) == 1


def _run_single_plan(
    session_factory: sessionmaker[Session],
    *,
    place_source: PlaceSource | None,
    places: PlaceFactsSource | None,
) -> dict[str, object]:
    """走一遍真实链路：POST 建行程 → 代跑一次 worker → GET 详情。"""

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
    headers = {
        "Authorization": (
            f"Bearer {create_access_token(user_id=str(uuid.uuid4()), username='t', role='user')}"
        ),
        "Idempotency-Key": str(uuid.uuid4()),
    }

    with TestClient(app) as client:
        created = client.post(
            API,
            json={"city": "长沙", "days": 1, "people": 1, "budget_total": 10000},
            headers=headers,
        )
        assert created.status_code == 202
        plan_id = created.json()["id"]

        # 后台 worker 的活由测试代跑：领一条任务并执行（领活 + 执行两个会话，与线上一致）
        session = session_factory()
        try:
            service = PlannerService(session)
            job = service.claim_job(worker="test")
            assert job is not None
            service.execute_job(job, place_source=place_source, places=places)
            session.commit()
        finally:
            session.close()

        detail = client.get(f"{API}/{plan_id}", headers=headers)

    app.dependency_overrides.clear()
    assert detail.status_code == 200
    return detail.json()


def test_detail_reports_cost_and_pending_items(
    session_factory: sessionmaker[Session],
) -> None:
    """详情接口：有可信来源的项算进总额，没来源的项单独计数并标「待核实」。"""
    priced_id = uuid.uuid4()
    unknown_id = uuid.uuid4()

    def place_source(_ctx: PlanContext) -> list[Candidate]:
        return [
            Candidate(
                place_id=str(priced_id),
                name="岳麓山",
                lat=28.18,
                lon=112.94,
                stay_minutes=120,
            ),
            Candidate(
                place_id=str(unknown_id),
                name="橘子洲",
                lat=28.19,
                lon=112.96,
                stay_minutes=90,
            ),
        ]

    places = {
        priced_id: PlaceView(
            id=priced_id,
            name="岳麓山",
            district="岳麓区",
            lat=28.18,
            lon=112.94,
            claims=(
                ClaimView(
                    claim="门票 60 元", confidence="verified", source_urls=("https://a.example",)
                ),
            ),
        )
    }

    body = _run_single_plan(session_factory, place_source=place_source, places=places.get)

    assert body["state"] == "READY"
    assert body["steps"], "四步留痕不能丢"
    items = [item for day in body["days_plan"] for item in day["items"]]
    assert len(items) == 2

    priced = next(item for item in items if item["confidence"] == "verified")
    assert (priced["price_min"], priced["price_max"]) == (6000, 6000)
    assert priced["reason"] == REASON_VERIFIED

    unknown = next(item for item in items if item["price_min"] is None)
    assert unknown["confidence"] == "unverified"
    assert "待核实" in unknown["reason"]

    # 有价的那项进总额，没价的那项只计数——不是 0 元
    assert body["cost"]["min_total"] == 6000
    assert body["cost"]["max_total"] == 6000
    assert body["cost"]["unknown_count"] == 1
    assert body["cost"]["currency"] == "CNY"


def test_detail_marks_everything_pending_without_catalog(
    session_factory: sessionmaker[Session],
) -> None:
    """catalog 查不到时（本分支还没 CA-03），所有项都必须标待核实，接口不能 500。"""

    def place_source(_ctx: PlanContext) -> list[Candidate]:
        return [Candidate(place_id=str(uuid.uuid4()), name="太平老街", lat=28.19, lon=112.97)]

    body = _run_single_plan(session_factory, place_source=place_source, places=None)

    items = [item for day in body["days_plan"] for item in day["items"]]
    assert items
    assert all(item["confidence"] == "unverified" for item in items)
    assert all("待核实" in item["reason"] for item in items)
    assert body["cost"]["unknown_count"] == len(items)
    assert body["cost"]["min_total"] == 0 and body["cost"]["max_total"] == 0
