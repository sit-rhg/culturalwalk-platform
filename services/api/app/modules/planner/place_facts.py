# 行程费用 · 从 catalog 取「带来源的说法」
"""把 catalog 的点位读成价格判定需要的样子，并把「取不到」当正常情况处理。

为什么不直接用 `PlaceDetailOut`：planner 只需要「名称 + 坐标 + 若干条带可信度的
价格说法」，用它自己的轻量结构，catalog 的字段增删就不会撞到这里；测试也不必
先造一个完整的 catalog 响应。

为什么 `catalog_place_source` 要探测 `get_place` 是否存在：PL-06 的代码叠在
PL-05 之上，而 CA-03（catalog 查询接口）是另一条链上的卡，合入顺序不确定。
方法不在时**退回「查不到任何事实」**而不是抛异常——查不到事实的结果是所有项目
都标「待核实」，这是符合验收口径的降级，而不是 500。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.modules.catalog.service import CatalogService
from app.modules.planner.pricing import UNVERIFIED


@dataclass(frozen=True)
class ClaimView:
    """一条「说法」：内容 + 可信度 + 来源链接。"""

    claim: str
    confidence: str = UNVERIFIED
    source_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlaceView:
    """价格判定需要的点位信息（契约 Place 的子集 + 说法列表）。"""

    id: uuid.UUID
    name: str
    district: str | None = None
    lat: float = 0.0
    lon: float = 0.0
    summary: str | None = None
    culture_tags: tuple[str, ...] = ()
    status: str = "draft"
    claims: tuple[ClaimView, ...] = field(default=())

    def with_claims(self, claims: tuple[ClaimView, ...]) -> PlaceView:
        """换一组说法，其余字段照抄（测试里造点位时省掉重复参数）。"""
        return PlaceView(
            id=self.id,
            name=self.name,
            district=self.district,
            lat=self.lat,
            lon=self.lon,
            summary=self.summary,
            culture_tags=self.culture_tags,
            status=self.status,
            claims=claims,
        )


# 给 planner 用的一行签名：给点位 id，返回点位（含说法）；查不到返回 None。
PlaceFactsSource = Callable[[uuid.UUID], PlaceView | None]


def _missing(_place_id: uuid.UUID) -> PlaceView | None:
    """catalog 还没提供查询能力时的兜底：永远查不到。"""
    return None


def catalog_place_source(db: Session) -> PlaceFactsSource:
    """造一个「查 catalog」的函数；catalog 尚不可用时返回恒为 None 的版本。"""
    service = CatalogService(db)
    # noqa: B009 —— CA-03（catalog 查询接口）可能还没合进来，方法在不在要运行时看。
    get_place = getattr(service, "get_place", None)  # noqa: B009
    if not callable(get_place):
        return _missing

    def load(place_id: uuid.UUID) -> PlaceView | None:
        try:
            detail = get_place(place_id)
        except AppError:
            # 点位不存在 / 未发布：对行程来说只是"这一项没有资料"，
            # 不该让整次生成失败，所以吞掉业务错误、返回 None。
            return None
        return _to_view(detail)

    return load


def _confidence_value(raw: Any) -> str:
    """把 catalog 给的可信度统一成字符串。

    catalog 现在存的是字符串；但如果哪天换成 StrEnum，`str(FactConfidence.VERIFIED)`
    会得到 "FactConfidence.VERIFIED"，价格判断就会全部掉进「待核实」分支——
    不报错，只是悄悄降级。这里先把枚举拆成 .value，别把这种事留给运气。
    """
    value = getattr(raw, "value", raw)
    text = str(value or "").strip()
    return text or UNVERIFIED


def _to_view(detail: Any) -> PlaceView:
    """把 catalog 的详情对象转成本模块的轻量结构。

    全部走 `getattr`：这条链的两个模块各自演进，字段少一个不该炸整次生成。
    """
    facts = list(getattr(detail, "facts", None) or [])
    claims = tuple(
        ClaimView(
            claim=str(getattr(fact, "claim", "") or ""),
            confidence=_confidence_value(getattr(fact, "confidence", None)),
            source_urls=tuple(
                str(url)
                for source in (getattr(fact, "sources", None) or [])
                if (url := getattr(source, "url", None))
            ),
        )
        for fact in facts
    )
    return PlaceView(
        id=detail.id,
        name=str(getattr(detail, "name", "") or ""),
        district=getattr(detail, "district", None),
        lat=float(getattr(detail, "lat", 0.0) or 0.0),
        lon=float(getattr(detail, "lon", 0.0) or 0.0),
        summary=getattr(detail, "summary", None),
        culture_tags=tuple(str(tag) for tag in (getattr(detail, "culture_tags", None) or [])),
        status=str(getattr(detail, "status", "draft") or "draft"),
        claims=claims,
    )
