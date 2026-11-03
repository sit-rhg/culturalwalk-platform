"""上传签名适配器的绑定测试：一次签名只能写一个 key、一个长度、一种类型。"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from app.modules.media.adapters.storage import PlaceholderS3Signer, UploadTicket


def _signer() -> PlaceholderS3Signer:
    return PlaceholderS3Signer(
        endpoint="http://localhost:9000",
        bucket="culturalwalk",
        access_key="test-access",
        secret_key="test-secret",
    )


def _policy(ticket: UploadTicket) -> dict:
    return json.loads(base64.b64decode(ticket.fields["policy"]).decode("utf-8"))


def test_policy_binds_exact_object_key() -> None:
    key = "quarantine/1111/2222/yuelu.jpg"
    ticket = _signer().presign_upload(
        object_key=key,
        mime="image/jpeg",
        size=2048,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )

    conditions = _policy(ticket)["conditions"]
    assert {"key": key} in conditions
    # 曾经的 ["starts-with", "$key", "quarantine/"] 允许任意隔离区 key，已收紧。
    assert not any(
        isinstance(item, list) and item[:2] == ["starts-with", "$key"] for item in conditions
    )
    assert ticket.fields["key"] == key


def test_two_object_keys_get_different_policies_and_signatures() -> None:
    signer = _signer()
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    first = signer.presign_upload(
        object_key="quarantine/1111/aaaa/one.jpg",
        mime="image/jpeg",
        size=1024,
        expires_at=expires_at,
    )
    second = signer.presign_upload(
        object_key="quarantine/1111/bbbb/two.jpg",
        mime="image/jpeg",
        size=1024,
        expires_at=expires_at,
    )

    assert first.fields["policy"] != second.fields["policy"]
    assert first.fields["x-amz-signature"] != second.fields["x-amz-signature"]


def test_policy_binds_exact_size_and_content_type() -> None:
    ticket = _signer().presign_upload(
        object_key="quarantine/1111/cccc/three.png",
        mime="image/png",
        size=4096,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )

    conditions = _policy(ticket)["conditions"]
    assert ["content-length-range", 4096, 4096] in conditions
    assert {"Content-Type": "image/png"} in conditions


def test_expiry_is_echoed_in_ticket_and_policy() -> None:
    expires_at = datetime(2026, 11, 3, 12, 30, tzinfo=UTC)
    ticket = _signer().presign_upload(
        object_key="quarantine/1111/dddd/four.webp",
        mime="image/webp",
        size=512,
        expires_at=expires_at,
    )

    assert ticket.expires_at == expires_at
    assert _policy(ticket)["expiration"] == "2026-11-03T12:30:00Z"
