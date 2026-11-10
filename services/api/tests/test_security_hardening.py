"""第一批修复的回归测试：令牌硬约束、登出即失效、响应前提交。

这三条都属于「单测全绿、线上出事」的类型：
- 没有 exp 的令牌永不过期（伪造一次就永久有效）；
- 登出后 access 令牌还能用满 900 秒；
- yield 依赖的 commit 在响应之后才跑，写库失败用户照样拿到 200。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, settings
from app.core.db import Base
from app.main import create_app
from app.modules.identity.models import User

ME = "/api/v1/users/me"
REGISTER = "/api/v1/auth/register"
LOGOUT = "/api/v1/auth/logout"
PASSWORD = "password123"


def _raw_token(**claims: object) -> str:
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_alg)


def _base_claims() -> dict[str, object]:
    return {
        "sub": "22222222-2222-2222-2222-222222222222",
        "username": "xiaoming",
        "role": "user",
        "typ": "access",
    }


def test_token_without_exp_is_rejected(client: TestClient) -> None:
    """exp 缺失 = 永不过期。这种令牌必须当无效处理，而不是放着不管。"""
    token = _raw_token(**_base_claims())

    response = client.get(ME, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_expired_token_is_rejected(client: TestClient) -> None:
    token = _raw_token(**_base_claims(), exp=int(time.time()) - 60)

    response = client.get(ME, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_logout_revokes_access_token_immediately(client: TestClient) -> None:
    """登出后同一个 access 令牌立刻 401，不用等它自然过期；重复登出仍是 204。"""
    issued = client.post(REGISTER, json={"username": "logout_case", "password": PASSWORD})
    assert issued.status_code == 201, issued.text
    headers = {
        "Authorization": f"Bearer {issued.json()['access_token']}",
        "Idempotency-Key": "logout-key-0001",
    }

    assert client.get(ME, headers=headers).status_code == 200
    assert client.post(LOGOUT, headers=headers).status_code == 204
    assert client.get(ME, headers=headers).status_code == 401
    # 退出是幂等的：拿着已撤销的令牌再退一次，仍然是 204 而不是 401
    assert client.post(LOGOUT, headers=headers).status_code == 204


def test_non_local_env_refuses_the_repository_default_secret() -> None:
    """demo/prod 用仓库默认密钥等于公开管理员签名私钥，启动就该失败。"""
    with pytest.raises(ValidationError):
        Settings(env="prod")

    with pytest.raises(ValidationError):
        Settings(env="prod", jwt_secret="still-too-short")

    secure = dict(
        env="prod",
        jwt_secret="x" * 32,
        storage_backend="s3",
        s3_access_key="AKIAEXAMPLE123456",
        s3_secret_key="y" * 32,
    )
    assert Settings(**secure).env == "prod"

    with pytest.raises(ValidationError):
        Settings(env="prod", jwt_secret="x" * 32)


def test_commit_failure_is_reported_before_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """commit 失败时客户端必须看到 5xx。

    以前 commit 在响应发出之后才执行：注册接口返回 201，用户却根本没落库。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("app.core.db.SessionLocal", factory)

    def _boom(self: Session) -> None:
        raise RuntimeError("磁盘写满")

    monkeypatch.setattr(Session, "commit", _boom)

    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.post(REGISTER, json={"username": "commit_fail", "password": PASSWORD})
    finally:
        engine.dispose()

    assert response.status_code >= 500


def test_public_endpoint_tolerates_an_expired_token(client: TestClient) -> None:
    """访客浏览器里可能躺着过期令牌：公开列表不该因此 401。"""
    token = _raw_token(**_base_claims(), exp=int(time.time()) - 60)

    response = client.get("/api/v1/routes", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_openapi_declares_bearer_auth_instead_of_header_parameters(client: TestClient) -> None:
    """Swagger UI 要能直接鉴权：用 securitySchemes + security，而不是 authorization 参数。"""
    document = client.get("/openapi.json").json()

    scheme = document["components"]["securitySchemes"]["bearerAuth"]
    assert scheme == {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}

    has_security = False
    for operations in document["paths"].values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                assert parameter.get("name") != "authorization"
            if operation.get("security") == [{"bearerAuth": []}]:
                has_security = True
    assert has_security


def test_writes_are_committed_before_the_response(client: TestClient, db_session: Session) -> None:
    """中间件在响应前提交：换个新 Session 也必须看得到刚注册的用户。"""
    response = client.post(REGISTER, json={"username": "fresh_session", "password": PASSWORD})
    assert response.status_code == 201, response.text

    with Session(db_session.bind) as fresh:
        found = fresh.scalar(select(User.id).where(User.username == "fresh_session"))
    assert found is not None
