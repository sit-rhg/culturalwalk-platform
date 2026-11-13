"""routes: 草稿节点保存文化资料来源编号（#187）

Revision ID: 0021_route_node_sources
Revises: 0020_planner_agent_step_unique
Create Date: 2026-11-11

行程项已经有 source_ids，但路线节点没有承接列，转草稿时只能丢弃。
沿用媒体编号的存储方式：跨模块不建物理外键，JSON 数组只存来源编号；
存量节点默认空数组，降级时整列移除。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_route_node_sources"
down_revision: str | Sequence[str] | None = "0020_planner_agent_step_unique"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_nodes",
        sa.Column(
            "source_ids",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("route_nodes", "source_ids")
