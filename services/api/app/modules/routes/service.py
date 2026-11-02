# 路线与版本 · 业务规则
"""本模块唯一允许写业务判断的地方。router 不许写 if/else 业务逻辑。

拥有数据：Route, RouteDraft, RouteNode, RouteVersion
明确不做：审核、发布、AI 生成
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import Conflict, Forbidden, NotFound, PreconditionFailed, ValidationFailed
from app.core.pagination import decode_cursor, encode_cursor
from app.modules.catalog.schemas import PlaceOut
from app.modules.catalog.service import CatalogService
from app.modules.routes.models import DraftState, RouteDraft, RouteNode
from app.modules.routes.repository import RoutesRepository
from app.modules.routes.schemas import (
    CostSummaryOut,
    RouteDraftCreate,
    RouteDraftDetailOut,
    RouteDraftOut,
    RouteDraftPatch,
    RouteNodeInput,
    RouteNodeOut,
    RouteNodesReplace,
)

# 与 catalog 的 PlaceStatus.VERIFIED 落库值一致（小写）。这里不 import 别人的
# ORM 枚举，避免 routes 依赖 catalog 的表定义；值变化由契约测试兜住。
_PUBLISHED_PLACE_STATUS = "verified"


def _input_hash(payload: BaseModel, *, exclude_unset: bool = False) -> str:
    """把请求体压成稳定指纹（sha256 十六进制）。

    sort_keys + 紧凑分隔符：字段顺序、空格不同都得到同一个值。
    exclude_unset 用于 PATCH——只对"真正带上来的字段"算指纹，
    否则两次 PATCH 只是没提到的字段不同就会被判成同一个请求。
    """
    raw = json.dumps(
        payload.model_dump(mode="json", exclude_unset=exclude_unset),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _clean_tags(tags: list[str]) -> list[str]:
    """去空白、去空串、保序去重：前端多带一个空标签不至于写成 ["", "历史"]。"""
    seen: dict[str, None] = {}
    for tag in tags:
        cleaned = tag.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


class RoutesService:
    """依赖注入的官方入口；外部模块只能通过它访问本模块能力。"""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = RoutesRepository(db)

    # ===== 草稿列表 =====
    def list_mine(
        self,
        *,
        owner_id: uuid.UUID,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[RouteDraft], str | None]:
        after = decode_cursor(cursor) if cursor else None
        # 多取一条判断还有没有下一页：比 count(*) 便宜，且不会因为并发插入错位。
        rows = self.repo.list_by_owner(owner_id=owner_id, after=after, limit=limit + 1)
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
        return page, next_cursor

    # ===== 新建草稿 =====
    def create(
        self,
        *,
        owner_id: uuid.UUID,
        payload: RouteDraftCreate,
        idempotency_key: str,
    ) -> RouteDraft:
        request_hash = _input_hash(payload)

        existing = self.repo.get_by_owner_and_key(
            owner_id=owner_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            return self._replay_create(existing, request_hash)

        title = payload.title.strip()
        # 和 PATCH 同一条规则：长度校验看的是原串，全空白能过 min_length=1，
        # 落库前必须再判一次，否则会建出一份空标题草稿。
        if not title:
            raise ValidationFailed(
                "标题不能为空", details=[{"field": "title", "reason": "不能全为空白"}]
            )

        draft = RouteDraft(
            owner_id=owner_id,
            title=title,
            summary=payload.summary,
            theme_tags=_clean_tags(payload.theme_tags),
            duration_minutes=payload.duration_minutes,
            # 显式给 EDITING / 1：列默认值要等 INSERT 才生效，构造后立刻读是 None。
            state=DraftState.EDITING,
            lock_version=1,
            idempotency_key=idempotency_key,
            input_hash=request_hash,
        )
        try:
            self.repo.add(draft)
        except IntegrityError:
            # 并发下两个请求都通过了"先查"：后到的撞唯一约束，回滚后取先落库的那份。
            self.db.rollback()
            winner = self.repo.get_by_owner_and_key(
                owner_id=owner_id, idempotency_key=idempotency_key
            )
            if winner is None:  # 撞的是别的约束，不是幂等键
                raise
            return self._replay_create(winner, request_hash)

        self.db.refresh(draft)  # created_at/updated_at 由数据库默认值生成
        return draft

    def _replay_create(self, draft: RouteDraft, request_hash: str) -> RouteDraft:
        """同一个 key 的重复创建：内容一致就还原来那份，不一致报 409。

        不一致不能将错就错：用户以为改的是标题，拿到的却是第一次的草稿。
        """
        if draft.input_hash != request_hash:
            raise Conflict(
                "这个 Idempotency-Key 已经用于另一份不同的草稿内容，请换一个新的 key",
                code="IDEMPOTENCY_KEY_REUSED",
                details=[
                    {
                        "field": "Idempotency-Key",
                        "reason": "already_used_with_different_body",
                    }
                ],
            )
        return draft

    # ===== 草稿详情 =====
    def get_detail(self, *, draft_id: uuid.UUID, owner_id: uuid.UUID) -> RouteDraftDetailOut:
        draft = self._load_owned(draft_id=draft_id, owner_id=owner_id)
        return self._detail(draft)

    # ===== 更新草稿 =====
    def patch(
        self,
        *,
        draft_id: uuid.UUID,
        owner_id: uuid.UUID,
        payload: RouteDraftPatch,
        if_match: int,
        idempotency_key: str,
    ) -> RouteDraft:
        draft = self._load_owned(draft_id=draft_id, owner_id=owner_id)
        request_hash = _input_hash(payload, exclude_unset=True)
        # 幂等重放排在状态检查之前：先成功的写、事后再被锁定，
        # 重试仍该拿到那次成功的结果，而不是 409「已提交不能再改」。
        if draft.patch_key == idempotency_key:
            if draft.patch_hash != request_hash:
                raise Conflict(
                    "这个 Idempotency-Key 已经用于另一份不同的修改，请换一个新的 key",
                    code="IDEMPOTENCY_KEY_REUSED",
                    details=[
                        {
                            "field": "Idempotency-Key",
                            "reason": "already_used_with_different_body",
                        }
                    ],
                )
            return draft

        if draft.state is not DraftState.EDITING:
            raise Conflict(
                "草稿已提交或已归档，不能再修改；需要改请新建草稿",
                code="DRAFT_NOT_EDITABLE",
            )

        if if_match != draft.lock_version:
            raise PreconditionFailed(
                "草稿已被其他窗口修改，请刷新后再改",
                code="STALE_DRAFT_VERSION",
                details=[{"field": "If-Match", "reason": "lock_version 已变化"}],
            )

        values = self._patch_values(payload)
        values["patch_key"] = idempotency_key
        values["patch_hash"] = request_hash
        updated = self.repo.patch_cas(draft_id=draft_id, expected_version=if_match, values=values)
        if updated == 0:
            # 检查之后、写入之前被别人抢先改了：同样是版本冲突，不是 500。
            raise PreconditionFailed(
                "草稿已被其他窗口修改，请刷新后再改",
                code="STALE_DRAFT_VERSION",
                details=[{"field": "If-Match", "reason": "lock_version 已变化"}],
            )

        self.db.refresh(draft)
        return draft

    # ===== 节点整表替换 =====
    def replace_nodes(
        self,
        *,
        draft_id: uuid.UUID,
        owner_id: uuid.UUID,
        payload: RouteNodesReplace,
        if_match: int,
        idempotency_key: str,
    ) -> RouteDraftDetailOut:
        draft = self._load_owned(draft_id=draft_id, owner_id=owner_id)
        request_hash = _input_hash(payload)
        # 和 PATCH 一样，重放判断排在状态检查之前：成功提交后草稿被锁定，
        # 客户端拿同一个 key 重试，应该拿到那次成功的结果，而不是 409。
        if draft.nodes_key == idempotency_key:
            if draft.nodes_hash != request_hash:
                raise Conflict(
                    "这个 Idempotency-Key 已经用于另一份不同的节点，请换一个新的 key",
                    code="IDEMPOTENCY_KEY_REUSED",
                    details=[
                        {
                            "field": "Idempotency-Key",
                            "reason": "already_used_with_different_body",
                        }
                    ],
                )
            return self._detail(draft)

        if draft.state is not DraftState.EDITING:
            raise Conflict(
                "草稿已提交或已归档，不能再调整节点；需要改请新建草稿",
                code="DRAFT_NOT_EDITABLE",
            )

        if if_match != draft.lock_version:
            raise PreconditionFailed(
                "草稿已被其他窗口修改，请刷新后再改",
                code="STALE_DRAFT_VERSION",
                details=[{"field": "If-Match", "reason": "lock_version 已变化"}],
            )

        # 先把地点全部解析一遍再动库：有一个不存在就整单 422，
        # 不能让前几个节点落库、后一个失败，留下半份路线。
        places = self._resolve_places(payload.nodes)
        updated = self.repo.patch_cas(
            draft_id=draft_id,
            expected_version=if_match,
            values={"nodes_key": idempotency_key, "nodes_hash": request_hash},
        )
        if updated == 0:
            raise PreconditionFailed(
                "草稿已被其他窗口修改，请刷新后再改",
                code="STALE_DRAFT_VERSION",
                details=[{"field": "If-Match", "reason": "lock_version 已变化"}],
            )

        self.repo.replace_nodes(
            draft_id,
            [
                RouteNode(
                    draft_id=draft_id,
                    place_id=node.place_id,
                    # 顺序由数组下标决定，服务端重排 1..N；客户端不能自带 position。
                    position=position,
                    stay_minutes=node.stay_minutes,
                    transport=node.transport,
                    note=node.note,
                    price_min=node.price_min,
                    price_max=node.price_max,
                )
                for position, node in enumerate(payload.nodes, start=1)
            ],
        )
        self.db.refresh(draft)
        return self._detail(draft, places=places)

    def _resolve_places(self, nodes: list[RouteNodeInput]) -> dict[uuid.UUID, PlaceOut]:
        """把节点引用的地点解析成公开 DTO；有一个不存在就整体 422。

        走 CatalogService 而不是直接查表：是否已发布、草稿一律 404 的口径
        只有 catalog 自己说了算，routes 不该复制一份过滤条件。
        """
        catalog = CatalogService(self.db)
        resolved: dict[uuid.UUID, PlaceOut] = {}
        for index, node in enumerate(nodes):
            if node.place_id in resolved:
                continue
            try:
                place = PlaceOut.model_validate(catalog.get_place(node.place_id))
            except NotFound as exc:
                raise ValidationFailed(
                    "节点引用的地点不存在或尚未发布",
                    details=[
                        {
                            # 与 FastAPI 校验错误的 field 口径一致（点号分层），
                            # 前端才能用同一个 setFieldError 处理两种 422。
                            "field": f"nodes.{index}.place_id",
                            "reason": "地点不存在或未发布",
                        }
                    ],
                ) from exc
            # get_place 在 catalog 的「公开读只读已发布」落地前仍可能返回草稿；
            # 路线节点必须是已发布地点，这里再挡一次（等 PR #95 合入后是双保险）。
            if place.status != _PUBLISHED_PLACE_STATUS:
                raise ValidationFailed(
                    "节点引用的地点不存在或尚未发布",
                    details=[
                        {
                            "field": f"nodes.{index}.place_id",
                            "reason": "地点不存在或未发布",
                        }
                    ],
                )
            resolved[node.place_id] = place
        return resolved

    def _patch_values(self, payload: RouteDraftPatch) -> dict[str, Any]:
        """只把请求里真正出现的字段转成列更新；没出现的保持原值。"""
        values: dict[str, Any] = {}
        if "title" in payload.model_fields_set:
            title = (payload.title or "").strip()
            if not title:
                raise ValidationFailed(
                    "标题不能为空", details=[{"field": "title", "reason": "不能全为空白"}]
                )
            values["title"] = title
        if "summary" in payload.model_fields_set:
            values["summary"] = payload.summary
        if "theme_tags" in payload.model_fields_set:
            values["theme_tags"] = _clean_tags(payload.theme_tags or [])
        if "duration_minutes" in payload.model_fields_set:
            values["duration_minutes"] = payload.duration_minutes
        return values

    # ===== 删除草稿 =====
    def delete(self, *, draft_id: uuid.UUID, owner_id: uuid.UUID) -> None:
        draft = self._load_owned(draft_id=draft_id, owner_id=owner_id)
        if draft.state is not DraftState.EDITING or draft.route_id is not None:
            raise Conflict(
                "只有还在编辑中、且从未发布的草稿可以删除",
                code="DRAFT_NOT_DELETABLE",
            )
        self.repo.delete(draft)

    # ===== 内部 =====
    def _detail(
        self,
        draft: RouteDraft,
        *,
        places: dict[uuid.UUID, PlaceOut] | None = None,
    ) -> RouteDraftDetailOut:
        """把草稿 + 节点渲染成详情。

        places 是本次写入已经解析过的缓存，省掉重复查询；读详情时按需解析。
        地点在加入草稿之后被下线/删除的节点直接跳过——不能 500，否则作者
        连打开草稿去替换它的机会都没有。
        """
        cache: dict[uuid.UUID, PlaceOut | None] = dict(places or {})
        catalog = CatalogService(self.db)
        nodes: list[RouteNodeOut] = []
        for node in self.repo.list_nodes(draft.id):
            place = cache.get(node.place_id)
            if place is None and node.place_id not in cache:
                try:
                    place = PlaceOut.model_validate(catalog.get_place(node.place_id))
                except NotFound:
                    place = None
                cache[node.place_id] = place
            if place is None:
                continue
            nodes.append(
                RouteNodeOut(
                    position=node.position,
                    place=place,
                    stay_minutes=node.stay_minutes,
                    transport=node.transport,
                    note=node.note,
                    price_min=node.price_min,
                    price_max=node.price_max,
                )
            )
        base = RouteDraftOut.model_validate(draft)
        return RouteDraftDetailOut(**base.model_dump(), nodes=nodes, cost=_cost_of(nodes))

    def _load_owned(self, *, draft_id: uuid.UUID, owner_id: uuid.UUID) -> RouteDraft:
        draft = self.repo.get(draft_id)
        if draft is None:
            raise NotFound("草稿不存在", code="DRAFT_NOT_FOUND")
        if draft.owner_id != owner_id:
            # 不是 404：草稿 ID 可能是别人分享出来的，明确告诉他无权访问更安全。
            raise Forbidden("只能操作自己的草稿", code="DRAFT_FORBIDDEN")
        return draft


def _cost_of(nodes: list[RouteNodeOut]) -> CostSummaryOut:
    """按已知价格求和；缺任一端的价格算 unknown，而不是按 0 计入总额。

    把缺失当 0 会让「预算 300」的路线看起来比实际便宜，所以单独计数，
    前端可以提示「还有 N 站未填价格」。
    """
    unknown = sum(1 for node in nodes if node.price_min is None or node.price_max is None)
    return CostSummaryOut(
        min_total=sum(node.price_min for node in nodes if node.price_min is not None),
        max_total=sum(node.price_max for node in nodes if node.price_max is not None),
        unknown_count=unknown,
    )
