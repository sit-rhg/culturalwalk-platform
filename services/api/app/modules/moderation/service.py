# 审核与发布 · 业务规则
"""本模块唯一允许写业务判断的地方。router 不许写 if/else 业务逻辑。

拥有数据：Submission, ReviewTask, ReviewAction, Publication
明确不做：路线内容编辑（routes）、账号与角色（identity）

状态机见 docs/02-数据模型与状态机.md §3：
- 草稿：EDITING -> SUBMITTED_LOCKED -> APPROVED_LOCKED；打回/拒绝/撤回回 EDITING
- 投稿：PENDING_REVIEW -> APPROVED / CHANGES_REQUESTED / REJECTED / WITHDRAWN；
        同一草稿再投时旧投稿转 SUPERSEDED
- 审核任务：OPEN -> CLAIMED -> COMPLETED；release / 租约过期回 OPEN；
            投稿撤回时取消为 CANCELLED（COMPLETED 只表示「审核动作结束」）
- 发布：PUBLISHED -> UNPUBLISHED ->（重新发布）PUBLISHED
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import CurrentUser
from app.core.errors import (
    Conflict,
    Forbidden,
    NotFound,
    PreconditionFailed,
    Unauthorized,
    ValidationFailed,
)
from app.core.pagination import decode_cursor, encode_cursor
from app.modules.audit.service import AuditService
from app.modules.identity.service import IdentityService
from app.modules.moderation.models import (
    Publication,
    PublicationState,
    ReviewAction,
    ReviewDecision,
    ReviewTask,
    ReviewTaskState,
    Submission,
    SubmissionState,
)
from app.modules.moderation.repository import ModerationRepository
from app.modules.moderation.schemas import (
    DecisionRequest,
    PublishRequest,
    SubmitRequest,
    UnpublishRequest,
)
from app.modules.notifications.service import (
    REVIEW_APPROVED,
    REVIEW_REJECTED,
    REVIEW_REQUEST_CHANGES,
    NotificationsService,
)
from app.modules.routes.models import DraftState, RouteDraft, RouteVersion
from app.modules.routes.service import RoutesService

# 允许的审核角色。仓库只有 user/editor/admin 三种角色，没有独立的 reviewer：
# reviewer 的职责按 docs/00 §5 由 editor/admin 承担（admin 是 editor 的超集，
# 见 core/deps.CurrentUser.is_admin）。这里不放宽成 user。
_REVIEW_ROLES = ("editor", "admin")


def _as_utc(value: datetime) -> datetime:
    """SQLite 取回来是 naive，统一按 UTC 解释，否则租约比较会在不同机器上飘。"""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _input_hash(payload: BaseModel | None, *, exclude_unset: bool = False) -> str:
    """请求体指纹：同 key 不同 body 要报 409，不能悄悄按首次结果返回。"""
    raw = json.dumps(
        payload.model_dump(mode="json", exclude_unset=exclude_unset) if payload else {},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _user_id_of(user: CurrentUser) -> uuid.UUID:
    """token 的 sub 不是 UUID 时按登录失效处理，与 routes 模块同一口径。"""
    try:
        return uuid.UUID(user.id)
    except ValueError as exc:
        raise Unauthorized("登录已失效，请重新登录") from exc


def _snapshot_place_ids(*snapshots: dict[str, Any] | None) -> list[str]:
    """从快照里收集节点引用的点位 id，给审核页做批量地名解析。"""
    ids: list[str] = []
    seen: set[str] = set()
    for snapshot in snapshots:
        if not snapshot:
            continue
        for node in snapshot.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            place_id = node.get("place_id")
            if isinstance(place_id, str) and place_id and place_id not in seen:
                seen.add(place_id)
                ids.append(place_id)
    return ids


class ModerationService:
    """依赖注入的官方入口；外部模块只能通过它访问本模块能力。"""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ModerationRepository(db)
        self.audit = AuditService(db)
        self.routes = RoutesService(db)

    # ===== 提交审核 =====
    def submit(
        self,
        *,
        draft_id: uuid.UUID,
        author: CurrentUser,
        payload: SubmitRequest,
        idempotency_key: str,
        expected_lock_version: int | None = None,
        request_id: str | None = None,
    ) -> Submission:
        """冻结版本 + 建投稿 + 建审核任务 + 锁草稿 + 写审计，同一事务。"""
        author_id = _user_id_of(author)
        request_hash = _input_hash(payload)

        # 幂等重放先于状态检查：网络重试发生在投稿已创建之后，
        # 此时草稿已经是 SUBMITTED_LOCKED，按状态先判会把重试误报成 409。
        existing = self.repo.get_submission_by_submit_key(draft_id=draft_id, key=idempotency_key)
        if existing is not None:
            self._ensure_same_request(existing.submit_hash, request_hash)
            return existing

        draft = self._load_owned_draft(draft_id=draft_id, owner_id=author_id)
        self._check_lock_version(draft, expected_lock_version)
        if draft.state is not DraftState.EDITING:
            raise Conflict(
                "草稿当前不在可提交状态",
                code="DRAFT_NOT_EDITABLE",
                details=[{"field": "state", "reason": draft.state.value}],
            )

        # 先 CAS 锁草稿，再冻结版本：两个提交请求并发时，只有一个能把
        # EDITING 改成 SUBMITTED_LOCKED，另一个拿 409，不会建出两份投稿。
        self._lock_draft(
            draft=draft, from_state=DraftState.EDITING, to_state=DraftState.SUBMITTED_LOCKED
        )

        version = self.routes.freeze_version(draft_id=draft.id, actor_id=author_id)
        self._supersede_previous(draft_id=draft.id)

        submission = Submission(
            draft_id=draft.id,
            route_id=draft.route_id,
            version_id=version.id,
            content_hash=version.content_hash,
            submitted_by=author_id,
            state=SubmissionState.PENDING_REVIEW,
            note=payload.note,
            submit_key=idempotency_key,
            submit_hash=request_hash,
        )
        self.repo.add_submission(submission)
        self.repo.add_task(ReviewTask(submission_id=submission.id, state=ReviewTaskState.OPEN))

        self.audit.write(
            action="submission.submit",
            object_type="submission",
            object_id=str(submission.id),
            actor_id=str(author_id),
            actor_username=author.username,
            after_hash=version.content_hash,
            request_id=request_id,
        )
        self.db.flush()
        return submission

    def _supersede_previous(self, *, draft_id: uuid.UUID) -> None:
        """同一草稿的历史投稿转 SUPERSEDED；已批准的投稿不允许被再投覆盖。"""
        for previous in self.repo.list_submissions_by_draft(draft_id):
            if previous.state is SubmissionState.APPROVED:
                raise Conflict(
                    "该草稿已有通过审核的投稿，不能重复提交",
                    code="SUBMISSION_ALREADY_APPROVED",
                )
            if previous.state is not SubmissionState.SUPERSEDED:
                previous.state = SubmissionState.SUPERSEDED

    # ===== 撤回投稿 =====
    def withdraw(
        self,
        *,
        draft_id: uuid.UUID,
        author: CurrentUser,
        idempotency_key: str,
        expected_lock_version: int | None = None,
        request_id: str | None = None,
    ) -> RouteDraft:
        """作者撤回到 EDITING。已被领取的投稿只能等 reviewer release / 租约过期。"""
        author_id = _user_id_of(author)
        request_hash = _input_hash(None)

        draft = self._load_owned_draft(draft_id=draft_id, owner_id=author_id)
        submission = self.repo.latest_submission(draft.id)
        if submission is None:
            raise Conflict("该草稿没有可撤回的投稿", code="SUBMISSION_NOT_FOUND")
        if submission.withdraw_key == idempotency_key:
            self._ensure_same_request(submission.withdraw_hash, request_hash)
            return draft

        self._check_lock_version(draft, expected_lock_version)
        if draft.state is not DraftState.SUBMITTED_LOCKED:
            raise Conflict(
                "草稿当前不在审核中",
                code="DRAFT_NOT_SUBMITTED",
                details=[{"field": "state", "reason": draft.state.value}],
            )
        if submission.state is not SubmissionState.PENDING_REVIEW:
            raise Conflict("当前投稿已结束，不能撤回", code="SUBMISSION_NOT_PENDING")

        task = self.repo.get_task_by_submission(submission.id)
        if task is not None and task.state is ReviewTaskState.CLAIMED:
            if self._lease_active(task):
                raise Conflict(
                    "投稿已被审核员领取，作者不能撤回；请联系审核员释放或等待租约过期",
                    code="REVIEW_TASK_CLAIMED",
                )
            # 租约已过期：先按规则回收成 OPEN，撤回才能继续。
            self.repo.reclaim_expired(now=_now())
            self.db.refresh(task)

        self._lock_draft(
            draft=draft, from_state=DraftState.SUBMITTED_LOCKED, to_state=DraftState.EDITING
        )
        submission.state = SubmissionState.WITHDRAWN
        submission.withdraw_key = idempotency_key
        submission.withdraw_hash = request_hash
        if task is not None and task.state is ReviewTaskState.OPEN:
            # 取消不是「审核完成」：COMPLETED 只保留给真实的审核动作。
            task.state = ReviewTaskState.CANCELLED
            task.cancel_reason = "作者撤回投稿"
            task.updated_at = _now()
            self.db.add(
                ReviewAction(
                    task_id=task.id,
                    actor_id=author_id,
                    action="cancel",
                    reason=task.cancel_reason,
                )
            )

        self.audit.write(
            action="submission.withdraw",
            object_type="submission",
            object_id=str(submission.id),
            actor_id=str(author_id),
            actor_username=author.username,
            before_hash=submission.content_hash,
            reason="作者撤回投稿",
            request_id=request_id,
        )
        self.db.flush()
        return draft

    # ===== 审核队列 =====
    def list_tasks(
        self,
        *,
        state: ReviewTaskState | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[ReviewTask], str | None]:
        # 惰性回收：没有后台 worker，读队列/领取/决定前先把过期租约清掉。
        # 这不是用户触发的写操作，不写审计。
        self.repo.reclaim_expired(now=_now())
        after = decode_cursor(cursor) if cursor else None
        rows = self.repo.page_tasks(state=state, after=after, limit=limit + 1)
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
        return page, next_cursor

    # ===== 领取任务 =====
    def claim(
        self,
        *,
        task_id: uuid.UUID,
        reviewer: CurrentUser,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> ReviewTask:
        reviewer_id = _user_id_of(reviewer)
        request_hash = _input_hash(None)
        task = self._load_task(task_id)
        if task.claim_key == idempotency_key:
            self._ensure_same_request(task.claim_hash, request_hash)
            return task

        if task.state is ReviewTaskState.COMPLETED:
            raise Conflict("任务已完成", code="TASK_NOT_CLAIMABLE")
        if task.state is ReviewTaskState.CANCELLED:
            raise Conflict("任务已取消", code="TASK_NOT_CLAIMABLE")

        now = _now()
        lease_expires_at = now + timedelta(minutes=settings.review_lease_minutes)
        # CAS：OPEN 或「租约已过期的 CLAIMED」才能被抢到。SQLite 没有
        # SELECT ... FOR UPDATE，单条条件 UPDATE 是这里唯一可靠的互斥。
        rowcount = self.repo.claim_cas(
            task_id=task.id,
            assignee_id=reviewer_id,
            lease_expires_at=lease_expires_at,
            key=idempotency_key,
            request_hash=request_hash,
            now=now,
        )
        if rowcount == 0:
            raise Conflict("任务已被其他审核员领取", code="TASK_ALREADY_CLAIMED")
        self.db.refresh(task)
        self.audit.write(
            action="review.claim",
            object_type="review_task",
            object_id=str(task.id),
            actor_id=str(reviewer_id),
            actor_username=reviewer.username,
            request_id=request_id,
        )
        self.db.flush()
        return task

    # ===== 释放任务 =====
    def release(
        self,
        *,
        task_id: uuid.UUID,
        reviewer: CurrentUser,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> ReviewTask:
        reviewer_id = _user_id_of(reviewer)
        request_hash = _input_hash(None)
        task = self._load_task(task_id)
        if task.release_key == idempotency_key:
            self._ensure_same_request(task.release_hash, request_hash)
            return task

        if task.state is not ReviewTaskState.CLAIMED:
            if task.state is ReviewTaskState.OPEN:
                raise Conflict("任务未被领取，无需释放", code="TASK_NOT_CLAIMED")
            raise Conflict("任务已结束，不能释放", code="TASK_NOT_RELEASABLE")
        if task.assignee_id != reviewer_id:
            raise Forbidden("只能释放自己领取的任务", code="NOT_TASK_ASSIGNEE")
        if not self._lease_active(task):
            # 租约已经过期，本来就会被回收成 OPEN；这里按规则显式回收。
            self.repo.reclaim_expired(now=_now())
            self.db.refresh(task)
            raise Conflict("任务租约已过期，已自动回到队列", code="TASK_LEASE_EXPIRED")

        rowcount = self.repo.release_cas(
            task_id=task.id,
            assignee_id=reviewer_id,
            key=idempotency_key,
            request_hash=request_hash,
            now=_now(),
        )
        if rowcount == 0:
            raise Conflict("任务状态已变化，请刷新后重试", code="TASK_STATE_CHANGED")
        self.db.refresh(task)
        self.audit.write(
            action="review.release",
            object_type="review_task",
            object_id=str(task.id),
            actor_id=str(reviewer_id),
            actor_username=reviewer.username,
            request_id=request_id,
        )
        self.db.flush()
        return task

    # ===== 审核决定 =====
    def decide(
        self,
        *,
        task_id: uuid.UUID,
        reviewer: CurrentUser,
        payload: DecisionRequest,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> ReviewTask:
        reviewer_id = _user_id_of(reviewer)
        request_hash = _input_hash(payload)
        task = self._load_task(task_id)
        if task.decision_key == idempotency_key:
            self._ensure_same_request(task.decision_hash, request_hash)
            return task

        if task.state is not ReviewTaskState.CLAIMED:
            if task.state is ReviewTaskState.OPEN:
                raise Conflict("任务尚未被领取", code="TASK_NOT_CLAIMED")
            raise Conflict("任务已结束，不能重复决定", code="TASK_ALREADY_FINISHED")
        if task.assignee_id != reviewer_id:
            raise Forbidden("只能处理自己领取的任务", code="NOT_TASK_ASSIGNEE")
        if not self._lease_active(task):
            self.repo.reclaim_expired(now=_now())
            raise Conflict("任务租约已过期，请重新领取", code="TASK_LEASE_EXPIRED")

        submission = self.repo.get_submission(task.submission_id)
        if submission is None:
            raise NotFound("投稿不存在", code="SUBMISSION_NOT_FOUND")
        if submission.submitted_by == reviewer_id:
            # 利益冲突：自己的稿子不能自己批。
            raise Forbidden("不能审核自己的投稿", code="SELF_REVIEW_FORBIDDEN")
        if submission.state is not SubmissionState.PENDING_REVIEW:
            raise Conflict("投稿当前不在待审核状态", code="SUBMISSION_NOT_PENDING")

        # 退回/拒绝必须写理由（验收条件：退回通知带理由）。校验放在 CAS 之前：
        # 否则任务先被标成 COMPLETED，再因为缺理由报 422，用户重试只会看到「任务已结束」。
        reason = (payload.reason or "").strip()
        if payload.action is not ReviewDecision.APPROVE and not reason:
            raise ValidationFailed(
                "退回或拒绝必须写明理由",
                code="REVIEW_REASON_REQUIRED",
                details=[{"field": "reason", "message": "退回必须带理由"}],
            )

        now = _now()
        rowcount = self.repo.complete_cas(
            task_id=task.id,
            assignee_id=reviewer_id,
            action=payload.action.value,
            key=idempotency_key,
            request_hash=request_hash,
            now=now,
        )
        if rowcount == 0:
            raise Conflict("任务状态已变化，请刷新后重试", code="TASK_STATE_CHANGED")

        decision = payload.action
        if decision is ReviewDecision.APPROVE:
            submission.state = SubmissionState.APPROVED
            next_draft_state = DraftState.APPROVED_LOCKED
        elif decision is ReviewDecision.REQUEST_CHANGES:
            submission.state = SubmissionState.CHANGES_REQUESTED
            next_draft_state = DraftState.EDITING
        else:
            submission.state = SubmissionState.REJECTED
            next_draft_state = DraftState.EDITING

        draft = self.db.get(RouteDraft, submission.draft_id)
        if draft is None:
            raise Conflict("投稿关联的草稿不存在", code="DRAFT_NOT_FOUND")
        if draft.state is not DraftState.SUBMITTED_LOCKED:
            raise Conflict(
                "草稿当前不在审核中，投稿状态可能已过期",
                code="DRAFT_NOT_SUBMITTED",
                details=[{"field": "state", "reason": draft.state.value}],
            )
        # 审核通过/打回都要放开草稿锁：通过后锁成 APPROVED_LOCKED 等发布，
        # 打回则回到 EDITING 让作者改。
        self._lock_draft(
            draft=draft,
            from_state=DraftState.SUBMITTED_LOCKED,
            to_state=next_draft_state,
        )
        self.db.add(
            ReviewAction(
                task_id=task.id,
                actor_id=reviewer_id,
                action=decision.value,
                reason=reason or None,
            )
        )
        self.audit.write(
            action=f"review.{decision.value}",
            object_type="submission",
            object_id=str(submission.id),
            actor_id=str(reviewer_id),
            actor_username=reviewer.username,
            before_hash=submission.content_hash,
            after_hash=submission.content_hash,
            reason=reason or None,
            request_id=request_id,
        )
        self._notify_author(submission=submission, draft=draft, decision=decision, reason=reason)
        self.db.refresh(task)
        self.db.flush()
        return task

    def _notify_author(
        self,
        *,
        submission: Submission,
        draft: RouteDraft,
        decision: ReviewDecision,
        reason: str,
    ) -> None:
        """审核结果写站内通知。退回/拒绝必须带理由，notify() 会再校验一次。

        与裁决同一个事务：这里失败（例如退回没带理由）会让整次裁决回滚，
        所以理由校验必须跑在 complete_cas 之前，不能只靠 notify() 兜底。
        """
        route_id = submission.route_id or draft.route_id
        payload: dict[str, Any] = {
            "submission_id": str(submission.id),
            "title": draft.title,
            "decision": decision.value,
        }
        if route_id is not None:
            payload["route_id"] = str(route_id)
        if decision is ReviewDecision.APPROVE:
            notif_type = REVIEW_APPROVED
        elif decision is ReviewDecision.REQUEST_CHANGES:
            notif_type = REVIEW_REQUEST_CHANGES
            payload["reason"] = reason
        else:
            notif_type = REVIEW_REJECTED
            payload["reason"] = reason
        NotificationsService(self.db).notify(submission.submitted_by, notif_type, payload)

    # ===== 投稿详情 =====
    def submission_detail(
        self, *, submission_id: uuid.UUID, viewer: CurrentUser
    ) -> tuple[Submission, dict[str, Any], dict[str, Any] | None, str | None, dict[str, str]]:
        submission = self.repo.get_submission(submission_id)
        if submission is None:
            raise NotFound("投稿不存在", code="SUBMISSION_NOT_FOUND")
        viewer_id = _user_id_of(viewer)
        if submission.submitted_by != viewer_id and not viewer.is_admin():
            raise Forbidden("只能查看自己的投稿", code="SUBMISSION_FORBIDDEN")

        version = self.db.get(RouteVersion, submission.version_id)
        snapshot = dict(version.snapshot) if version is not None else {}
        previous_snapshot: dict[str, Any] | None = None
        if version is not None:
            stmt = (
                select(RouteVersion)
                .where(
                    RouteVersion.draft_id == version.draft_id,
                    RouteVersion.version_no < version.version_no,
                )
                .order_by(RouteVersion.version_no.desc())
                .limit(1)
            )
            previous = self.db.scalars(stmt).first()
            if previous is not None:
                previous_snapshot = dict(previous.snapshot)

        author = IdentityService(self.db).get_user(submission.submitted_by)
        place_names = self.routes.place_names(_snapshot_place_ids(snapshot, previous_snapshot))
        return (
            submission,
            snapshot,
            previous_snapshot,
            author.username if author else None,
            place_names,
        )

    # ===== 发布 =====
    def publish(
        self,
        *,
        submission_id: uuid.UUID,
        actor: CurrentUser,
        payload: PublishRequest,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> Publication:
        actor_id = _user_id_of(actor)
        request_hash = _input_hash(payload)
        submission = self.repo.get_submission(submission_id)
        if submission is None:
            raise NotFound("投稿不存在", code="SUBMISSION_NOT_FOUND")

        existing = self.repo.get_publication_by_publish_key(
            submission_id=submission.id, key=idempotency_key
        )
        if existing is not None:
            self._ensure_same_request(existing.publish_hash, request_hash)
            return existing

        if submission.state is not SubmissionState.APPROVED:
            raise Conflict("投稿尚未通过审核，不能发布", code="SUBMISSION_NOT_APPROVED")
        if payload.version_id != submission.version_id:
            raise Conflict(
                "请求的版本与已批准的版本不一致",
                code="VERSION_MISMATCH",
                details=[{"field": "version_id", "reason": "不是该投稿被批准的版本"}],
            )
        version = self.db.get(RouteVersion, submission.version_id)
        if version is None:
            raise Conflict("被批准的版本不存在", code="VERSION_NOT_FOUND")
        if (
            payload.content_hash != version.content_hash
            or payload.content_hash != submission.content_hash
        ):
            raise Conflict(
                "内容 hash 与已批准的版本不一致，拒绝发布",
                code="CONTENT_HASH_MISMATCH",
            )

        draft = self.db.get(RouteDraft, submission.draft_id)
        route_id = self._resolve_route_id(draft=draft, version=version)
        already = self.repo.get_published_by_route(route_id)
        if already is not None:
            raise Conflict(
                "该路线已有处于发布状态的版本，请先下线",
                code="ROUTE_ALREADY_PUBLISHED",
            )

        now = _now()
        publication = Publication(
            submission_id=submission.id,
            route_id=route_id,
            version_id=version.id,
            content_hash=version.content_hash,
            state=PublicationState.PUBLISHED,
            published_by=actor_id,
            published_at=now,
            publish_key=idempotency_key,
            publish_hash=request_hash,
        )
        try:
            with self.db.begin_nested():
                self.repo.add_publication(publication)
        except IntegrityError:
            # 并发发布兜底：两个请求都通过了上面的预检，最后只能由部分唯一索引裁决。
            # 用 SAVEPOINT 包住插入，失败只回滚这一小段，外层事务仍可继续读写。
            concurrent = self.repo.get_publication_by_publish_key(
                submission_id=submission.id, key=idempotency_key
            )
            if concurrent is not None and concurrent.publish_hash == request_hash:
                return concurrent
            raise Conflict(
                "该路线已有处于发布状态的版本，请先下线",
                code="ROUTE_ALREADY_PUBLISHED",
            ) from None
        # 发布后草稿才有稳定的 route：回填到投稿与版本，前端详情页才能串起来。
        submission.route_id = route_id
        self.audit.write(
            action="publication.publish",
            object_type="publication",
            object_id=str(publication.id),
            actor_id=str(actor_id),
            actor_username=actor.username,
            after_hash=version.content_hash,
            request_id=request_id,
        )
        self.db.flush()
        return publication

    def _resolve_route_id(self, *, draft: RouteDraft | None, version: RouteVersion) -> uuid.UUID:
        """草稿第一次发布时分配路线 ID，并回填到该草稿的版本行。

        用 UUID 而不是自增：route_id 是公开读取的标识，不能暴露内部计数。

        这里必须是**确定性**的（由 draft_id 派生）。否则两个请求同时首次发布同一
        草稿时会各生成一个随机 route_id，部分唯一索引按 route_id 判重就拦不住，
        两条 PUBLISHED 仍然会落库。
        """
        route_id = (draft.route_id if draft is not None else None) or version.route_id
        if route_id is not None:
            return route_id
        route_id = uuid.uuid5(uuid.NAMESPACE_URL, f"culturalwalk:route-draft:{version.draft_id}")
        if draft is not None:
            draft.route_id = route_id
        self.db.execute(
            update(RouteVersion)
            .where(RouteVersion.draft_id == version.draft_id, RouteVersion.route_id.is_(None))
            .values(route_id=route_id)
        )
        return route_id

    # ===== 下线 =====
    def unpublish(
        self,
        *,
        publication_id: uuid.UUID,
        actor: CurrentUser,
        payload: UnpublishRequest,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> Publication:
        actor_id = _user_id_of(actor)
        request_hash = _input_hash(payload)
        publication = self.repo.get_publication(publication_id)
        if publication is None:
            raise NotFound("发布记录不存在", code="PUBLICATION_NOT_FOUND")
        if publication.unpublish_key == idempotency_key:
            self._ensure_same_request(publication.unpublish_hash, request_hash)
            return publication
        if publication.state is not PublicationState.PUBLISHED:
            raise Conflict(
                "该发布记录当前不在发布状态",
                code="PUBLICATION_NOT_PUBLISHED",
                details=[{"field": "state", "reason": publication.state.value}],
            )

        publication.state = PublicationState.UNPUBLISHED
        publication.unpublished_reason = payload.reason
        publication.unpublish_key = idempotency_key
        publication.unpublish_hash = request_hash
        self.audit.write(
            action="publication.unpublish",
            object_type="publication",
            object_id=str(publication.id),
            actor_id=str(actor_id),
            actor_username=actor.username,
            before_hash=publication.content_hash,
            after_hash=publication.content_hash,
            reason=payload.reason,
            request_id=request_id,
        )
        self.db.flush()
        return publication

    # ===== 内部 =====
    def _load_task(self, task_id: uuid.UUID) -> ReviewTask:
        task = self.repo.get_task(task_id)
        if task is None:
            raise NotFound("审核任务不存在", code="TASK_NOT_FOUND")
        return task

    def _load_owned_draft(self, *, draft_id: uuid.UUID, owner_id: uuid.UUID) -> RouteDraft:
        draft = self.db.get(RouteDraft, draft_id)
        if draft is None:
            raise NotFound("草稿不存在", code="DRAFT_NOT_FOUND")
        if draft.owner_id != owner_id:
            # 与 routes 一致：不是 404，明确告诉调用方无权操作。
            raise Forbidden("只能操作自己的草稿", code="DRAFT_FORBIDDEN")
        return draft

    def _check_lock_version(self, draft: RouteDraft, expected: int | None) -> None:
        if expected is not None and draft.lock_version != expected:
            raise PreconditionFailed("草稿已被修改，请刷新后重试", code="STALE_DRAFT_VERSION")

    def _lock_draft(
        self,
        *,
        draft: RouteDraft,
        from_state: DraftState,
        to_state: DraftState,
    ) -> None:
        """条件 UPDATE 推草稿状态：并发下只有一个请求能改成功。"""
        result = self.db.execute(
            update(RouteDraft)
            .where(RouteDraft.id == draft.id, RouteDraft.state == from_state)
            .values(state=to_state, lock_version=RouteDraft.lock_version + 1, updated_at=_now())
        )
        if (result.rowcount or 0) == 0:
            raise Conflict("草稿状态已变化，请刷新后重试", code="DRAFT_STATE_CHANGED")
        self.db.refresh(draft)

    def _lease_active(self, task: ReviewTask) -> bool:
        return task.lease_expires_at is not None and _as_utc(task.lease_expires_at) > _now()

    def _ensure_same_request(self, stored_hash: str | None, request_hash: str) -> None:
        if stored_hash != request_hash:
            raise Conflict(
                "同一个 Idempotency-Key 被用于不同的请求体",
                code="IDEMPOTENCY_KEY_REUSED",
            )
