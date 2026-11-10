# 站内通知 · HTTP 入口
"""只做编解码与依赖注入。业务规则一律写进 service.py。

契约以 docs/api/openapi.yaml 为准，本文件只负责把端点挂上去。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import CurrentUser, CurrentUserDep
from app.core.errors import Unauthorized
from app.modules.notifications.schemas import NotificationListOut, NotificationOut
from app.modules.notifications.service import DEFAULT_LIMIT, MAX_LIMIT, NotificationsService

router = APIRouter(prefix="/notifications", tags=["notifications"])

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=64, description="幂等键"),
]


def _user_id_of(user: CurrentUser) -> uuid.UUID:
    try:
        return uuid.UUID(user.id)
    except ValueError as exc:  # token 里的 sub 不是 UUID，等同登录失效
        raise Unauthorized("登录已失效，请重新登录") from exc


@router.get("", summary="我的通知", response_model=NotificationListOut)
def list_notifications(
    user: CurrentUserDep,
    db: Annotated[Session, Depends(get_db)],
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> NotificationListOut:
    items, next_cursor = NotificationsService(db).list_mine(
        _user_id_of(user), cursor=cursor, limit=limit
    )
    return NotificationListOut(
        items=[NotificationOut.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


@router.post(
    "/{notification_id}/read",
    status_code=204,
    summary="标记通知已读（幂等）",
)
def mark_notification_read(
    notification_id: uuid.UUID,
    user: CurrentUserDep,
    db: Annotated[Session, Depends(get_db)],
    request: Request,
    idempotency_key: IdempotencyKey,
) -> Response:
    # 契约只声明 204：重复标记同样返回 204，客户端不需要区分"本次改了"与"本来就已读"。
    NotificationsService(db).mark_read(
        notification_id,
        user_id=_user_id_of(user),
        username=user.username,
        request_id=getattr(request.state, "request_id", None),
    )
    return Response(status_code=204)
