# 身份与会话 · 业务规则
"""本模块唯一允许写业务判断的地方。router 不许写 if/else 业务逻辑。

拥有数据：User, Session
明确不做：路线内容、审核判断

职责分界：
- 密码只以 argon2id 摘要入库（argon2-cffi 的 PasswordHasher 默认就是 Argon2id）；
- refresh 令牌是独立签发的 JWT（typ="refresh"），库里只留它的 sha256；
- access 令牌沿用 app/core/deps.py#create_access_token，require_user 认它（typ="access"）。
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from jose import JWTError, jwt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import create_access_token
from app.core.errors import Conflict, NotFound, Unauthorized
from app.modules.identity.models import (
    ROLE_USER,
    STATUS_ACTIVE,
    User,
    utcnow,
)
from app.modules.identity.models import Session as LoginSession
from app.modules.identity.repository import IdentityRepository, PublishedRoute
from app.modules.identity.schemas import (
    ProfileAuthorOut,
    ProfileRouteOut,
    UserOut,
    UserProfileOut,
)

# 令牌类型：access 由 app.core.deps 校验，refresh 只在本模块校验
TYP_ACCESS = "access"
TYP_REFRESH = "refresh"

# argon2-cffi 的默认实现就是 Argon2id（并行度、内存、轮数都用库的默认值），
# 这里不手调参数：调紧了拖慢登录，调松了等于自降强度。
_hasher = PasswordHasher()


@dataclass(frozen=True)
class IssuedTokens:
    """一次签发的结果。router 只负责把它翻成 TokenPairOut。"""

    user: User
    access_token: str
    refresh_token: str
    expires_in: int


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """验密码。摘要损坏（InvalidHashError）也按"不匹配"处理，不把 500 抛给前端。"""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def hash_refresh_token(token: str) -> str:
    """refresh 令牌按 sha256 存库：库里没有可直接使用的明文令牌，泄库不等于被盗号。"""
    return hashlib.sha256(token.encode()).hexdigest()


def create_refresh_token(*, user_id: uuid.UUID, session_id: uuid.UUID) -> str:
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": TYP_REFRESH,
        "exp": int((utcnow() + timedelta(seconds=settings.refresh_token_ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_refresh_token(token: str) -> tuple[uuid.UUID, uuid.UUID]:
    """校验签名/过期/类型，返回 (user_id, session_id)。任何异常一律 401。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
        if payload.get("typ") != TYP_REFRESH:
            raise Unauthorized("令牌类型错误")
        return uuid.UUID(str(payload["sub"])), uuid.UUID(str(payload["sid"]))
    except (JWTError, KeyError, ValueError) as exc:  # 签名错/过期/缺字段/sub 不是 UUID
        raise Unauthorized("登录已失效，请重新登录") from exc


def _as_utc(value: datetime) -> datetime:
    """SQLite 读回来的时间没有 tzinfo，比较前统一按 UTC 补齐。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class IdentityService:
    """依赖注入的官方入口；外部模块只能通过它访问本模块能力。"""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = IdentityRepository(db)

    # ===== 对外能力 =====
    def register(self, *, username: str, password: str, email: str | None = None) -> IssuedTokens:
        name = _normalize_username(username)
        if self.repo.get_user_by_username(name) is not None:
            raise Conflict("这个用户名已经被注册了")
        try:
            user = self.repo.add_user(
                User(
                    username=name,
                    email=_normalize_email(email),
                    password_hash=hash_password(password),
                    role=ROLE_USER,
                    status=STATUS_ACTIVE,
                )
            )
        except IntegrityError as exc:
            # 预检与落库之间有间隙：另一个请求同时注册同名时，uq_users_username_lower 会在这里炸。
            # 先回滚把会话修好（否则这条连接上的后续查询全废），再翻成契约里的 409。
            self.db.rollback()
            raise Conflict("这个用户名已经被注册了") from exc
        return self._issue(user)

    def login(self, *, username: str, password: str) -> IssuedTokens:
        user = self.repo.get_user_by_username(_normalize_username(username))
        # 用户不存在与密码错返回同一句文案：别把"这个用户名存在"透给撞库的人
        if user is None or not verify_password(user.password_hash, password):
            raise Unauthorized("用户名或密码不正确")
        if user.status != STATUS_ACTIVE:
            raise Unauthorized("账号已被停用")
        return self._issue(user)

    def refresh(self, refresh_token: str) -> IssuedTokens:
        user_id, session_id = decode_refresh_token(refresh_token)
        session = self.repo.get_session(session_id)
        if session is None or session.user_id != user_id:
            raise Unauthorized("登录已失效，请重新登录")
        # 摘要对不上说明这枚令牌不是这次会话签出去的（或已被轮换掉）
        if not hmac.compare_digest(session.refresh_hash, hash_refresh_token(refresh_token)):
            raise Unauthorized("登录已失效，请重新登录")
        now = utcnow()
        if session.revoked_at is not None or _as_utc(session.expires_at) <= now:
            raise Unauthorized("登录已失效，请重新登录")
        user = self.repo.get_user(user_id)
        if user is None or user.status != STATUS_ACTIVE:
            raise Unauthorized("登录已失效，请重新登录")
        # 轮换：旧会话当场作废，泄漏的旧 refresh 令牌不能一直换得下去。
        # 用条件更新而不是"读-判-写"：上面那次 get_session 只是读快照，
        # 两个并发请求可能都拿到 revoked_at 为空的那一份，唯一能拦住第二个的就是这条 SQL。
        if not self.repo.revoke_if_active(session.id, now):
            raise Unauthorized("登录已失效，请重新登录")
        return self._issue(user)

    def logout(self, *, user_id: uuid.UUID, session_id: uuid.UUID | None = None) -> int:
        """退出登录：按 access 令牌里的 sid 撤销**本次**会话。

        契约的 /auth/logout 没有请求体，所以定位会话只能靠 access 令牌自带的 sid。
        会话不存在或不属于本人时返回 0 且不报错：退出是幂等的，重复调用仍 204。
        返回被撤销的会话数，给测试和审计留个可断言的返回值。

        兜底（有意为之，需要知道）：没有 sid 的 access 令牌会撤销该用户**全部**未撤销
        会话——多设备会一起下线。当前只有 ID-02 之前签发的令牌没有 sid，等令牌全面带
        sid 之后应当收紧成"只退本次"，或者干脆按 401 处理。
        """
        now = utcnow()
        if session_id is None:
            return self.repo.revoke_all_for_user(user_id, now)
        session = self.repo.get_session(session_id)
        if session is None or session.user_id != user_id:
            return 0
        if session.revoked_at is None:
            self.repo.revoke(session, now)
        return 1

    def get_user(self, user_id: uuid.UUID) -> User | None:
        return self.repo.get_user(user_id)

    def require_active_user(self, user_id: uuid.UUID) -> User:
        """已登录但库里没有这行、或状态不是 active：一律按登录失效处理。

        /users/me 的契约只有 200/401，所以账号被删、被停用都走 401，
        既不给幽灵令牌发资料，也不把"这个 id 存在过"透出去。
        """
        user = self.repo.get_user(user_id)
        if user is None or user.status != STATUS_ACTIVE:
            raise Unauthorized("登录已失效，请重新登录")
        return user

    def profile(self, user_id: uuid.UUID) -> UserProfileOut:
        """公开主页：用户资料 + 已发布路线，谁都能看。

        不存在与已停用一律 404：这个端点是公开的，不能让外部靠
        「404 还是 403」推断某个账号是否存在、是不是被封了。
        """
        user = self.repo.get_user(user_id)
        if user is None or user.status != STATUS_ACTIVE:
            raise NotFound("用户不存在")
        return UserProfileOut(
            user=UserOut.model_validate(user),
            published_routes=[
                _profile_route(row, user=user)
                for row in self.repo.published_routes(owner_id=user.id)
            ],
        )

    # ===== 内部 =====
    def _issue(self, user: User) -> IssuedTokens:
        """先定 session id 再签令牌：refresh 的 sid 与库里的行一次成形，不需要占位值。"""
        session_id = uuid.uuid4()
        refresh_token = create_refresh_token(user_id=user.id, session_id=session_id)
        self.repo.add_session(
            LoginSession(
                id=session_id,
                user_id=user.id,
                refresh_hash=hash_refresh_token(refresh_token),
                expires_at=utcnow() + timedelta(seconds=settings.refresh_token_ttl_seconds),
            )
        )
        return IssuedTokens(
            user=user,
            access_token=create_access_token(
                user_id=str(user.id),
                username=user.username,
                role=user.role,
                session_id=str(session_id),
            ),
            refresh_token=refresh_token,
            expires_in=settings.access_token_ttl_seconds,
        )


def _normalize_username(raw: str) -> str:
    """入库前统一 strip + lower：小写函数唯一索引才对得上，大小写不同算同一个人。"""
    return raw.strip().lower()


def session_is_revoked(db: Session, session_id: str) -> bool:
    """access 令牌里的 sid 是否已不能再用。

    登出、refresh 轮换（旧会话当场作废）、会话被删，三种情况都算失效；
    sid 不是 UUID 说明令牌是被改过的，也按失效处理。由 core.deps 注册调用。
    """
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        return True
    session = db.get(LoginSession, sid)
    return session is None or session.revoked_at is not None


def _normalize_email(raw: str | None) -> str | None:
    value = (raw or "").strip().lower()
    return value or None


def _profile_route(row: PublishedRoute, *, user: User) -> ProfileRouteOut:
    """把一条已发布版本翻成主页卡片。

    snapshot 是自由 JSON，历史版本可能缺字段或类型不对；逐字段判类型再取值，
    缺了就退回草稿标题 / 空值，不让一份坏快照把整个主页打成 500。
    """
    snapshot = row.snapshot
    title = snapshot.get("title")
    summary = snapshot.get("summary")
    tags = snapshot.get("theme_tags")
    duration = snapshot.get("duration_minutes")
    return ProfileRouteOut(
        id=row.route_id,
        title=title.strip() if isinstance(title, str) and title.strip() else row.draft_title,
        summary=summary if isinstance(summary, str) and summary else None,
        channel="user",
        author=ProfileAuthorOut(id=user.id, username=user.username),
        theme_tags=[tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else [],
        duration_minutes=duration
        if isinstance(duration, int) and not isinstance(duration, bool)
        else None,
    )
