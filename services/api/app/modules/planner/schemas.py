# 行程规划（多 Agent） · DTO
"""请求/响应模型。字段与 docs/api/openapi.yaml 保持一致，改 yaml 再改这里。"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.modules.planner.models import Itinerary, PlanState
from app.modules.planner.pricing import REASON_UNKNOWN, UNVERIFIED


class DayPart(StrEnum):
    """一天里想出门的时段，取值与契约 day_parts 的 enum 一致。"""

    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


class TransportMode(StrEnum):
    """交通方式。只收契约里列的五种，前端多传一个词就该被挡回去。"""

    WALK = "walk"
    METRO = "metro"
    BUS = "bus"
    TAXI = "taxi"
    BIKE = "bike"


class Pace(StrEnum):
    """节奏：一天塞几个点。模糊的字眼交给 AI，接口只负责把选项限定死。"""

    RELAXED = "relaxed"
    NORMAL = "normal"
    PACKED = "packed"


class StartPoint(BaseModel):
    """出发地。坐标用 WGS84，和契约里的 lat/lon 一致。

    契约（openapi PlanInput.start_point）没把 lat/lon 标 required：前端只让用户
    填地名时没有坐标。这里必须同样可选，否则一填出发地就 422。
    """

    model_config = ConfigDict(extra="forbid")

    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    label: str | None = Field(default=None, max_length=128)


class PlanIn(BaseModel):
    """POST /plans 的请求体（契约 PlanInput）。

    extra="forbid"：拼错字段名要报错，而不是悄悄忽略——用户以为加了"想去岳麓山"，
    实际被丢掉却照样出结果，这比直接报错难查得多。
    """

    model_config = ConfigDict(extra="forbid")

    city: str = Field(min_length=1, max_length=64)
    days: int = Field(ge=1, le=7, description="天数，1-7")
    people: int = Field(ge=1, le=12, description="人数，1-12")
    budget_total: int = Field(ge=0, description="总预算，单位：分")
    include_lodging: bool = False
    day_parts: list[DayPart] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    transport: list[TransportMode] = Field(default_factory=list)
    pace: Pace = Pace.NORMAL
    start_point: StartPoint | None = None

    def preferences(self) -> dict[str, Any]:
        """把"软条件"原样存进 itineraries.preferences。

        这些条件不影响建表，只影响 AI 怎么排行程；存成 JSON 一是不用为每个新
        偏好都写一次迁移，二是生成时可以整块塞给模型。
        """
        return {
            "include_lodging": self.include_lodging,
            "day_parts": [part.value for part in self.day_parts],
            "themes": list(self.themes),
            "transport": [mode.value for mode in self.transport],
            "pace": self.pace.value,
            "start_point": self.start_point.model_dump() if self.start_point else None,
        }


class PlanOut(BaseModel):
    """POST /plans 的 202 响应（契约 Plan）。

    只回执"收了、开始生成了"，不含日程：此时 AI 还没跑，回内容只会是假的。
    run_id 是这次生成的编号：前端拿它判断"我等的还是不是同一份"，
    运维排障时也靠它把一次请求和后面的四步 agent_steps 串起来。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID | None = None
    state: PlanState
    city: str
    days: int
    people: int
    budget_total: int
    share_token: str | None = None
    error_code: str | None = None
    created_at: datetime

    @classmethod
    def from_itinerary(
        cls,
        itinerary: Itinerary,
        *,
        run_id: uuid.UUID | None = None,
    ) -> PlanOut:
        """从行程实体拼出回执。

        不走 model_validate(itinerary)：run_id 在 plan_runs 表上，不在行程上，
        直接把两个实体的字段拼在一起，比给行程塞一个临时属性干净。
        """
        return cls(
            id=itinerary.id,
            run_id=run_id,
            state=itinerary.state,
            city=itinerary.city,
            days=itinerary.days,
            people=itinerary.people,
            budget_total=itinerary.budget_total,
            share_token=itinerary.share_token,
            error_code=itinerary.error_code,
            created_at=itinerary.created_at,
        )


class PlansPageOut(BaseModel):
    """GET /plans 的响应（契约里是内联对象 {items, next_cursor}）。

    items 复用 PlanOut：列表页和回执看到的是同一张行程，字段不允许长不一样。
    """

    items: list[PlanOut] = Field(default_factory=list)
    next_cursor: str | None = None


class PlanStepOut(BaseModel):
    """一个 Agent 的留痕（契约 PlanDetail.steps）。没跑完的步骤 duration_ms 为空。"""

    model_config = ConfigDict(from_attributes=True)

    agent: str
    status: str
    duration_ms: int | None = None


class PlaceBriefOut(BaseModel):
    """行程项里的点位摘要（契约 Place 的子集）。

    只带"要在页面上显示的一行字"需要的字段：名字、区域、坐标、一句话简介、标签。
    不嵌 facts/sources：逐日安排是列表页级别的信息，把每条说法都塞进来会让
    一份三天行程的响应膨胀到几百 KB。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    district: str | None = None
    lat: float = 0.0
    lon: float = 0.0
    summary: str | None = None
    culture_tags: list[str] = Field(default_factory=list)
    status: str = "draft"


class PlanItemOut(BaseModel):
    """一天里的一项安排（契约 PlanDetail.days_plan[].items[]）。

    `price_min`/`price_max` 为 null 表示"没有可用价格"，前端据此显示「待核实」；
    `confidence` 不是 verified 时必须显示「待核实」标记，`reason` 说明为什么。
    """

    model_config = ConfigDict(from_attributes=True)

    order: int
    place: PlaceBriefOut | None = None
    stay_minutes: int = 0
    transport: str | None = None
    price_min: int | None = None
    price_max: int | None = None
    confidence: str = UNVERIFIED
    reason: str = REASON_UNKNOWN


class PlanDayOut(BaseModel):
    """一天的安排（契约 PlanDetail.days_plan[]）。day_index 从 1 开始。"""

    model_config = ConfigDict(from_attributes=True)

    day_index: int
    items: list[PlanItemOut] = Field(default_factory=list)


class CostSummaryOut(BaseModel):
    """费用汇总（契约 CostSummary），金额单位一律是「分」。

    `unknown_count` 单列：文档要求「缺失项单独计数，不假装是 0」。
    """

    min_total: int = 0
    max_total: int = 0
    currency: str = "CNY"
    unknown_count: int = 0


class PlanDetailOut(PlanOut):
    """GET /plans/{plan_id} 的响应（契约 PlanDetail）。

    前端轮询的就是这个：state 变了说明生成有了进展，steps 变长说明跑到第几站了；
    days_plan 是逐日安排，cost 是费用汇总（PL-06）。
    """

    steps: list[PlanStepOut] = Field(default_factory=list)
    cost: CostSummaryOut = Field(default_factory=CostSummaryOut)
    days_plan: list[PlanDayOut] = Field(default_factory=list)


class ToDraftOut(BaseModel):
    """POST /plans/{plan_id}/to-draft 的响应（契约里是内联对象，只有 draft_id）。

    只回编号不回整份草稿：草稿详情由 routes 模块的 GET /route-drafts/{id} 提供，
    两个模块各管自己的形状，避免 planner 复制一份别人的 DTO。
    """

    draft_id: uuid.UUID
