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
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, settings
from app.core.db import Base
from app.main import create_app

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
    headers = {"Authorization": f"Bearer {issued.json()['access_token']}"}

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

    assert Settings(env="prod", jwt_secret="x" * 32).env == "prod"


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
