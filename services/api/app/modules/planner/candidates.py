"""从地点库挑候选点位（补 PL-03 留的那个空）。

PL-03 交付时，调研员的 `place_source` 默认是「恒返回空清单」的，理由写在
`agents/researcher.py` 里：当时还没有检索服务，管道先通、内容后填。
结果是 worker 跑完一条任务后日程是空的 —— 规划页显示「这次没有排出行程」。
本文件就是补那一刀：从 catalog 的**已核验**点位里挑候选。

为什么不做打分排序：热度、好评、季节性都要先有数据源和口径，属于 CA 卡的活。
这里只做三件确定的事 —— 只取 verified、按用户选的文化主题过滤、按天数和
节奏算出需要多少个点位。规则简单到可以预测，同一份数据跑两次结果一致。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.catalog.models import Place, PlaceStatus
from app.modules.planner.agents.base import Candidate, PlanContext

# 每种点位的默认停留时长（分钟）。数值取自 docs/02 §2.5 的默认建议；
# 没有分类型数据之前，用一张常量表比让模型瞎猜更可控。
STAY_MINUTES: dict[str, int] = {
    "museum": 120,
    "memorial": 90,
    "temple": 60,
    "old_street": 90,
    "park": 60,
    "food": 45,
}
DEFAULT_STAY_MINUTES = 90

# 一天最多带几个候选：比排班上限多一点，留给挑刺员淘汰。
CANDIDATES_PER_DAY = 5


def catalog_candidate_source(db: Session):
    """返回一个「吃规划上下文、给出候选清单」的函数。

    签名与 `agents.base.PlaceSource` 一致：`(PlanContext) -> list[Candidate]`，
    所以 orchestrator 里换掉它不需要动四个岗位的任何代码。
    """

    def load(ctx: PlanContext) -> list[Candidate]:
        rows = db.execute(
            select(Place).where(Place.status == PlaceStatus.VERIFIED).order_by(Place.created_at)
        ).scalars()
        themes = _themes(ctx)
        picked: list[Candidate] = []
        for place in rows:
            tags = [str(tag) for tag in (place.culture_tags or [])]
            if themes and not (set(themes) & set(tags)):
                continue
            picked.append(
                Candidate(
                    place_id=str(place.id),
                    name=place.name,
                    category=tags[0] if tags else None,
                    lat=float(place.lat) if place.lat is not None else None,
                    lon=float(place.lon) if place.lon is not None else None,
                    stay_minutes=STAY_MINUTES.get(tags[0] if tags else "", DEFAULT_STAY_MINUTES),
                )
            )
            if len(picked) >= ctx.days * CANDIDATES_PER_DAY:
                break
        return picked

    return load


def _themes(ctx: PlanContext) -> list[str]:
    """用户勾的文化主题，容忍字符串/列表两种写法（前端两种都传过）。"""
    raw = ctx.preferences.get("themes") or ctx.preferences.get("theme")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, (list, tuple, set)):
        return [str(item) for item in raw if item]
    return []
