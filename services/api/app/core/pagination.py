# 公共分页 · 游标编解码
"""游标对前端不透明，内部就是「上一页最后一条」的 (created_at, id) 位置。

所有列表接口都按 `(created_at, id)` 倒序分页；把编解码放在 core，
模块的 service 只决定业务筛选条件，不各写一份 base64。
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

from app.core.errors import AppError

DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def encode_cursor(created_at: datetime, row_id: uuid.UUID | str) -> str:
    raw = json.dumps(
        {"c": created_at.isoformat(), "i": str(row_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """解析游标；坏游标抛 INVALID_CURSOR（400），不落到 500。"""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(data["c"]), uuid.UUID(str(data["i"]))
    except (KeyError, TypeError, ValueError) as exc:  # base64 错、JSON 错、时间/ID 格式错
        raise AppError("分页游标无效", code="INVALID_CURSOR") from exc
