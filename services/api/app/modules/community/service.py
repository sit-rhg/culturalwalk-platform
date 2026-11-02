# 社区互动 · 业务规则
"""本模块唯一允许写业务判断的地方。router 不许写 if/else 业务逻辑。

拥有数据：Comment, Favorite
明确不做：路线内容修改、举报申诉（二期）

读路线的口径：routes 模块还没提供「已发布路线」的公开读服务（发布 RT-05 也尚未落地），
当前把 `route_versions.route_id 非空` 当作已发布标记；草稿/待审的版本这列是 NULL，
所以列表看不到、详情按 404 处理，不泄露存在性。等 routes 侧补齐 service / publications
表，替换 repository 里的三段查询即可（见 repository.py 文件头）。
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.errors import AppError, Forbidden, NotFound, Unauthorized, ValidationFailed
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.modules.audit.service import AuditService
from app.modules.catalog.service import CatalogService
from app.modules.community.models import Comment, CommentStatus, Favorite
from app.modules.community.repository import CommentPosition, CommunityRepository
from app.modules.community.schemas import (
    AuthorOut,
    CommentListOut,
    CommentOut,
    RouteDayItemOut,
    RouteDayOut,
    RouteDetailOut,
    RouteSummaryOut,
    RouteVersionOut,
)
from app.modules.identity.service import IdentityService
from app.modules.routes.models import RouteVersion
from app.modules.routes.schemas import CostSummaryOut

MAX_COMMENT_LENGTH = 500
# 平台目前只收录长沙；按别的城市筛时返回空页而不是把全量当结果。
_CITY_ALIASES = frozenset({"changsha", "长沙", "长沙市"})
# 一次查询最多扫 10 批候选；防止「筛选条件极严 + 大表」时单次请求无限翻页。
_MAX_SCAN_BATCHES = 10


def _as_utc(value: datetime) -> datetime:
    """SQLite 取回来是 naive，出参统一按 UTC 解释，前端拿到的 ISO-8601 才带时区。"""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _cost_of(snapshot: dict[str, Any]) -> CostSummaryOut:
    """按快照里各节点的已知价格求和；缺任一端的价格算 unknown，不按 0 计入。

    与 routes 模块对草稿详情的口径一致：把缺失当 0 会让预算看起来比实际便宜。
    """
    nodes = [node for node in snapshot.get("nodes") or [] if isinstance(node, dict)]
    unknown = sum(
        1 for node in nodes if node.get("price_min") is None or node.get("price_max") is None
    )
    return CostSummaryOut(
        min_total=sum(
            int(node["price_min"]) for node in nodes if isinstance(node.get("price_min"), int)
        ),
        max_total=sum(
            int(node["price_max"]) for node in nodes if isinstance(node.get("price_max"), int)
        ),
        unknown_count=unknown,
    )


def _parse_duration(value: str | None) -> tuple[int | None, int | None] | None:
    """把 "0-120" / "120-" 解析成分钟区间；格式错按参数错误 400。"""
    if not value:
        return None
    low_text, _, high_text = value.partition("-")
    try:
        low = int(low_text) if low_text.strip() else None
        high = int(high_text) if high_text.strip() else None
    except ValueError as exc:
        raise AppError(
            "时长筛选格式应为 0-120",
            code="INVALID_DURATION_FILTER",
        ) from exc
    if low is None and high is None:
        raise AppError("时长筛选格式应为 0-120", code="INVALID_DURATION_FILTER")
    if low is not None and high is not None and low > high:
        raise AppError("时长筛选区间下限不能大于上限", code="INVALID_DURATION_FILTER")
    return low, high


def _actor_id(user: CurrentUser) -> uuid.UUID:
    """令牌里的 sub 不是 UUID 时等同登录失效（与 routes router 的处理一致）。"""
    try:
        return uuid.UUID(user.id)
    except ValueError as exc:
        raise Unauthorized("登录已失效，请重新登录") from exc


def _encode_comment_cursor(comment: Comment) -> str:
    """评论列表按「置顶 + 时间」排序，core 的两字段游标装不下 pinned，单独编一份。

    游标对前端不透明（docs/03 §1.5），字段是内部约定；改排序时要同步改解码。
    """
    raw = json.dumps(
        {
            "p": bool(comment.pinned),
            "c": _as_utc(comment.created_at).isoformat(),
            "i": str(comment.id),
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_comment_cursor(cursor: str) -> CommentPosition:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded))
        return bool(data["p"]), datetime.fromisoformat(data["c"]), uuid.UUID(str(data["i"]))
    except (KeyError, TypeError, ValueError) as exc:  # base64/JSON/时间/ID 任一坏掉
        raise AppError("分页游标无效", code="INVALID_CURSOR") from exc


@dataclass(frozen=True)
class _RouteFilter:
    """公开列表的筛选条件。JSON 快照字段跨库查询不可移植，统一在 Python 侧过滤。"""

    q: str | None = None
    theme: str | None = None
    duration: tuple[int | None, int | None] | None = None
    budget_max: int | None = None
    channel: str | None = None

    def matches(self, snapshot: dict[str, Any]) -> bool:
        if self.q:
            haystack = f"{snapshot.get('title') or ''}\n{snapshot.get('summary') or ''}"
            if self.q.casefold() not in haystack.casefold():
                return False
        if self.theme:
            tags = {str(tag).casefold() for tag in snapshot.get("theme_tags") or []}
            if self.theme.casefold() not in tags:
                return False
        if self.duration is not None:
            minutes = _optional_int(snapshot.get("duration_minutes"))
            if minutes is None:
                return False
            low, high = self.duration
            if low is not None and minutes < low:
                return False
            if high is not None and minutes > high:
                return False
        if self.budget_max is not None and _cost_of(snapshot).min_total > self.budget_max:
            return False
        # routes 表尚未落地，没有 channel 字段：所有路线都按用户投稿处理，
        # 因此筛 official 只能得到空页（不伪造官方身份）。
        return self.channel != "official"


class CommunityService:
    """依赖注入的官方入口；外部模块只能通过它访问本模块能力。"""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = CommunityRepository(db)
        self.audit = AuditService(db)
        self._usernames: dict[uuid.UUID, str | None] = {}

    # ---------- 公开路线 ----------

    def list_routes(
        self,
        *,
        q: str | None = None,
        theme: str | None = None,
        city: str | None = None,
        duration: str | None = None,
        budget_max: int | None = None,
        channel: str | None = None,
        sort: str = "latest",
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> tuple[list[RouteSummaryOut], str | None]:
        limit = max(1, min(limit, MAX_LIMIT))
        if city and city.strip().casefold() not in _CITY_ALIASES:
            # 没有任何该城市的数据；给空页而不是悄悄忽略筛选条件。
            return [], None
        route_filter = _RouteFilter(
            q=q.strip() if q else None,
            theme=theme.strip() if theme else None,
            duration=_parse_duration(duration),
            budget_max=budget_max,
            channel=channel,
        )
        descending = sort != "oldest"
        after = decode_cursor(cursor) if cursor else None

        matched: list[tuple[uuid.UUID, RouteVersion, datetime]] = []
        scan_after = after
        exhausted = False
        batches = 0
        batch_size = max(limit + 1, DEFAULT_LIMIT)
        # 先按发布时间分页取候选，再在 Python 里按 JSON 快照过滤：
        # 过滤会淘汰候选，所以循环到凑满 limit+1 条或候选扫完为止。
        while len(matched) <= limit and not exhausted and batches < _MAX_SCAN_BATCHES:
            rows = self.repo.latest_published_positions(
                after=scan_after,
                limit=batch_size,
                descending=descending,
            )
            batches += 1
            if len(rows) < batch_size:
                exhausted = True
            if not rows:
                break
            versions = self.repo.latest_versions([route_id for route_id, _ in rows])
            for route_id, last_at in rows:
                version = versions.get(route_id)
                if version is None:
                    continue
                if not route_filter.matches(self.repo.snapshot_of(version)):
                    continue
                matched.append((route_id, version, last_at))
                if len(matched) > limit:
                    break
            scan_after = (rows[-1][1], rows[-1][0])

        page = matched[:limit]
        if len(matched) > limit:
            last = page[-1]
            next_cursor: str | None = encode_cursor(last[2], last[0])
        elif not exhausted and scan_after is not None and page:
            # 还有候选没扫完（批次上限），游标指向本次扫到的位置，下一页继续。
            next_cursor = encode_cursor(scan_after[0], scan_after[1])
        else:
            next_cursor = None
        return self._summaries([(route_id, version) for route_id, version, _ in page]), next_cursor

    def get_route(
        self, *, route_id: uuid.UUID, viewer_id: uuid.UUID | None = None
    ) -> RouteDetailOut:
        version = self._require_published(route_id)
        summary = self._summaries([(route_id, version)])[0]
        is_favorited = False
        if viewer_id is not None:
            is_favorited = self.repo.get_favorite(user_id=viewer_id, route_id=route_id) is not None
        return RouteDetailOut(
            **summary.model_dump(),
            version_id=version.id,
            content_hash=version.content_hash,
            published_at=_as_utc(version.created_at),
            days=self._days(self.repo.snapshot_of(version)),
            is_favorited=is_favorited,
        )

    def list_versions(self, *, route_id: uuid.UUID) -> list[RouteVersionOut]:
        """版本历史：契约要求登录（没有 security: []），但未发布路线仍然 404。"""
        self._require_published(route_id)
        return [
            RouteVersionOut(
                id=version.id,
                version_no=version.version_no,
                content_hash=version.content_hash,
                created_at=_as_utc(version.created_at),
                created_by=version.created_by,
            )
            for version in self.repo.versions_of(route_id)
        ]

    # ---------- 评论 ----------

    def list_comments(
        self, *, route_id: uuid.UUID, cursor: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> CommentListOut:
        self._require_published(route_id)
        limit = max(1, min(limit, MAX_LIMIT))
        after = _decode_comment_cursor(cursor) if cursor else None
        rows = self.repo.list_comments(route_id=route_id, after=after, limit=limit + 1)
        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = _encode_comment_cursor(rows[-1])
        return CommentListOut(
            items=[self._comment_out(row) for row in rows],
            next_cursor=next_cursor,
        )

    def create_comment(self, *, route_id: uuid.UUID, body: str, user: CurrentUser) -> CommentOut:
        self._require_published(route_id)
        cleaned = body.strip()
        if not cleaned:
            raise ValidationFailed(
                "评论内容不能为空",
                details=[{"field": "body", "reason": "不能全为空白"}],
            )
        if len(cleaned) > MAX_COMMENT_LENGTH:
            raise ValidationFailed(
                "评论内容过长",
                details=[{"field": "body", "reason": f"最多 {MAX_COMMENT_LENGTH} 字"}],
            )
        comment = Comment(route_id=route_id, user_id=_actor_id(user), body=cleaned)
        self.repo.add_comment(comment)
        self.db.flush()
        self._audit(
            action="comment.create",
            object_type="comment",
            object_id=comment.id,
            user=user,
            after={"route_id": str(route_id), "body": comment.body},
        )
        self.db.commit()
        return self._comment_out(comment)

    def pin_comment(self, *, comment_id: uuid.UUID, user: CurrentUser) -> CommentOut:
        comment = self.repo.get_comment(comment_id)
        if comment is None or comment.status != CommentStatus.VISIBLE:
            raise NotFound("评论不存在", code="COMMENT_NOT_FOUND")
        version = self._require_published(comment.route_id)
        owner_id = self.repo.draft_owners([version.draft_id]).get(version.draft_id)
        if owner_id is None or owner_id != _actor_id(user):
            # 403 而不是 404：评论已经公开存在，装作没有反而让作者困惑。
            raise Forbidden("只有路线作者可以置顶评论", code="NOT_ROUTE_AUTHOR")
        if not comment.pinned:
            # 每路线最多一条置顶：先清掉旧的再置顶（部分唯一索引是最后一道兜底）。
            self.repo.unpin_others(route_id=comment.route_id, keep_id=comment.id)
            self.db.flush()
            comment.pinned = True
            self.db.flush()
            self._audit(
                action="comment.pin",
                object_type="comment",
                object_id=comment.id,
                user=user,
                after={"route_id": str(comment.route_id), "pinned": True},
            )
            self.db.commit()
        return self._comment_out(comment)

    # ---------- 收藏 ----------

    def add_favorite(self, *, route_id: uuid.UUID, user: CurrentUser) -> None:
        self._require_published(route_id)
        actor_id = _actor_id(user)
        if self.repo.get_favorite(user_id=actor_id, route_id=route_id) is not None:
            # 幂等：重复收藏不报错、不重复写审计（没有状态变化）。
            return
        try:
            favorite = Favorite(user_id=actor_id, route_id=route_id)
            self.repo.add_favorite(favorite)
            self.db.flush()
        except IntegrityError:
            # 并发下另一个请求先落了同一条收藏，唯一约束替我们做了幂等。
            self.db.rollback()
            return
        self._audit(
            action="favorite.create",
            object_type="favorite",
            object_id=favorite.id,
            user=user,
            after={"user_id": str(actor_id), "route_id": str(route_id)},
        )
        self.db.commit()

    def remove_favorite(self, *, route_id: uuid.UUID, user: CurrentUser) -> None:
        self._require_published(route_id)
        actor_id = _actor_id(user)
        favorite = self.repo.get_favorite(user_id=actor_id, route_id=route_id)
        if favorite is None:
            # 幂等：本来就没收藏，同样 204，不制造一条无意义的审计。
            return
        before = {"user_id": str(actor_id), "route_id": str(route_id)}
        self.repo.delete_favorite(favorite)
        self.db.flush()
        self._audit(
            action="favorite.delete",
            object_type="favorite",
            object_id=favorite.id,
            user=user,
            before=before,
        )
        self.db.commit()

    # ---------- 内部 ----------

    def _require_published(self, route_id: uuid.UUID) -> RouteVersion:
        version = self.repo.latest_version(route_id)
        if version is None:
            # 不存在与未发布同码同文案：不能用 403/409 把「存在但没发」透出去。
            raise NotFound("路线不存在或未发布", code="ROUTE_NOT_FOUND")
        return version

    def _summaries(self, pairs: list[tuple[uuid.UUID, RouteVersion]]) -> list[RouteSummaryOut]:
        if not pairs:
            return []
        route_ids = [route_id for route_id, _ in pairs]
        comment_counts = self.repo.comment_counts(route_ids)
        favorite_counts = self.repo.favorite_counts(route_ids)
        owners = self.repo.draft_owners([version.draft_id for _, version in pairs])
        items: list[RouteSummaryOut] = []
        for route_id, version in pairs:
            snapshot = self.repo.snapshot_of(version)
            author_id = owners.get(version.draft_id)
            items.append(
                RouteSummaryOut(
                    id=route_id,
                    title=str(snapshot.get("title") or ""),
                    summary=_optional_str(snapshot.get("summary")),
                    cover_url=None,
                    channel="user",
                    author=AuthorOut(id=author_id, username=self._username_of(author_id)),
                    theme_tags=[str(tag) for tag in snapshot.get("theme_tags") or []],
                    duration_minutes=_optional_int(snapshot.get("duration_minutes")),
                    cost=_cost_of(snapshot),
                    comment_count=comment_counts.get(route_id, 0),
                    favorite_count=favorite_counts.get(route_id, 0),
                )
            )
        return items

    def _days(self, snapshot: dict[str, Any]) -> list[RouteDayOut]:
        """把快照节点展开成行程天。

        快照没有 day_index（RouteNode 也不分天），发布格式只支持单日路线，
        所以统一返回 day_index=1；将来分天时要先改 routes 的快照结构。
        """
        catalog = CatalogService(self.db)
        items: list[RouteDayItemOut] = []
        for index, node in enumerate(snapshot.get("nodes") or [], start=1):
            if not isinstance(node, dict):
                continue
            place_id = node.get("place_id")
            try:
                place = catalog.get_place(uuid.UUID(str(place_id)))
            except (NotFound, ValueError):
                # 公开详情只展示已核验（verified）地点：草稿地点、已下架地点不出现，
                # 也不因为「节点里有个无效 place_id」整条路线 500。
                continue
            items.append(
                RouteDayItemOut(
                    position=_optional_int(node.get("position")) or index,
                    start_time=None,
                    stay_minutes=_optional_int(node.get("stay_minutes")) or 0,
                    transport=_optional_str(node.get("transport")),
                    place=place,
                    price_min=_optional_int(node.get("price_min")),
                    price_max=_optional_int(node.get("price_max")),
                    confidence=None,
                )
            )
        return [RouteDayOut(day_index=1, items=items)] if items else []

    def _comment_out(self, comment: Comment) -> CommentOut:
        return CommentOut(
            id=comment.id,
            route_id=comment.route_id,
            body=comment.body,
            pinned=bool(comment.pinned),
            author=AuthorOut(
                id=comment.user_id,
                username=self._username_of(comment.user_id),
            ),
            created_at=_as_utc(comment.created_at),
        )

    def _username_of(self, user_id: uuid.UUID | None) -> str | None:
        if user_id is None:
            return None
        if user_id not in self._usernames:
            user = IdentityService(self.db).get_user(user_id)
            self._usernames[user_id] = user.username if user is not None else None
        return self._usernames[user_id]

    def _audit(
        self,
        *,
        action: str,
        object_type: str,
        object_id: uuid.UUID,
        user: CurrentUser,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> None:
        """与业务写同一事务（audit.write 只 flush 不 commit），提交由调用方做。"""
        self.audit.write(
            action=action,
            object_type=object_type,
            object_id=str(object_id),
            actor_id=user.id,
            actor_username=user.username,
            before_hash=AuditService.digest(before) if before is not None else None,
            after_hash=AuditService.digest(after) if after is not None else None,
        )
