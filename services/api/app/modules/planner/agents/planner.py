"""排班员：把候选点位排进「第几天 / 哪个时段 / 停留多久 / 怎么去」。

确定性贪心：按候选顺序填时段，填满当天就换下一天，超出天数的多余候选直接丢掉。
不做地理聚类或时长博弈 —— 那是挑刺员反馈之后的重排，属于后续卡，骨架里先保持可预测，
否则同一份输入跑两次排出两张不同的表，测试就没法钉住行为了。

两个用户偏好在这里第一次真正生效（WP-04 D4）：
- `start_point`：只用来给第一天的前几个候选排序，从起点附近开始逛；
- `include_lodging`：晚上留给住宿，观光时段少一段，日期摘要上标「含住宿」。
以前这两个字段只写进 preferences 就没人读，用户选了等于没选。
"""

from __future__ import annotations

import math
from typing import Final

from app.modules.planner.agents.base import Candidate, PlanContext, primary_transport
from app.modules.planner.models import AgentName

# 一天能塞几个点，取决于用户选的节奏。数值来自 docs/02 §2.5 的默认建议。
PER_DAY_BY_PACE: Final[dict[str, int]] = {"relaxed": 2, "normal": 3, "packed": 4}
DEFAULT_DAY_PARTS: Final[tuple[str, ...]] = ("morning", "afternoon", "evening")
SLOT_START: Final[dict[str, str]] = {
    "morning": "09:00",
    "afternoon": "14:00",
    "evening": "19:00",
}
# 含住宿的那几天，晚上不进观光时段：19:00 之后是回酒店的时间。
LODGING_PART: Final[str] = "evening"

_EARTH_RADIUS_M: Final[float] = 6_371_000.0
# 没经纬度的候选一律排到最后：宁可不动，也不要拿 0,0 当坐标排出荒唐顺序。
_NO_COORDINATE: Final[float] = float("inf")


class PlannerAgent:
    """按时段表排出逐日的行程条目。"""

    name = AgentName.PLANNER

    def run(self, ctx: PlanContext) -> PlanContext:
        lodging = _wants_lodging(ctx.preferences)
        parts = _day_parts(ctx.preferences.get("day_parts"), lodging=lodging)
        pace_cap = PER_DAY_BY_PACE.get(str(ctx.preferences.get("pace", "normal")), 3)
        per_day = min(pace_cap, len(parts))
        # 请求里 transport 是数组；取第一个合法值落库，绝不能 str(list)。
        transport = primary_transport(ctx.preferences)
        candidates = _order_from_start(
            ctx.candidates,
            per_day=per_day,
            start=_start_point(ctx.preferences),
        )

        schedule: list[dict[str, object]] = []
        for index, candidate in enumerate(candidates):
            day_index = index // per_day + 1
            if day_index > ctx.days:
                break
            order = index % per_day + 1
            part = parts[order - 1]
            schedule.append(
                {
                    "day_index": day_index,
                    "order": order,
                    "part": part,
                    "start_time": SLOT_START[part],
                    "place_id": candidate.place_id,
                    "name": candidate.name,
                    "stay_minutes": candidate.stay_minutes,
                    "transport": transport,
                }
            )
        ctx.schedule = schedule
        return ctx


def _day_parts(raw: object, *, lodging: bool = False) -> tuple[str, ...]:
    """用户想把一天出门的时段限定到哪几段。

    传了非法值时退回默认三段而不是 422：到这里请求早就校验过了，
    再报错只会让一次生成白等半天。空列表同样按默认处理（用户没表达意愿）。
    """
    without_lodging = tuple(part for part in DEFAULT_DAY_PARTS if part != LODGING_PART)
    if isinstance(raw, list):
        wanted = [str(part) for part in raw if str(part) in SLOT_START]
        if wanted:
            parts = tuple(sorted(wanted, key=DEFAULT_DAY_PARTS.index))
            # 「晚上留给住宿」是更硬的约束：前端默认会带上全部三段，
            # 不能因为 day_parts 里出现了 evening 就把住宿时段又排满观光。
            if lodging:
                parts = tuple(part for part in parts if part != LODGING_PART)
            return parts or without_lodging
    if lodging:
        return without_lodging
    return DEFAULT_DAY_PARTS


def _wants_lodging(preferences: dict[str, object]) -> bool:
    """`include_lodging` 只认真正的 true；字符串 "false" 不该被当成想要住宿。"""
    return preferences.get("include_lodging") is True


def _start_point(preferences: dict[str, object]) -> tuple[float, float] | None:
    """读取用户给的起点；坐标缺失或不是数字时返回 None（当作没填）。"""
    raw = preferences.get("start_point")
    if not isinstance(raw, dict):
        return None
    lat, lon = raw.get("lat"), raw.get("lon")
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    return None


def _order_from_start(
    candidates: list[Candidate],
    *,
    per_day: int,
    start: tuple[float, float] | None,
) -> list[Candidate]:
    """把第一天的候选按「离起点远近」排一遍，其余候选原样保留。

    只动第一天是刻意的：用户给的是一整天行程的起点，第一天从近处开始走最合理；
    后面的天数如果也按这个点排序，会把整条路线拧成"每天回到起点再出发"，
    反而比不排更差。排序键带名称与编号兜底，保证同一份输入两次跑结果一致。
    """
    if start is None or per_day <= 0 or not candidates:
        return list(candidates)
    head = list(candidates[:per_day])
    head.sort(key=lambda item: (_distance_m(start, item), item.name, item.place_id))
    return head + list(candidates[per_day:])


def _distance_m(start: tuple[float, float], candidate: Candidate) -> float:
    """起点到候选的球面距离（米）。缺坐标的排最后，不抛异常。"""
    if candidate.lat is None or candidate.lon is None:
        return _NO_COORDINATE
    lat1, lon1 = math.radians(start[0]), math.radians(start[1])
    lat2, lon2 = math.radians(candidate.lat), math.radians(candidate.lon)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))
