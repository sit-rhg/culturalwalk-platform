# CulturalWalk API · 认证与依赖
"""鉴权只在这里做。router 里只允许 `user = Depends(require_user)` 或
`Depends(require_role("admin", ...))` 这几种写法。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header
from jose import JWTError, jwt

from app.core.config import settings
from app.core.db import DbDep
from app.core.errors import Forbidden, Unauthorized

# 「这个 sid 还能不能用」的检查由 identity 模块注册进来。
# 放在这里而不是直接 import identity.models，是为了守住依赖方向：业务模块 -> core，
# 反过来会绕出一个 core -> identity -> core 的环。返回 True 表示会话已失效。
SessionGuard = Callable[[object, str], bool]
_session_guard: SessionGuard | None = None


def register_session_guard(guard: SessionGuard) -> None:
    global _session_guard
    _session_guard = guard


@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    role: str  # user | editor | admin
    # access 令牌里的 session id（老令牌可能没有）。契约的 /auth/logout 没有请求体，
    # 这个声明就是"该退哪一次会话"的唯一线索。
    session_id: str | None = None

    def is_admin(self) -> bool:
        return self.role in {"admin", "editor"}


def create_access_token(
    *, user_id: str, username: str, role: str, session_id: str | None = None
) -> str:
    import time

    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "exp": int(time.time()) + settings.access_token_ttl_seconds,
        "typ": "access",
    }
    if session_id is not None:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_alg],
        )
    except JWTError as exc:  # 过期、签名错、格式错统一 401
        raise Unauthorized("登录已失效，请重新登录") from exc
    # python-jose 的 require_exp 在部分版本里会被静默忽略，这里显式再挡一次：
    # 没有 exp 的令牌永不过期，等于一把万能钥匙。
    if "exp" not in payload or "sub" not in payload:
        raise Unauthorized("登录已失效，请重新登录")
    return payload


def _assert_session_active(db: object, session_id: object) -> None:
    """登出后 access 令牌里的 sid 立刻失效，不用等 900 秒过期。"""
    if session_id is None:
        return  # ID-02 之前签发的旧令牌没有 sid，只能等它自然过期
    if _session_guard is None:
        # 没注册检查器就放行会静默关掉吊销能力，宁可让这种装配错误当场暴露。
        raise Unauthorized("登录状态校验不可用，请重新登录")
    if _session_guard(db, str(session_id)):
        raise Unauthorized("登录已失效，请重新登录")


def _user_from_auth_header(
    authorization: str | None, db: object, *, check_session: bool = True
) -> CurrentUser | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("认证头格式错误")
    payload = decode_token(token)
    if payload.get("typ") != "access":
        raise Unauthorized("令牌类型错误")
    if check_session:
        _assert_session_active(db, payload.get("sid"))
    return CurrentUser(
        id=payload["sub"],
        username=payload["username"],
        role=payload["role"],
        session_id=payload.get("sid"),
    )


def optional_user(
    db: DbDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser | None:
    """公开接口用它：有 token 就带上用户（用于 is_favorited 之类）。"""
    return _user_from_auth_header(authorization, db)


def require_user(
    db: DbDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    user = _user_from_auth_header(authorization, db)
    if user is None:
        raise Unauthorized("请先登录")
    return user


def require_user_stale_ok(
    db: DbDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """登出专用：令牌签名有效即可，会话已经被撤销也放行。

    退出是幂等操作（契约 204，重复调用不许翻脸），所以这里不能因为
    「sid 已失效」就把第二次登出打成 401；其他端点一律走 require_user。
    """
    user = _user_from_auth_header(authorization, db, check_session=False)
    if user is None:
        raise Unauthorized("请先登录")
    return user


_ROLE_LABELS = {"admin": "管理员", "editor": "编辑"}


def _forbidden_message(allowed: frozenset[str]) -> str:
    if not allowed:
        return "接口未配置可访问角色，已默认拒绝"
    labels = "、".join(_ROLE_LABELS.get(role, role) for role in sorted(allowed))
    return f"需要{labels}权限"


def require_role(*allowed_roles: str) -> Callable[..., CurrentUser]:
    """角色依赖工厂：`require_role("admin", "editor")` 生成一个能直接 Depends 的依赖。

    用法（角色判断只写在这里，router 里不许再写 if）::

        AdminDep = Annotated[CurrentUser, Depends(require_role("admin", "editor"))]

    不传角色按 fail-closed 处理：谁都进不去（403）。漏写一个角色参数应该是"门关死了"
    而不是"门全开了"，而且失败方向是看得见的 403，比 import 期抛异常更难被忽略。
    """
    allowed = frozenset(allowed_roles)

    def dependency(user: Annotated[CurrentUser, Depends(require_user)]) -> CurrentUser:
        if user.role not in allowed:
            raise Forbidden(_forbidden_message(allowed))
        return user

    return dependency


# 历史别名：admin 与 editor 都能进管理端（沿用 is_admin 的既有口径，行为不变）
require_admin = require_role("admin", "editor")


CurrentUserDep = Annotated[CurrentUser, Depends(require_user)]
LogoutUserDep = Annotated[CurrentUser, Depends(require_user_stale_ok)]
OptionalUserDep = Annotated[CurrentUser | None, Depends(optional_user)]
AdminDep = Annotated[CurrentUser, Depends(require_admin)]
