"""ID-02 验收：注册 / 登录 / 刷新令牌 / 退出登录 / 当前用户。

契约：docs/api/openapi.yaml 的 /auth/register、/auth/login、/auth/refresh、
/auth/logout 与 /users/me，以及 components/schemas 的 TokenPair、User。
数据：docs/02-数据模型与状态机.md §2.1 的 users / sessions。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import create_access_token
from app.core.errors import Conflict
from app.modules.audit.models import AuditLog
from app.modules.identity import service as identity_service
from app.modules.identity.models import STATUS_DISABLED, User, utcnow
from app.modules.identity.models import Session as LoginSession
from app.modules.identity.repository import IdentityRepository
from app.modules.identity.service import IdentityService, decode_refresh_token, hash_refresh_token

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/users/me"

PASSWORD = "password123"


def _register(client: TestClient, username: str = "Ming", password: str = PASSWORD) -> dict:
    resp = client.post(REGISTER, json={"username": username, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _logout_headers(token: str) -> dict[str, str]:
    """契约把 Idempotency-Key 声明成 logout 的必填头，测试也照契约发。"""
    return {**_bearer(token), "Idempotency-Key": "logout-key-0001"}


def _precheck_missed(self: IdentityRepository, username: str) -> User | None:
    """模拟注册竞态：预检那一刻另一个请求还没提交，所以什么都没查到。"""
    return None


def test_register_returns_token_pair(client: TestClient) -> None:
    """201 + TokenPair 的三个必填字段（access_token / refresh_token / user）。"""
    body = _register(client, username="  Ming  ")

    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["username"] == "ming", "入库前要 strip + lower，citext 语义靠它"
    assert body["user"]["role"] == "user"
    assert body["user"]["id"]
    assert body["expires_in"] > 0


def test_register_duplicate_username_conflict(client: TestClient) -> None:
    """重名报 409；大小写不同也算重名。"""
    _register(client, username="ming")
    resp = client.post(REGISTER, json={"username": "MING", "password": PASSWORD})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_register_short_password_is_validation_error(client: TestClient) -> None:
    """minLength 由 pydantic 判，返回统一信封里的 422（不是自己造的 422）。"""
    resp = client.post(REGISTER, json={"username": "ming", "password": "short"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_register_invalid_email_is_validation_error(client: TestClient) -> None:
    """契约的 format: email 由 EmailStr 判，非法格式走 422 而不是 500。"""
    resp = client.post(
        REGISTER, json={"username": "ming", "password": PASSWORD, "email": "not-an-email"}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_register_accepts_valid_email(client: TestClient) -> None:
    resp = client.post(
        REGISTER,
        json={"username": "ming", "password": PASSWORD, "email": "Ming@Example.COM"},
    )

    assert resp.status_code == 201, resp.text


def test_login_with_wrong_password_is_unauthorized(client: TestClient) -> None:
    _register(client, username="ming")
    resp = client.post(LOGIN, json={"username": "ming", "password": "wrong-password"})

    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_login_with_correct_password_returns_tokens(client: TestClient) -> None:
    _register(client, username="ming")
    resp = client.post(LOGIN, json={"username": "Ming", "password": PASSWORD})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["username"] == "ming"


def test_refresh_issues_new_pair_and_drops_the_old_token(client: TestClient) -> None:
    """换新令牌成功；旧 refresh 因为轮换过，不能再用。"""
    old = _register(client, username="ming")
    resp = client.post(REFRESH, json={"refresh_token": old["refresh_token"]})

    assert resp.status_code == 200, resp.text
    fresh = resp.json()
    # access 令牌同一秒内可能逐字节相同（HS256 对相同 payload 是确定性的），
    # 所以只断言换了 refresh 令牌，另外确认新 access 能直接过 /users/me。
    assert fresh["refresh_token"] != old["refresh_token"]
    me = client.get(ME, headers=_bearer(fresh["access_token"]))
    assert me.status_code == 200, me.text

    again = client.post(REFRESH, json={"refresh_token": old["refresh_token"]})
    assert again.status_code == 401
    assert again.json()["error"]["code"] == "UNAUTHORIZED"


def test_refresh_race_loses_at_the_conditional_revoke(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发轮换的回归用例：即使读到的是"未撤销"快照，也必须被条件更新拦下。

    TestClient 是串行的，造不出真并发；所以把 service 读到的会话换成撤销发生之前的快照，
    让读路径（revoked_at 为空、摘要匹配、未过期）全部放行——此时唯一能拦住第二次兑换的
    就是 revoke_if_active 的那条 WHERE revoked_at IS NULL。没有这个修复，这里会换出第二条会话。
    """
    tokens = _register(client, username="ming")
    old_refresh = tokens["refresh_token"]
    user_id, session_id = decode_refresh_token(old_refresh)

    first = client.post(REFRESH, json={"refresh_token": old_refresh})
    assert first.status_code == 200, first.text

    stale = LoginSession(
        id=session_id,
        user_id=user_id,
        refresh_hash=hash_refresh_token(old_refresh),
        expires_at=utcnow() + timedelta(minutes=5),
    )
    monkeypatch.setattr(IdentityRepository, "get_session", lambda self, _sid: stale)

    second = client.post(REFRESH, json={"refresh_token": old_refresh})
    assert second.status_code == 401, second.text
    assert second.json()["error"]["code"] == "UNAUTHORIZED"

    # 输掉的那一方被拦下，不等于赢家被误伤：它换到的新 refresh 必须仍然可用。
    # 先撤掉上面的快照替换，否则这里读到的还是撤销发生之前的旧行。
    monkeypatch.undo()
    winner = client.post(REFRESH, json={"refresh_token": first.json()["refresh_token"]})
    assert winner.status_code == 200, winner.text


def test_logout_needs_no_body_and_revokes_current_session(client: TestClient) -> None:
    """契约的 /auth/logout 没有请求体：只凭 access 令牌里的 sid 定位该退哪一次会话。"""
    tokens = _register(client, username="ming")
    resp = client.post(LOGOUT, headers=_logout_headers(tokens["access_token"]))
    assert resp.status_code == 204, resp.text

    again = client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 401, again.text
    assert again.json()["error"]["code"] == "UNAUTHORIZED"


def test_logout_frees_only_the_current_session(client: TestClient) -> None:
    """两台设备各登录一次：退掉一台，另一台的 refresh 仍能换令牌。"""
    phone = _register(client, username="ming")
    pad = client.post(LOGIN, json={"username": "ming", "password": PASSWORD}).json()
    assert pad["refresh_token"] != phone["refresh_token"]

    resp = client.post(LOGOUT, headers=_logout_headers(phone["access_token"]))
    assert resp.status_code == 204, resp.text

    gone = client.post(REFRESH, json={"refresh_token": phone["refresh_token"]})
    assert gone.status_code == 401, gone.text
    alive = client.post(REFRESH, json={"refresh_token": pad["refresh_token"]})
    assert alive.status_code == 200, alive.text


def test_logout_is_idempotent(client: TestClient) -> None:
    """重复退出仍是 204，不因为会话已撤销就翻脸。"""
    tokens = _register(client, username="ming")
    for _ in range(2):
        resp = client.post(LOGOUT, headers=_logout_headers(tokens["access_token"]))
        assert resp.status_code == 204, resp.text


def test_logout_without_sid_revokes_every_session(client: TestClient) -> None:
    """老令牌没有 sid 时退回"撤销全部会话"，响应语义不变。"""
    tokens = _register(client, username="ming")
    legacy = create_access_token(user_id=tokens["user"]["id"], username="ming", role="user")

    assert client.post(LOGOUT, headers=_logout_headers(legacy)).status_code == 204
    gone = client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
    assert gone.status_code == 401, gone.text


def test_logout_without_token_is_unauthorized(client: TestClient) -> None:
    assert client.post(LOGOUT, headers={"Idempotency-Key": "logout-key-0002"}).status_code == 401


def test_me_without_token_is_unauthorized(client: TestClient) -> None:
    resp = client.get(ME)

    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_me_with_user_headers_matches_contract(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    """/users/me：必填 id / username / role 齐活，bio 与 created_at 可空。"""
    resp = client.get(ME, headers=user_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {"id", "username", "role"} <= set(body)
    assert body["id"] == "22222222-2222-2222-2222-222222222222"
    assert body["username"] == "xiaoming"
    assert body["role"] == "user"
    assert body["bio"] is None


def test_me_after_register_returns_created_at(client: TestClient) -> None:
    """真注册出来的用户在库里有行，created_at 要带回来且是 ISO-8601。"""
    tokens = _register(client, username="ming")
    resp = client.get(ME, headers=_bearer(tokens["access_token"]))

    assert resp.status_code == 200, resp.text
    assert resp.json()["created_at"] is not None


def test_session_stores_hash_not_plaintext(client: TestClient, db_session: Session) -> None:
    """sessions.refresh_hash 是摘要，库里不能出现明文 refresh 令牌。"""
    body = _register(client, username="ming")
    rows = list(db_session.scalars(select(LoginSession)))

    assert len(rows) == 1
    row = rows[0]
    assert row.refresh_hash != body["refresh_token"]
    assert body["refresh_token"] not in row.refresh_hash
    assert len(row.refresh_hash) == 64, "sha256 十六进制"
    assert row.revoked_at is None
    assert row.expires_at is not None


def test_me_rejects_token_for_missing_user(client: TestClient) -> None:
    """幽灵令牌：签名有效但库里没有这个用户，不许发一份"资料"出去。"""
    token = create_access_token(user_id=str(uuid.uuid4()), username="ghost", role="user")
    resp = client.get(ME, headers=_bearer(token))

    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_me_rejects_disabled_user(client: TestClient, db_session: Session) -> None:
    """账号被停用后，还没过期的旧 access 令牌也不能再用。"""
    tokens = _register(client, username="ming")
    row = db_session.scalar(select(User).where(User.username == "ming"))
    assert row is not None
    row.status = STATUS_DISABLED
    db_session.flush()

    resp = client.get(ME, headers=_bearer(tokens["access_token"]))

    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_register_race_over_http_is_conflict(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发同名：预检没查到、落库时唯一索引炸 —— 必须 409，不能 500。"""
    db_session.add(User(username="ming", password_hash="irrelevant", role="user", status="active"))
    db_session.flush()
    monkeypatch.setattr(IdentityRepository, "get_user_by_username", _precheck_missed)

    resp = client.post(REGISTER, json={"username": "ming", "password": PASSWORD})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"


def test_service_register_race_raises_conflict(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一竞态在 service 层直接断言：抛的是 AppError 子类，不是裸 IntegrityError。"""
    db_session.add(User(username="ming", password_hash="irrelevant", role="user", status="active"))
    db_session.flush()
    monkeypatch.setattr(IdentityRepository, "get_user_by_username", _precheck_missed)

    with pytest.raises(Conflict):
        IdentityService(db_session).register(username="MING", password=PASSWORD)


def test_register_rejects_blank_username_before_trimming(client: TestClient) -> None:
    """长度必须按 strip 之后算：否则 "   " 会以空用户名入库、"  ab  " 会以 2 字入库。"""
    blank = client.post(REGISTER, json={"username": "   ", "password": PASSWORD})
    assert blank.status_code == 422, blank.text
    padded = client.post(REGISTER, json={"username": "  ab  ", "password": PASSWORD})
    assert padded.status_code == 422, padded.text


def test_duplicate_email_is_reported_as_email_conflict(client: TestClient) -> None:
    first = {"username": "alice", "password": PASSWORD, "email": "Alice@Example.com"}
    assert client.post(REGISTER, json=first).status_code == 201
    same_email = {"username": "alice2", "password": PASSWORD, "email": "alice@example.com"}
    resp = client.post(REGISTER, json=same_email)
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["message"] == "这个邮箱已经被注册了"
    assert error["details"] == [{"field": "email"}]


def test_duplicate_email_race_still_reports_email(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预检没查到、落库时撞唯一索引：也要按约束名翻成邮箱冲突，别说成用户名。"""
    first = {"username": "race", "password": PASSWORD, "email": "race@example.com"}
    assert client.post(REGISTER, json=first).status_code == 201
    monkeypatch.setattr(IdentityRepository, "get_user_by_email", lambda self, email: None)
    second = {"username": "race2", "password": PASSWORD, "email": "race@example.com"}
    resp = client.post(REGISTER, json=second)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["message"] == "这个邮箱已经被注册了"


def test_logout_audits_only_the_first_revocation(client: TestClient, db_session: Session) -> None:
    """重复退出仍是 204，但审计只该记一条「这次真的撤了」。"""
    tokens = _register(client, username="logoutaudit")
    headers = _logout_headers(tokens["access_token"])

    assert client.post(LOGOUT, headers=headers).status_code == 204
    assert client.post(LOGOUT, headers=headers).status_code == 204

    actions = db_session.scalars(
        select(AuditLog.action).where(AuditLog.action == "auth.logout")
    ).all()
    assert list(actions) == ["auth.logout"]


def test_refresh_replay_revokes_the_whole_family(client: TestClient) -> None:
    """旧 refresh 被重放 = 令牌可能泄露：撤销该用户全部会话，新令牌一起失效。"""
    tokens = _register(client, username="reuse")
    rotated = client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200, rotated.text

    replay = client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401, replay.text
    assert replay.json()["error"]["code"] == "UNAUTHORIZED"

    after = client.post(REFRESH, json={"refresh_token": rotated.json()["refresh_token"]})
    assert after.status_code == 401, "家族已撤销，攻击者手里的新令牌也不能继续用"


def test_login_unknown_user_still_runs_the_password_hash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未知用户也要做一次 argon2，否则响应耗时能区分「账号存在吗」。"""
    calls: list[str] = []
    original = identity_service.verify_password

    def spy(hashed: str, password: str) -> bool:
        calls.append(hashed)
        return original(hashed, password)

    monkeypatch.setattr(identity_service, "verify_password", spy)

    resp = client.post(LOGIN, json={"username": "nobody-here", "password": "password123"})

    assert resp.status_code == 401
    assert calls, "未知用户分支必须先跑一次 dummy 校验"
