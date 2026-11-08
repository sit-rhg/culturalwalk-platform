"""D11：CI 里的 Postgres 门禁。

背景：PG 专属的用例（jsonb、GIN、部分索引、时区）都靠 `pytest.skip` 在
「没有 PG」的机器上自动跳过。好处是本机能跑，坏处是 CI 里如果 PG 服务没起好、
DATABASE_URL 配错，这些用例会**安静地全跳过**，流水线照样绿——门形同虚设。

这个文件只在 `REQUIRE_POSTGRES=1`（CI 的 backend / e2e job 会设）时生效：
连不上就红，让「PG 没接上」变成一次可见的失败。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.core.config import settings


def test_postgres_is_wired_when_required() -> None:
    if os.environ.get("REQUIRE_POSTGRES") != "1":
        pytest.skip("本机没要求 PG（REQUIRE_POSTGRES != 1）")

    assert settings.database_url.startswith("postgresql"), (
        f"CI 要求 PG，但 DATABASE_URL 是 {settings.database_url!r}"
    )

    engine = create_engine(settings.database_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SHOW server_version")).scalar_one()
            assert version, "PG 连上了但读不到版本号"
    finally:
        engine.dispose()
