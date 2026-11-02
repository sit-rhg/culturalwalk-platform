"""RT-05：为路线节点增加媒体引用 media_asset_ids

Revision ID: 0011_node_media
Revises: 0010_media
Create Date: 2026-11-03 00:36:00.000000

说明：
- 节点是整表替换的，媒体 id 直接存 JSON 数组，不建关联表：没有独立查询需求，
  也不需要跨模块物理外键（media_assets 属 media 模块）。
- server_default 用 `'[]'` 兜住已有行；模型侧同样声明，避免 `alembic check` 漂移。
- 存量节点没有配图，补空数组是安全默认值。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011_node_media"
down_revision: str | None = "0010_media"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_nodes",
        sa.Column(
            "media_asset_ids",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("route_nodes", "media_asset_ids")
