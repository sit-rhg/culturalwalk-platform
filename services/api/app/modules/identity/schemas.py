# 身份与会话 · DTO
"""请求/响应模型。字段与 docs/api/openapi.yaml 保持一致，改 yaml 再改这里。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
)

# strip 必须在长度判断之前：否则 "   " 能通过 min_length=3，入库后变成空用户名，
# 而 "  ab  " 会以 "ab" 落库——两者都绕过了契约的 3..24。
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=24)]


class RegisterIn(BaseModel):
    """POST /auth/register 的请求体。

    minLength / maxLength 交给 pydantic，越界就由框架自然返回契约里的 422，
    不在 service 里手搓 422。
    """

    username: Username
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


class ProfileAuthorOut(BaseModel):
    """openapi.yaml#RouteSummary.author。主页只露作者 id 和用户名，不带邮箱等账号信息。"""

    id: uuid.UUID
    username: str


class ProfileRouteOut(BaseModel):
    """openapi.yaml#RouteSummary 的子集，用于公开主页的「已发布路线」卡片。

    cost / comment_count / favorite_count 在契约里都是可选字段，但它们是 community
    的聚合口径（评论数、收藏数由那边数）。identity 不复刻别人的统计规则，
    宁可不返回，也不返回一个数不上口径的值。

    完整的 RouteSummary（含 cost 与两个计数）只有 community 模块的 schemas.py 一份；
    这里是刻意裁剪的子集，不要再把整份搬回来。
    """

    id: uuid.UUID
    title: str
    summary: str | None = None
    cover_url: str | None = None
    channel: str = "user"
    author: ProfileAuthorOut | None = None
    theme_tags: list[str] = Field(default_factory=list)
    duration_minutes: int | None = None


class TokenPairOut(BaseModel):
    """openapi.yaml#TokenPair。user 必带，expires_in 可选（这里给实际值）。"""

    access_token: str
    refresh_token: str
    expires_in: int
    user: UserOut


class UserProfileOut(BaseModel):
    """openapi.yaml#UserProfile。required 只有 user 与 published_routes。

    comment_count 现在给不出真值：评论表还没落地。未知就不出现
    （路由上用了 response_model_exclude_none），不要回"0 条评论"这种假数据。
    """

    user: UserOut
    published_routes: list[ProfileRouteOut]
    comment_count: int | None = None
