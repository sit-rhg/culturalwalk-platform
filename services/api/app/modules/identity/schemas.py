# 身份与会话 · DTO
"""请求/响应模型。字段与 docs/api/openapi.yaml 保持一致，改 yaml 再改这里。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class RegisterIn(BaseModel):
    """POST /auth/register 的请求体。

    minLength / maxLength 交给 pydantic，越界就由框架自然返回契约里的 422，
    不在 service 里手搓 422。
    """

    username: str = Field(min_length=3, max_length=24)
    password: str = Field(min_length=8)
    # EmailStr 就是契约里的 format: email（由 email-validator 提供），格式不对直接 422
    email: EmailStr | None = Field(default=None, max_length=254)


class LoginIn(BaseModel):
    """POST /auth/login 的请求体。密码不设下限：登录只验对错，不重判注册规则。"""

    username: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    """openapi.yaml#User。required 只有 id / username / role，其余可空。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: str
    bio: str | None = None
    created_at: datetime | None = None

    @field_validator("created_at")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        # SQLite 会丢掉 tzinfo；出参一律补齐 UTC，前端才拿到带时区的 ISO-8601
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class TokenPairOut(BaseModel):
    """openapi.yaml#TokenPair。user 必带，expires_in 可选（这里给实际值）。"""

    access_token: str
    refresh_token: str
    expires_in: int
    user: UserOut


class RouteAuthorOut(BaseModel):
    """openapi.yaml#RouteSummary.author（内联对象，两个字段都没有 required）。"""

    id: uuid.UUID | None = None
    username: str | None = None


class CostSummaryOut(BaseModel):
    """openapi.yaml#CostSummary。契约里一个字段都不是 required。"""

    min_total: int | None = None
    max_total: int | None = None
    currency: str | None = None
    unknown_count: int | None = None


class RouteSummaryOut(BaseModel):
    """openapi.yaml#RouteSummary。required 只有 id / title / channel。

    只有 summary / cover_url / duration_minutes 在契约里是 nullable，
    其余可选字段给非空默认值：宁可少给一个字段，也别回一个契约里没有的 null。
    """

    id: uuid.UUID
    title: str
    channel: str
    summary: str | None = None
    cover_url: str | None = None
    author: RouteAuthorOut | None = None
    theme_tags: list[str] = Field(default_factory=list)
    duration_minutes: int | None = None
    cost: CostSummaryOut | None = None
    comment_count: int = 0
    favorite_count: int = 0


class UserProfileOut(BaseModel):
    """openapi.yaml#UserProfile。required 只有 user 与 published_routes。

    comment_count 现在给不出真值：评论表还没落地。未知就不出现
    （路由上用了 response_model_exclude_none），不要回"0 条评论"这种假数据。
    """

    user: UserOut
    published_routes: list[RouteSummaryOut]
    comment_count: int | None = None
