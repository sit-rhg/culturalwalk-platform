"""对象存储签名与后端选择：SigV4 派生链、工厂、启动期配置校验。"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.modules.media.adapters.objects import LocalObjectStore, S3ObjectStore, get_object_store
from app.modules.media.adapters.storage import signing_key_for


def _expected_signing_key(secret: str, *, day: str, region: str) -> bytes:
    """按 AWS 文档的链独立写一遍：kDate → kRegion → kService → kSigning。"""
    key = hmac.new(("AWS4" + secret).encode(), day.encode(), hashlib.sha256).digest()
    key = hmac.new(key, region.encode(), hashlib.sha256).digest()
    key = hmac.new(key, b"s3", hashlib.sha256).digest()
    return hmac.new(key, b"aws4_request", hashlib.sha256).digest()


def test_signing_key_follows_the_full_aws4_chain() -> None:
    secret = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    actual = signing_key_for(secret, day="20150830", region="us-east-1")
    assert actual == _expected_signing_key(secret, day="20150830", region="us-east-1")
    assert actual != hmac.new(secret.encode(), b"20150830", hashlib.sha256).digest()


def test_object_store_follows_the_configured_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "storage_backend", "local")
    assert isinstance(get_object_store(), LocalObjectStore)

    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_endpoint", "http://minio:9000")
    monkeypatch.setattr(settings, "s3_bucket", "culturalwalk-clean")
    monkeypatch.setattr(settings, "s3_access_key", "AKIAEXAMPLE123456")
    monkeypatch.setattr(settings, "s3_secret_key", "y" * 32)
    store = get_object_store()
    assert isinstance(store, S3ObjectStore)


def test_shared_env_requires_real_object_storage_config() -> None:
    """demo/prod 还挂本地盘或留着空密钥，等于把未加固的原型暴露出去。"""
    base = dict(env="prod", jwt_secret="x" * 32)

    with pytest.raises(ValidationError):
        Settings(**base)
    with pytest.raises(ValidationError):
        Settings(**base, storage_backend="s3")
    with pytest.raises(ValidationError):
        Settings(**base, storage_backend="s3", s3_access_key="short", s3_secret_key="y" * 32)

    ok = Settings(
        **base,
        storage_backend="s3",
        s3_access_key="AKIAEXAMPLE123456",
        s3_secret_key="y" * 32,
    )
    assert ok.storage_backend == "s3"
