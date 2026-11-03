# 公共分页游标 · 单测
"""游标是不透明串，但「坏游标必须 400 而不是 500」是接口契约的一部分，单独测住。"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from app.core.errors import AppError
from app.core.pagination import decode_cursor, decode_payload, encode_cursor, encode_payload

NOW = datetime(2026, 11, 3, 8, 30, 15, 123456, tzinfo=UTC)
ROW_ID = uuid.uuid4()


def test_cursor_round_trips_datetime_and_id() -> None:
    cursor = encode_cursor(NOW, ROW_ID)

    assert "=" not in cursor  # 去掉补位，URL 里不用转义
    assert decode_cursor(cursor) == (NOW, ROW_ID)


def test_payload_round_trips_extra_fields() -> None:
    cursor = encode_payload({"p": True, "c": NOW.isoformat(), "i": str(ROW_ID)})

    assert decode_payload(cursor) == {"p": True, "c": NOW.isoformat(), "i": str(ROW_ID)}


def test_default_cursor_fits_inside_payload_decoder() -> None:
    # 两套 API 共用同一种编码，换用时不会一个能解、一个不能。
    cursor = encode_cursor(NOW, ROW_ID)

    assert decode_payload(cursor) == {"c": NOW.isoformat(), "i": str(ROW_ID)}


@pytest.mark.parametrize(
    "bad",
    [
        "!!!not-base64!!!",
        base64.urlsafe_b64encode(b"[1,2,3]").decode().rstrip("="),  # JSON 不是对象
        base64.urlsafe_b64encode(b"{}").decode().rstrip("="),  # 缺 c/i 字段
    ],
)
def test_broken_cursor_is_400_not_500(bad: str) -> None:
    with pytest.raises(AppError) as exc_info:
        decode_cursor(bad)

    assert exc_info.value.code == "INVALID_CURSOR"
    assert exc_info.value.http_status == 400


def test_payload_decoder_rejects_non_object_json() -> None:
    bad = base64.urlsafe_b64encode(json.dumps("text").encode()).decode().rstrip("=")

    with pytest.raises(AppError) as exc_info:
        decode_payload(bad)

    assert exc_info.value.code == "INVALID_CURSOR"
