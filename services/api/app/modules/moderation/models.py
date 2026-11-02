# 审核与发布 · ORM 模型
"""表结构以 docs/02-数据模型与状态机.md §2.4 为准。

迁移只由 P3 一人生成：alembic revision --autogenerate -m "<动词>_<对象>"
（本模块的四张表在仓库里尚未有迁移，P3 需要补一个 create_moderation_* 迁移；
本文件只负责让 ORM/测试先跑起来。）

跨模块只存裸 ID（submitted_by / assignee_id / draft_id / version_id），
不建物理外键：users 属于 identity、route_drafts 属于 routes，模块边界见
docs/02 §1。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _utcnow() -> datetime:
    """时间戳由应用生成：SQLite 的 CURRENT_TIMESTAMP 只精确到秒，游标会错位。"""
    return datetime.now(UTC)


class SubmissionState(StrEnum):
    """投稿状态机，见 docs/02 §3.2。

    RESUBMITTED 是 docs/必读1 与 ADR-0003 里为「重投」保留的词汇；openapi 的
    Submission.state 枚举没有它，所以本批接口的响应仍只会出现契约里的状态：
    重投产生的新投稿是 PENDING_REVIEW，旧投稿转 SUPERSEDED。
    """

    PENDING_REVIEW = "PENDING_REVIEW"
    RESUBMITTED = "RESUBMITTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    WITHDRAWN = "WITHDRAWN"
    SUPERSEDED = "SUPERSEDED"


class ReviewTaskState(StrEnum):
    """审核任务状态机，见 docs/02 §3.3。COMPLETED 只表示审核动作结束。"""

    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ReviewDecision(StrEnum):
    """decision 请求体的 action 枚举（与 openapi 完全一致）。"""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"


class PublicationState(StrEnum):
    """发布状态机，见 docs/02 §3.4。"""

    PUBLISHED = "PUBLISHED"
    UNPUBLISHED = "UNPUBLISHED"
    ARCHIVED = "ARCHIVED"


def _enum_values(py_enum: type[StrEnum]) -> list[str]:
    """落库的是 value（与契约 enum 完全一致），不是 Python 成员名。"""
    return [member.value for member in py_enum]


def _stored_enum(py_enum: type[StrEnum], *, length: int) -> Enum:
    """枚举一律 VARCHAR + CHECK，不用 PG 原生 ENUM：加状态不用写迁移，SQLite 也能测。"""
    return Enum(
        py_enum,
        native_enum=False,
        length=length,
        create_constraint=True,
        validate_strings=True,
        values_callable=_enum_values,
    )


class Submission(Base):
    """一次投稿：把某个不可变 route_version 送进审核。

    同一草稿再次投稿时，旧投稿转 SUPERSEDED、新投稿重新进入 PENDING_REVIEW
    （docs/02 §3.2：SUPERSEDED 只由「同一草稿产生新投稿」触发）。
    """

    __tablename__ = "submissions"
    __table_args__ = (
        # docs/02 §5：一个版本只能投一次（重投必须冻结新版本）。
        UniqueConstraint("version_id", name="uq_submissions_version_id"),
        # 提交接口的幂等键：同一草稿 + 同一 key 只产生一份投稿。
        UniqueConstraint("draft_id", "submit_key", name="uq_submissions_draft_submit_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 投稿属于哪份草稿。docs/02 没列这一列，但撤回要按 draft 找当前投稿，
    # 只靠 version_id 反查 route_versions 会把本模块焊到 routes 的表结构上。
    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    # 发布前草稿没有 route；发布时才回填（见 publications）。
    route_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    state: Mapped[SubmissionState] = mapped_column(
        _stored_enum(SubmissionState, length=20),
        nullable=False,
        default=SubmissionState.PENDING_REVIEW,
    )
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Idempotency-Key：submit 与 withdraw 是两类写操作，各存一列，
    # 否则后一次撤回会覆盖提交时的 key，提交重试就查不到原投稿。
    submit_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submit_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    withdraw_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    withdraw_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ReviewTask(Base):
    """一个投稿对应一个审核任务；领取带租约，超时由本模块惰性回收回 OPEN。"""

    __tablename__ = "review_tasks"
    __table_args__ = (
        # 同一投稿同一时间只能有一个任务（docs/02 §5 的部分唯一索引在
        # 本流程里等价于 submission_id 唯一：每个投稿只建一次任务）。
        UniqueConstraint("submission_id", name="uq_review_tasks_submission_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    state: Mapped[ReviewTaskState] = mapped_column(
        _stored_enum(ReviewTaskState, length=16), nullable=False, default=ReviewTaskState.OPEN
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_action: Mapped[ReviewDecision | None] = mapped_column(
        _stored_enum(ReviewDecision, length=20), nullable=True
    )
    # 取消原因（作者撤回时写）。COMPLETED 只表示审核动作结束，取消语义单独记。
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 三个写操作各一套幂等键，理由同 Submission。
    claim_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    release_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    release_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ReviewAction(Base):
    """审核动作留痕：谁、什么时候、做了什么决定、理由是什么。"""

    __tablename__ = "review_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # approve / request_changes / reject / cancel
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Publication(Base):
    """一条路线的当前发布记录；发布绑定精确的 version_id + content_hash。"""

    __tablename__ = "publications"
    __table_args__ = (
        # 同一投稿用同一个 key 重试只产生一条发布记录。
        UniqueConstraint(
            "submission_id", "publish_key", name="uq_publications_submission_publish_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 发布来自哪份投稿。docs/02 没列，但审计/重放需要一条可追溯的边。
    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    route_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[PublicationState] = mapped_column(
        _stored_enum(PublicationState, length=16),
        nullable=False,
        default=PublicationState.PUBLISHED,
    )
    published_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unpublished_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    publish_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publish_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unpublish_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unpublish_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
