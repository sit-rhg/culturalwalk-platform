# 公共分页 · 游标编解码
"""游标对前端不透明，内部就是「上一页最后一条」的位置。

默认位置是 `(created_at, id)`；个别列表（例如评论按「置顶 + 时间」排序）需要
额外字段，可以把任意简单字典交给 `encode_payload` / `decode_payload`。
base64 补位、JSON 序列化、坏游标错误码都只留这一份，模块里不再各抄一遍。
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.core.errors import AppError

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def encode_payload(payload: Mapping[str, Any]) -> str:
    """把可 JSON 化的字典编成不透明游标（去掉 base64 补位）。"""
    raw = json.dumps(dict(payload), separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_payload(cursor: str) -> dict[str, Any]:
    """解析游标字典；坏游标抛 INVALID_CURSOR（400），不落到 500。"""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (TypeError, ValueError) as exc:  # base64 错、JSON 错
        raise AppError("分页游标无效", code="INVALID_CURSOR") from exc
    if not isinstance(data, dict):
        raise AppError("分页游标无效", code="INVALID_CURSOR")
    return data


def encode_cursor(created_at: datetime, row_id: uuid.UUID | str) -> str:
    return encode_payload({"c": created_at.isoformat(), "i": str(row_id)})


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """解析默认两字段游标；字段缺失或格式不对同样抛 INVALID_CURSOR。"""
    data = decode_payload(cursor)
    try:
        return datetime.fromisoformat(data["c"]), uuid.UUID(str(data["i"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError("分页游标无效", code="INVALID_CURSOR") from exc
