# 审核与发布 · DTO
"""请求/响应模型。字段与 docs/api/openapi.yaml 保持一致，改 yaml 再改这里。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.moderation.models import (
    PublicationState,
    ReviewDecision,
    ReviewTaskState,
    SubmissionState,
)


def _assume_utc(value: object) -> object:
    """SQLite 不存时区，读出来是 naive；输出前补成 UTC（与 routes 同一约定）。"""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class SubmitRequest(BaseModel):
    """POST /route-drafts/{draft_id}/submit 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=500)


class PublishRequest(BaseModel):
    """POST /submissions/{submission_id}/publish 的请求体。

    version_id + content_hash 一起构成「发哪一版、内容是什么」的授权，
    两者必须同时和已批准的投稿对上。
    """

    model_config = ConfigDict(extra="forbid")

    version_id: uuid.UUID
    content_hash: str = Field(min_length=1, max_length=128)


class UnpublishRequest(BaseModel):
    """POST /publications/{publication_id}/unpublish 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=500)


class DecisionRequest(BaseModel):
    """POST /review/tasks/{task_id}/decision 的请求体。

    reason 对 approve 可选；对 request_changes / reject 是业务上必须的，
    但契约没写 required，所以这里只做长度校验，缺失由 service 决定是否拦。
    """

    model_config = ConfigDict(extra="forbid")

    action: ReviewDecision
    reason: str | None = Field(default=None, min_length=10, max_length=1000)


class AuthorOut(BaseModel):
    """SubmissionDetail.author 的内嵌形状。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str


class SubmissionOut(BaseModel):
    """契约 Submission。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    route_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    content_hash: str | None = None
    state: SubmissionState
    note: str | None = None
    created_at: datetime

    @field_validator("created_at", mode="before")
    @classmethod
    def _utc(cls, value: object) -> object:
        return _assume_utc(value)


class SubmissionDetailOut(SubmissionOut):
    """契约 SubmissionDetail：在 Submission 上补版本快照与作者。"""

    snapshot: dict[str, Any] = Field(default_factory=dict)
    previous_snapshot: dict[str, Any] | None = None
    author: AuthorOut | None = None
    # 快照节点只有 place_id，审核页要显示地名；编辑中的草稿点位可能不是 VERIFIED，只查名字不查状态。
    place_names: dict[str, str] = Field(default_factory=dict)


class ReviewTaskOut(BaseModel):
    """契约 ReviewTask。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submission_id: uuid.UUID
    state: ReviewTaskState
    assignee_id: uuid.UUID | None = None
    lease_expires_at: datetime | None = None
    completed_action: ReviewDecision | None = None
    created_at: datetime

    @field_validator("lease_expires_at", "created_at", mode="before")
    @classmethod
    def _utc(cls, value: object) -> object:
        return _assume_utc(value)


class ReviewTaskListOut(BaseModel):
    items: list[ReviewTaskOut]
    next_cursor: str | None = None


class PublicationOut(BaseModel):
    """契约 Publication。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    route_id: uuid.UUID
    version_id: uuid.UUID
    content_hash: str
    state: PublicationState
    published_at: datetime | None = None
    unpublished_reason: str | None = None

    @field_validator("published_at", mode="before")
    @classmethod
    def _utc(cls, value: object) -> object:
        return _assume_utc(value)
