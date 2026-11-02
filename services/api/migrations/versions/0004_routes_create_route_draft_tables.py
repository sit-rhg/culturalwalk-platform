"""routes_create_route_draft

Revision ID: 0004_routes
Revises: 0003_catalog
Create Date: 2026-11-02 10:11:10.247472

只建 route_drafts 一张表。autogenerate 还顺手检出了 audit_logs / notifications：
这两个模块的模型早就在 main 上，但迁移一直没人补（历史欠账）。把它们塞进
RT-01 的迁移，会让这张卡的 diff 混进别的模块的表结构，也会让本卡的回滚变成
"顺手删别人的表"。所以这里手工裁掉，欠账单独补一张迁移再说。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_routes"
down_revision: str | None = "0003_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        # route_id 在发布前为空：草稿还不是一条公开路线。
        sa.Column("route_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=60), nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=True),
        sa.Column(
            "theme_tags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "EDITING",
                "SUBMITTED_LOCKED",
                "APPROVED_LOCKED",
                "ARCHIVED",
                name="draftstate",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("base_version_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("patch_key", sa.String(length=64), nullable=True),
        sa.Column("patch_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 幂等兜底：同一个作者 + 同一个 key 只能有一份草稿。
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_route_drafts_owner_idempotency"
        ),
    )
    op.create_index(op.f("ix_route_drafts_owner_id"), "route_drafts", ["owner_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_route_drafts_owner_id"), table_name="route_drafts")
    op.drop_table("route_drafts")
