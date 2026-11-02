"""routes_create_route_versions

Revision ID: 0006_routes
Revises: 0005_routes
Create Date: 2026-11-02 23:10:00.000000

建 route_versions 表：草稿冻结出的不可变版本（docs/02 §2.3）。

编号说明：main 当头是 0004_routes，RT-02 取了 0005_routes，#79 的 identity
仍停在 0003_identity（P2 计划改 0006_identity）。本卡按顺序取 0006_routes；
若 identity 先合入，本文件改成 0007 并 rebase down_revision。

autogenerate 会同时带出 audit_logs / notifications 两张历史欠账表，
这里手工裁掉——本卡只碰 routes 自己的表结构。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_routes"
down_revision: str | None = "0005_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        # 发布前草稿没有 route_id；发布（RT-05）时回填。
        sa.Column("route_id", sa.Uuid(), nullable=True),
        # 版本来自哪份草稿；ON DELETE RESTRICT：冻结过的草稿不允许物理删除。
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        # 整条路线的快照。PG 上落 JSONB（可查询、可建 GIN），SQLite 单测退化成 JSON。
        sa.Column(
            "snapshot",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        # sha256(canonical_json(snapshot))，见 service._content_hash。
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        # 谁冻结的；跨模块裸 ID（users 表尚未建）。
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version_no >= 1", name="ck_route_versions_version_no"),
        sa.ForeignKeyConstraint(["draft_id"], ["route_drafts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        # 同一份草稿的版本号递增；并发冻结靠它兜住，撞了重算。
        sa.UniqueConstraint("draft_id", "version_no", name="uq_route_versions_draft_version_no"),
    )
    op.create_index("ix_route_versions_route_id", "route_versions", ["route_id"], unique=False)
    op.create_index("ix_route_versions_draft_id", "route_versions", ["draft_id"], unique=False)
    # 审批/发布按 content_hash 精确定位版本，单独建索引。
    op.create_index(
        "ix_route_versions_content_hash", "route_versions", ["content_hash"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_route_versions_content_hash", table_name="route_versions")
    op.drop_index("ix_route_versions_draft_id", table_name="route_versions")
    op.drop_index("ix_route_versions_route_id", table_name="route_versions")
    op.drop_table("route_versions")
