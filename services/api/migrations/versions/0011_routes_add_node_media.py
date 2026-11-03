"""routes: 节点引用媒体资产（RT-05）

Revision ID: 0011_node_media
Revises: 0010_moderation_media
Create Date: 2026-11-03

为什么加列而不是建关联表：节点与媒体的关系只有「草稿页渲染缩略图」这一处读，
没有按媒体反查节点的需求；跨模块也不建物理外键（docs/02 §1），
存一组 id 足够，等真出现反查需求再拆表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_node_media"
down_revision: str | None = "0010_moderation_media"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


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
