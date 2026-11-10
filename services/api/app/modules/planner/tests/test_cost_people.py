"""费用口径回归：详情与汇总都必须按全队人数计价（D1）。

预算闸门本来就按全队总额判断，展示口径如果还是人均，会出现「详情写 100 元、
系统却认定已花 300 元」的矛盾；这两个用例把两边的乘数钉在同一个口径上。
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.modules.planner.models import Itinerary, ItineraryDay, ItineraryItem, PlanRun, PlanState
from app.modules.planner.service import PlannerService


def _ready_itinerary(db_session: Session, *, people: int) -> Itinerary:
    """造一张 READY 的行程：一天、一条已知门票价 100 元的点位、一次成功的生成。"""
    itinerary = Itinerary(
        user_id=uuid.uuid4(),
        city="长沙",
        days=1,
        people=people,
        budget_total=1_000_000,
        state=PlanState.READY,
    )
    db_session.add(itinerary)
    day = ItineraryDay(itinerary=itinerary, day_index=1, summary="第 1 天")
    db_session.add(day)
    db_session.add(
        ItineraryItem(
            day=day,
            order=1,
            stay_minutes=60,
            price_min=10_000,
            price_max=10_000,
            confidence="verified",
            source_ids=[],
        )
    )
    db_session.add(PlanRun(itinerary=itinerary, state=PlanState.READY, input_hash="0" * 64))
    db_session.commit()
    return itinerary


def test_cost_summary_multiplies_by_people(db_session: Session) -> None:
    """2 个人、一项 100 元门票：汇总应是 200 元，不是人均 100 元。"""
    itinerary = _ready_itinerary(db_session, people=2)

    total = PlannerService(db_session).cost_summary(itinerary)

    assert (total.min_total, total.max_total) == (20_000, 20_000)


def test_plan_detail_cost_multiplies_by_people(db_session: Session) -> None:
    """3 个人时详情里的 cost 也必须是全队总额。"""
    itinerary = _ready_itinerary(db_session, people=3)

    detail = PlannerService(db_session).detail(
        user_id=itinerary.user_id,
        plan_id=itinerary.id,
        places=lambda _place_id: None,
    )

    assert (detail.cost.min_total, detail.cost.max_total) == (30_000, 30_000)
