"""存储适配器的签名与边界。

签名 URL 是"浏览器的令牌"，一旦可以改 key / 改类型 / 改大小上限，就等于把桶
开放给任意写入（docs/…开发范式.md §11.1.1）。这里逐项钉死。
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app.adapters.storage import LocalStorageAdapter, StorageRejected


@pytest.fixture()
def adapter(tmp_path) -> LocalStorageAdapter:
    return LocalStorageAdapter(
        root=tmp_path, secret="unit-secret", public_base_url="http://media.test", ttl_seconds=60
    )


def _params(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def test_presigned_url_binds_one_key(adapter: LocalStorageAdapter) -> None:
    ticket = adapter.presign_put("quarantine/abc", mime="image/png", max_bytes=100)
    assert "/quarantine/abc" in ticket.url
    assert ticket.fields == {"Content-Type": "image/png"}
    params = _params(ticket.url)
    assert params["mime"] == "image/png"
    assert params["max_bytes"] == "100"


def test_signature_verifies_only_for_the_same_terms(adapter: LocalStorageAdapter) -> None:
    params = _params(adapter.presign_put("quarantine/abc", mime="image/png", max_bytes=100).url)
    expires = int(params["expires"])
    assert adapter.verify_put(
        key="quarantine/abc",
        expires=expires,
        mime="image/png",
        max_bytes=100,
        signature=params["signature"],
    )
    # 换 key、换类型、放大上限，签名都必须失效
    assert not adapter.verify_put(
        key="quarantine/other",
        expires=expires,
        mime="image/png",
        max_bytes=100,
        signature=params["signature"],
    )
    assert not adapter.verify_put(
        key="quarantine/abc",
        expires=expires,
        mime="image/jpeg",
        max_bytes=100,
        signature=params["signature"],
    )
    assert not adapter.verify_put(
        key="quarantine/abc",
        expires=expires,
        mime="image/png",
        max_bytes=10**9,
        signature=params["signature"],
    )


def test_expired_signature_is_rejected(adapter: LocalStorageAdapter) -> None:
    params = _params(adapter.presign_put("quarantine/abc", mime="image/png", max_bytes=100).url)
    assert not adapter.verify_put(
        key="quarantine/abc",
        expires=int(params["expires"]) - 120,
        mime="image/png",
        max_bytes=100,
        signature=params["signature"],
    )


def test_put_head_get_delete_round_trip(adapter: LocalStorageAdapter) -> None:
    meta = adapter.put("quarantine/abc", b"hello")
    assert meta.size == 5
    assert adapter.head("quarantine/abc").checksum == meta.checksum
    assert adapter.get("quarantine/abc") == b"hello"
    adapter.delete("quarantine/abc")
    assert adapter.head("quarantine/abc") is None
    assert adapter.get("quarantine/abc") is None


def test_path_traversal_is_rejected(adapter: LocalStorageAdapter) -> None:
    with pytest.raises(StorageRejected):
        adapter.put("../escape.txt", b"x")
