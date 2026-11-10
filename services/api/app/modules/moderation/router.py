# 审核与发布 · HTTP 入口
"""只做编解码与依赖注入。业务规则一律写进 service.py。

契约以 docs/api/openapi.yaml 为准，本文件只负责把端点挂上去。
本模块的端点分布在 /review、/submissions、/publications、/route-drafts 四个
前缀下，而 app/main.py 只 include 一个 router，所以这里在文件末尾把子 router
聚合起来（main.py 不可改，聚合是唯一能不发明新挂载点的做法）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, status

from app.core.db import DbDep
from app.core.deps import CurrentUserDep, EditorDep
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT
from app.modules.moderation.models import ReviewTaskState
from app.modules.moderation.schemas import (
    AuthorOut,
    DecisionRequest,
    PublicationOut,
    PublishRequest,
    ReviewTaskListOut,
    ReviewTaskOut,
    SubmissionDetailOut,
    SubmissionOut,
    SubmitRequest,
    UnpublishRequest,
)
from app.modules.moderation.service import ModerationService
from app.modules.routes.schemas import RouteDraftOut

# 仓库没有独立的 reviewer 角色（只有 user/editor/admin）。审核与发布都交给
# editor/admin，直接复用 core 的 EditorDep（admin 是 editor 的超集）：
# docs/00 §2 编辑「可审核、发布精选」，docs/03 §2.4 图例 ✍️ 也是这个口径。
ReviewerDep = EditorDep
PublisherDep = EditorDep

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=64,
        description="同一请求重试时带上同一个 key，服务端返回首次结果",
    ),
]

# 契约的 8 个端点都没有声明 If-Match（只有 PATCH 草稿和整表替换节点有），
# 所以这里做成可选头：客户端传了就按草稿 lock_version 做 CAS，冲突返回 412；
# 不传也不会破坏契约，状态机本身仍有条件 UPDATE 兜底。
IfMatch = Annotated[
    int | None,
    Header(alias="If-Match", description="草稿当前 lock_version，用于并发冲突检测"),
]

review_router = APIRouter(prefix="/review", tags=["review"])
submissions_router = APIRouter(prefix="/submissions", tags=["review"])
publications_router = APIRouter(prefix="/publications", tags=["review"])
drafts_router = APIRouter(prefix="/route-drafts", tags=["drafts"])

# 对外只暴露这一个聚合 router（main.py include 的就是它）。
router = APIRouter()


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


# ===== 草稿：提交 / 撤回 =====
@drafts_router.post(
    "/{draft_id}/submit",
    summary="提交审核",
    status_code=status.HTTP_201_CREATED,
    response_model=SubmissionOut,
)
def submit_draft(
    draft_id: uuid.UUID,
    payload: SubmitRequest,
    request: Request,
    user: CurrentUserDep,
    db: DbDep,
    idempotency_key: IdempotencyKey,
    if_match: IfMatch = None,
) -> SubmissionOut:
    submission = ModerationService(db).submit(
        draft_id=draft_id,
        author=user,
        payload=payload,
        idempotency_key=idempotency_key,
        expected_lock_version=if_match,
        request_id=_request_id(request),
    )
    return SubmissionOut.model_validate(submission)


@drafts_router.post(
    "/{draft_id}/withdraw",
    summary="撤回投稿",
    response_model=RouteDraftOut,
)
def withdraw_submission(
    draft_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    db: DbDep,
    idempotency_key: IdempotencyKey,
    if_match: IfMatch = None,
) -> RouteDraftOut:
    draft = ModerationService(db).withdraw(
        draft_id=draft_id,
        author=user,
        idempotency_key=idempotency_key,
        expected_lock_version=if_match,
        request_id=_request_id(request),
    )
    return RouteDraftOut.model_validate(draft)


# ===== 审核队列 =====
@review_router.get("/tasks", summary="审核队列", response_model=ReviewTaskListOut)
def list_review_tasks(
    reviewer: ReviewerDep,
    db: DbDep,
    state: Annotated[ReviewTaskState | None, Query(description="按任务状态过滤")] = None,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> ReviewTaskListOut:
    items, next_cursor = ModerationService(db).list_tasks(state=state, cursor=cursor, limit=limit)
    return ReviewTaskListOut(
        items=[ReviewTaskOut.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


@review_router.post("/tasks/{task_id}/claim", summary="领取任务", response_model=ReviewTaskOut)
def claim_review_task(
    task_id: uuid.UUID,
    request: Request,
    reviewer: ReviewerDep,
    db: DbDep,
    idempotency_key: IdempotencyKey,
) -> ReviewTaskOut:
    task = ModerationService(db).claim(
        task_id=task_id,
        reviewer=reviewer,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
    )
    return ReviewTaskOut.model_validate(task)


@review_router.post("/tasks/{task_id}/release", summary="释放任务", response_model=ReviewTaskOut)
def release_review_task(
    task_id: uuid.UUID,
    request: Request,
    reviewer: ReviewerDep,
    db: DbDep,
    idempotency_key: IdempotencyKey,
) -> ReviewTaskOut:
    task = ModerationService(db).release(
        task_id=task_id,
        reviewer=reviewer,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
    )
    return ReviewTaskOut.model_validate(task)


@review_router.post(
    "/tasks/{task_id}/decision",
    summary="审核决定",
    response_model=ReviewTaskOut,
)
def decide_review_task(
    task_id: uuid.UUID,
    payload: DecisionRequest,
    request: Request,
    reviewer: ReviewerDep,
    db: DbDep,
    idempotency_key: IdempotencyKey,
) -> ReviewTaskOut:
    task = ModerationService(db).decide(
        task_id=task_id,
        reviewer=reviewer,
        payload=payload,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
    )
    return ReviewTaskOut.model_validate(task)


# ===== 投稿详情 / 发布 =====
@submissions_router.get(
    "/{submission_id}",
    summary="投稿详情（含版本快照，用于 diff）",
    response_model=SubmissionDetailOut,
)
def get_submission(
    submission_id: uuid.UUID,
    viewer: CurrentUserDep,
    db: DbDep,
) -> SubmissionDetailOut:
    submission, snapshot, previous_snapshot, author_username, place_names = ModerationService(
        db
    ).submission_detail(submission_id=submission_id, viewer=viewer)
    detail = SubmissionDetailOut.model_validate(submission)
    detail.snapshot = snapshot
    detail.previous_snapshot = previous_snapshot
    detail.place_names = place_names
    if author_username is not None:
        detail.author = AuthorOut(id=submission.submitted_by, username=author_username)
    return detail


@submissions_router.post(
    "/{submission_id}/publish",
    summary="发布",
    status_code=status.HTTP_201_CREATED,
    response_model=PublicationOut,
)
def publish_submission(
    submission_id: uuid.UUID,
    payload: PublishRequest,
    request: Request,
    publisher: PublisherDep,
    db: DbDep,
    idempotency_key: IdempotencyKey,
) -> PublicationOut:
    publication = ModerationService(db).publish(
        submission_id=submission_id,
        actor=publisher,
        payload=payload,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
    )
    return PublicationOut.model_validate(publication)


# ===== 下线 =====
@publications_router.post(
    "/{publication_id}/unpublish",
    summary="下线",
    response_model=PublicationOut,
)
def unpublish_publication(
    publication_id: uuid.UUID,
    payload: UnpublishRequest,
    request: Request,
    publisher: PublisherDep,
    db: DbDep,
    idempotency_key: IdempotencyKey,
) -> PublicationOut:
    publication = ModerationService(db).unpublish(
        publication_id=publication_id,
        actor=publisher,
        payload=payload,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
    )
    return PublicationOut.model_validate(publication)


router.include_router(review_router)
router.include_router(submissions_router)
router.include_router(publications_router)
router.include_router(drafts_router)
