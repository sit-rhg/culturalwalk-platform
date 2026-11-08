"""routes: 草稿增加「适合人群」多选标签（DS-02）

Revision ID: 0015_route_audience
Revises: 0014_publication_uniqueness
Create Date: 2026-11-08

为什么存 JSON 数组而不是关联表：现在只有「发现页按单个值包含筛选」这一处读，
没有按受众反查路线或统计的需求；跨模块也不建物理外键（docs/02 §1），
等真出现多维统计再拆表。

服务端默认值 '[]' 是为了让已有草稿能直接升级；模型层同样声明 server_default，
否则 alembic check 会报 modify_default 漂移（0011 的 media_asset_ids 是同一套做法）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_route_audience"
down_revision: str | Sequence[str] | None = "0014_publication_uniqueness"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_drafts",
        sa.Column(
            "audience",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("route_drafts", "audience")
