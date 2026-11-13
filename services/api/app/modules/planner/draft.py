# 行程规划 · 转路线草稿（跨模块翻译层）
"""把一张生成好的行程，翻译成 routes 模块能收下的草稿。

为什么单独一层：
- 跨模块只许调对方 service，不许 import 对方 models（见 docs/必读1-开发约定.md 的依赖方向），
  所以这里只产出"纯数据"（DraftSpec / DraftNode），由 port 负责换成对方的 DTO 并调用。
- routes 模块的节点写入（PUT /route-drafts/{id}/nodes，RT-02）还没落地，
  这里用 getattr 探测：有就写，没有就先只建草稿，不把 planner 卡死在别人的卡上。

映射口径（验收「生成草稿且节点顺序与来源保留」就钉在这三件事上）：
- 顺序：先按 day_index，再按当天 order，逐项搬运，绝不重排。
- 来源：每项带 source_ids，原样保留，供投稿审核时逐条核对。
- 价格：price_min/max 原样搬（分为单位），缺失就是 None，不填 0 假装免费。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from app.core.errors import AppError, ServiceUnavailable
from app.modules.planner.models import (
    TRANSPORT_VALUES,
    Itinerary,
    ItineraryDay,
    ItineraryItem,
)

TITLE_MAX: Final[int] = 60
SUMMARY_MAX: Final[int] = 300
MINUTES_PER_LEG: Final[int] = 15
STAY_MIN: Final[int] = 10
STAY_MAX: Final[int] = 480
MAX_THEMES: Final[int] = 8
# 幂等键里带多少位内容摘要：够区分「改过」和「没改」，又不至于把 key 撑到 64 位上限。
KEY_HASH_CHARS: Final[int] = 16

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DraftNode:
    """路线草稿里的一个节点（对应契约 RouteNodeInput 的字段子集）。"""

    place_id: uuid.UUID
    stay_minutes: int
    transport: str | None = None
    price_min: int | None = None
    price_max: int | None = None
    source_ids: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class DraftSpec:
    """一份待创建的草稿：标题摘要这些"壳"字段 + 有序节点。"""

    title: str
    summary: str
    theme_tags: tuple[str, ...]
    duration_minutes: int
    nodes: tuple[DraftNode, ...]


class RouteDraftPort(Protocol):
    """planner 视角下的 routes 模块能力（测试里用假对象替换）。"""

    def create(
        self, *, owner_id: uuid.UUID, spec: DraftSpec, idempotency_key: str
    ) -> uuid.UUID: ...

    def replace_nodes(
        self,
        *,
        draft_id: uuid.UUID,
        owner_id: uuid.UUID,
        nodes: Sequence[DraftNode],
        idempotency_key: str,
    ) -> None: ...


# ----- 字段映射（纯函数，好测） ----------------------------------------------


def draft_title(itinerary: Itinerary) -> str:
    """「长沙 3 天文化路线」这种能一眼认出来的标题。

    城市名是用户自己写的，可能超长；截断保留城市，不能截成半个词。
    """
    city = (itinerary.city or "未命名城市").strip() or "未命名城市"
    days = itinerary.days or 1
    suffix = f" {days} 天文化路线"
    room = TITLE_MAX - len(suffix)
    if room < 1:  # 天数长到连后缀都放不下（理论上 days<=7，留着防以后放宽）
        return suffix.strip()[:TITLE_MAX]
    # 只砍城市名，后缀整段保留：直接 [:TITLE_MAX] 会把「文化路线」切成「文化」。
    return f"{city[:room].rstrip()}{suffix}"


def draft_summary(itinerary: Itinerary, nodes: Sequence[DraftNode]) -> str:
    """摘要只写事实：天数、人数、预算、点位数量、待核实数量。

    不写"精心策划"这类营销话术——草稿还没过审核，不该先夸自己。
    """
    city = (itinerary.city or "未命名城市").strip() or "未命名城市"
    parts = [f"由行程规划生成：{city} {itinerary.days or 1} 天"]
    if itinerary.people:
        parts.append(f"{itinerary.people} 人")
    if itinerary.budget_total is not None:
        parts.append(f"预算 ¥{itinerary.budget_total // 100}")
    summary = "，".join(parts) + f"。共 {len(nodes)} 个点位"
    pending = sum(1 for node in nodes if node.price_min is None and node.price_max is None)
    if pending:
        summary += f"，其中 {pending} 个点位价格待核实"
    return (summary + "。")[:SUMMARY_MAX]


def draft_themes(itinerary: Itinerary) -> tuple[str, ...]:
    """主题标签取用户提交的条件里的 themes，去空白、保序去重、限量。"""
    themes = (itinerary.preferences or {}).get("themes") or []
    seen: dict[str, None] = {}
    for theme in themes:
        if not isinstance(theme, str):
            continue
        cleaned = theme.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return tuple(list(seen)[:MAX_THEMES])


def _clamp_stay(value: object) -> int:
    """停留时间夹到契约允许的 10~480 分钟。

    生成侧偶尔给出 0 或超长值，直接塞进草稿会被 routes 的校验拒掉，
    不如在这里夹一下——用户看到的是"这一站待了半小时"，而不是 422。
    """
    try:
        minutes = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return STAY_MIN
    return max(STAY_MIN, min(STAY_MAX, minutes))


def _normalize_transport(value: str | None) -> str | None:
    if value in TRANSPORT_VALUES:
        return value
    return None


def _node_of(item: ItineraryItem) -> DraftNode | None:
    """一项行程内容转成节点；没有点位关联的项（比如"自由活动"）不搬。"""
    if item.place_id is None:
        return None
    sources = item.source_ids or []
    return DraftNode(
        place_id=item.place_id,
        stay_minutes=_clamp_stay(item.stay_minutes),
        transport=_normalize_transport(item.transport),
        price_min=item.price_min,
        price_max=item.price_max,
        source_ids=tuple(str(source) for source in sources),
        note=item.note,
    )


def draft_nodes(days: Iterable[ItineraryDay]) -> tuple[DraftNode, ...]:
    """按「天 → 当天顺序」搬点位。顺序是验收项，任何重排都会让草稿和用户看到的不一致。"""
    nodes: list[DraftNode] = []
    for day in sorted(days, key=lambda row: row.day_index):
        for item in sorted(day.items, key=lambda row: row.order):
            node = _node_of(item)
            if node is not None:
                nodes.append(node)
    return tuple(nodes)


def draft_duration_minutes(nodes: Sequence[DraftNode]) -> int:
    """总时长 = 各站停留 + 每段挪动 15 分钟（最后一段不算，没有"挪到终点"这回事）。"""
    if not nodes:
        return 0
    stay = sum(node.stay_minutes for node in nodes)
    legs = len(nodes) - 1
    return stay + legs * MINUTES_PER_LEG


def draft_spec(itinerary: Itinerary, days: Iterable[ItineraryDay]) -> DraftSpec:
    nodes = draft_nodes(days)
    return DraftSpec(
        title=draft_title(itinerary),
        summary=draft_summary(itinerary, nodes),
        theme_tags=draft_themes(itinerary),
        duration_minutes=draft_duration_minutes(nodes),
        nodes=nodes,
    )


def routes_idempotency_key(plan_id: uuid.UUID, input_hash: str | None = None) -> str:
    """同一份行程内容反复点「生成草稿」，必须落到同一份草稿上。

    只用 plan_id 不够：用户把行程改了内容再转草稿时，routes 那边会看到「同 key 不同内容」
    并报 IDEMPOTENCY_KEY_REUSED（409），从用户角度就是「我改了方案，它却不让我转」。
    把内容摘要截一段并进来，内容变了就是新 key，等于 fork 一份新草稿——
    和必读1「改动 = fork 新草稿」是同一条规矩。

    客户端 HTTP 头里的 Idempotency-Key 只负责网络重试级去重（同一请求别落两次），
    业务上的「同一张行程只该有一份草稿」靠这把派生键，不靠用户传什么。
    """
    base = f"plan:{plan_id}"
    if not input_hash:
        return base
    return f"{base}:{input_hash[:KEY_HASH_CHARS]}"


# ----- 真实适配器 -------------------------------------------------------------


class RoutesDraftPort:
    """走 routes 模块的 service 建草稿；本分支上 RT-01 还没合进来时给明确报错。"""

    def __init__(self, db: object) -> None:
        self.db = db

    def _service(self) -> object:
        # 延迟 import：本分支（stacked 在 PL-05 上）的 routes 还是空壳，
        # 模块级 import 不会报错，但没有 create 方法，所以下面用 getattr 探测。
        from app.modules.routes.service import RoutesService

        return RoutesService(self.db)  # type: ignore[arg-type]

    def create(self, *, owner_id: uuid.UUID, spec: DraftSpec, idempotency_key: str) -> uuid.UUID:
        service = self._service()
        create = getattr(service, "create", None)
        if create is None:
            raise ServiceUnavailable(
                "路线模块还没就绪，暂时不能生成草稿",
                code="ROUTES_UNAVAILABLE",
                details=[{"field": "routes", "reason": "routes 模块没有提供 create"}],
            )

        from app.modules.routes.schemas import RouteDraftCreate

        payload = RouteDraftCreate(
            title=spec.title,
            summary=spec.summary,
            theme_tags=list(spec.theme_tags),
            duration_minutes=spec.duration_minutes,
        )
        draft = create(owner_id=owner_id, payload=payload, idempotency_key=idempotency_key)
        if isinstance(draft, uuid.UUID):
            return draft
        draft_id = getattr(draft, "id", None)
        if not isinstance(draft_id, uuid.UUID):
            raise ServiceUnavailable(
                "路线模块返回的草稿没有编号",
                code="ROUTES_UNAVAILABLE",
                details=[{"field": "routes", "reason": "返回值既不是 UUID 也没有 id 字段"}],
            )
        return draft_id

    def _lock_version_of(self, service: object, *, draft_id: uuid.UUID, owner_id: uuid.UUID) -> int:
        """读草稿当前版本号，给 replace_nodes 的 if_match 用。

        不写死 1：重放或用户改过标题之后 lock_version 会变，拿旧值会被判成 412；
        读不到就退回 1——新建草稿本来就是 1，这时猜也不会错。
        """
        detail = getattr(service, "get_detail", None)
        if detail is None:
            return 1
        try:
            loaded = detail(draft_id=draft_id, owner_id=owner_id)
        except AppError:  # 404/403 这类预期内的失败：草稿刚建，按版本 1 试一次
            return 1
        return int(getattr(loaded, "lock_version", 1) or 1)

    def replace_nodes(
        self,
        *,
        draft_id: uuid.UUID,
        owner_id: uuid.UUID,
        nodes: Sequence[DraftNode],
        idempotency_key: str,
    ) -> None:
        """把节点按映射出来的顺序写进草稿（RT-02 的 PUT /route-drafts/{id}/nodes）。

        RT-02 的真实签名是 replace_nodes(
            *, draft_id, owner_id, payload: RouteNodesReplace, if_match, idempotency_key)
        所以要显式给 payload（整表替换）和 if_match（乐观锁版本）。
        顺序就是数组下标：nodes 已按「天 → order」排好，这里原样传、不再重排——
        重排会直接改掉卡面验收的「节点顺序与来源保留」。

        对方没提供这个方法时静默跳过，RT-02 落地后自动接通（有用例钉住真实链路）。
        """
        service = self._service()
        replace = getattr(service, "replace_nodes", None)
        if replace is None:
            # 不静默：草稿建好了但节点没写进去，事后排查必须能从这里看出原因。
            logger.warning("routes 模块没有 replace_nodes，节点未写入 draft=%s", draft_id)
            return

        from app.modules.routes.schemas import RouteNodeInput, RouteNodesReplace

        payload = RouteNodesReplace(
            nodes=[
                RouteNodeInput(
                    place_id=node.place_id,
                    stay_minutes=node.stay_minutes,
                    transport=node.transport,  # type: ignore[arg-type]
                    note=node.note,
                    price_min=node.price_min,
                    price_max=node.price_max,
                    source_ids=list(node.source_ids),
                )
                for node in nodes
            ]
        )
        replace(
            draft_id=draft_id,
            owner_id=owner_id,
            payload=payload,
            if_match=self._lock_version_of(service, draft_id=draft_id, owner_id=owner_id),
            idempotency_key=idempotency_key,
        )


def routes_draft_port(db: object) -> RouteDraftPort:
    """默认用真实 routes 模块；测试里替换这个函数即可注入假 port。"""
    return RoutesDraftPort(db)
