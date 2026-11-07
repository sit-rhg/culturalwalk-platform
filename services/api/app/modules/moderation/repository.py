# 审核与发布 · 数据访问
"""只写查询和落库，不写权限判断、不写业务流程。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.modules.moderation.models import (
    Publication,
    PublicationState,
    ReviewTask,
    ReviewTaskState,
    Submission,
)


class ModerationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ===== 投稿 =====
    def add_submission(self, submission: Submission) -> Submission:
        """落一行。调用方负责 commit（和审计、版本冻结同一个事务）。"""
        self.db.add(submission)
        self.db.flush()
        return submission

    def get_submission(self, submission_id: uuid.UUID) -> Submission | None:
        return self.db.get(Submission, submission_id)

    def get_submission_by_submit_key(self, *, draft_id: uuid.UUID, key: str) -> Submission | None:
        stmt = select(Submission).where(
            Submission.draft_id == draft_id,
            Submission.submit_key == key,
        )
        return self.db.scalars(stmt).first()

    def list_submissions_by_draft(self, draft_id: uuid.UUID) -> list[Submission]:
        stmt = select(Submission).where(Submission.draft_id == draft_id)
        return list(self.db.scalars(stmt))

    def latest_submission(self, draft_id: uuid.UUID) -> Submission | None:
        """同一秒可能有多条，用 id 兜底排序，保证「最新」是确定的。"""
        stmt = (
            select(Submission)
            .where(Submission.draft_id == draft_id)
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .limit(1)
        )
        return self.db.scalars(stmt).first()

    # ===== 审核任务 =====
    def add_task(self, task: ReviewTask) -> ReviewTask:
        self.db.add(task)
        self.db.flush()
        return task

    def get_task(self, task_id: uuid.UUID) -> ReviewTask | None:
        return self.db.get(ReviewTask, task_id)

    def get_task_by_submission(self, submission_id: uuid.UUID) -> ReviewTask | None:
        stmt = select(ReviewTask).where(ReviewTask.submission_id == submission_id)
        return self.db.scalars(stmt).first()

    def page_tasks(
        self,
        *,
        state: ReviewTaskState | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[ReviewTask]:
        stmt = select(ReviewTask)
        if state is not None:
            stmt = stmt.where(ReviewTask.state == state)
        if after is not None:
            created_at, row_id = after
            stmt = stmt.where(
                or_(
                    ReviewTask.created_at < created_at,
                    and_(ReviewTask.created_at == created_at, ReviewTask.id < row_id),
                )
            )
        stmt = stmt.order_by(ReviewTask.created_at.desc(), ReviewTask.id.desc()).limit(limit)
        return list(self.db.scalars(stmt))

    def reclaim_expired(self, *, now: datetime) -> int:
        """租约过期的 CLAIMED 任务回 OPEN（惰性回收，不依赖后台 worker）。

        一条 UPDATE 完成，避免先查后改时把刚被重新领取的任务又打回 OPEN。
        """
        stmt = (
            update(ReviewTask)
            .where(
                ReviewTask.state == ReviewTaskState.CLAIMED,
                ReviewTask.lease_expires_at.is_not(None),
                ReviewTask.lease_expires_at < now,
            )
            .values(
                state=ReviewTaskState.OPEN,
                assignee_id=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        return self._execute_cas(stmt)

    def _execute_cas(self, stmt: Any) -> int:
        """CAS 一律交给数据库判定，关掉 ORM 的会话内同步。

        `synchronize_session` 默认 `"auto"`，会让 ORM 在 Python 里重算 WHERE：
        这里的比较对象是库里的 `lease_expires_at` 与传入的 `now`，而 SQLite 把
        `DateTime(timezone=True)` 读回来是 naive 的，和 aware 的 `now` 一比就抛
        `TypeError: can't compare offset-naive and offset-aware datetimes` ——
        表现是「租约过期后再领取/决定」直接 500。
        """
        result = self.db.execute(stmt.execution_options(synchronize_session=False))
        return cast(CursorResult[Any], result).rowcount or 0

    def claim_cas(
        self,
        *,
        task_id: uuid.UUID,
        assignee_id: uuid.UUID,
        lease_expires_at: datetime,
        key: str,
        request_hash: str,
        now: datetime,
    ) -> int:
        """领取的 CAS：只有 OPEN、或租约已过期的 CLAIMED 能被抢到。

        两个 reviewer 同时点领取时，只有一个 UPDATE 的 rowcount 是 1，另一个
        拿到 0 并返回 409 —— 先读后写挡不住这种竞争。
        """
        stmt = (
            update(ReviewTask)
            .where(
                ReviewTask.id == task_id,
                or_(
                    ReviewTask.state == ReviewTaskState.OPEN,
                    and_(
                        ReviewTask.state == ReviewTaskState.CLAIMED,
                        ReviewTask.lease_expires_at.is_not(None),
                        ReviewTask.lease_expires_at < now,
                    ),
                ),
            )
            .values(
                state=ReviewTaskState.CLAIMED,
                assignee_id=assignee_id,
                lease_expires_at=lease_expires_at,
                claim_key=key,
                claim_hash=request_hash,
                updated_at=now,
            )
        )
        return self._execute_cas(stmt)

    def release_cas(
        self,
        *,
        task_id: uuid.UUID,
        assignee_id: uuid.UUID,
        key: str,
        request_hash: str,
        now: datetime,
    ) -> int:
        """释放的 CAS：只有当前领取人、且仍处于 CLAIMED 才能释放。"""
        stmt = (
            update(ReviewTask)
            .where(
                ReviewTask.id == task_id,
                ReviewTask.state == ReviewTaskState.CLAIMED,
                ReviewTask.assignee_id == assignee_id,
            )
            .values(
                state=ReviewTaskState.OPEN,
                assignee_id=None,
                lease_expires_at=None,
                release_key=key,
                release_hash=request_hash,
                updated_at=now,
            )
        )
        return self._execute_cas(stmt)

    def complete_cas(
        self,
        *,
        task_id: uuid.UUID,
        assignee_id: uuid.UUID,
        action: str,
        key: str,
        request_hash: str,
        now: datetime,
    ) -> int:
        """决定的 CAS：只有领取人还能在 CLAIMED 上做决定。"""
        stmt = (
            update(ReviewTask)
            .where(
                ReviewTask.id == task_id,
                ReviewTask.state == ReviewTaskState.CLAIMED,
                ReviewTask.assignee_id == assignee_id,
            )
            .values(
                state=ReviewTaskState.COMPLETED,
                completed_action=action,
                lease_expires_at=None,
                decision_key=key,
                decision_hash=request_hash,
                updated_at=now,
            )
        )
        return self._execute_cas(stmt)

    # ===== 发布 =====
    def add_publication(self, publication: Publication) -> Publication:
        self.db.add(publication)
        self.db.flush()
        return publication

    def get_publication(self, publication_id: uuid.UUID) -> Publication | None:
        return self.db.get(Publication, publication_id)

    def get_publication_by_publish_key(
        self, *, submission_id: uuid.UUID, key: str
    ) -> Publication | None:
        stmt = select(Publication).where(
            Publication.submission_id == submission_id,
            Publication.publish_key == key,
        )
        return self.db.scalars(stmt).first()

    def get_published_by_route(self, route_id: uuid.UUID) -> Publication | None:
        """一条路线同时只允许一个 PUBLISHED；没有部分唯一索引，只能应用层挡。"""
        stmt = select(Publication).where(
            Publication.route_id == route_id,
            Publication.state == PublicationState.PUBLISHED,
        )
        return self.db.scalars(stmt).first()
