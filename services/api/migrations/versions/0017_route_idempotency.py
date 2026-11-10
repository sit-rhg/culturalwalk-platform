"""routes: PATCH/PUT 幂等历史表（按 key 存首次响应）

Revision ID: 0017_route_idempotency
Revises: 0016_idempotency
Create Date: 2026-11-08

为什么单独建表：route_drafts 的单列只能记住最近一次的 key。用户先 K1 再 K2，
之后重试 K1 时单列已经不认识 K1，只能回 412 或把内容改回旧值。这张表按
(actor_id, draft_id, operation, key) 存每次写的响应快照，各 key 互不覆盖；
docs/03 §1.2 要求 24h 内重放首次结果（含状态码），过期后 key 可重新使用。

旧的四列 patch_key/patch_hash/nodes_key/nodes_hash 由这张表取代，一并删除。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_route_idempotency"
down_revision: str | Sequence[str] | None = "0016_idempotency"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_idempotency_records",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("draft_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column(
            "response_body",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["route_drafts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "actor_id",
            "draft_id",
            "operation",
            "key",
            name="uq_route_idempotency_actor_draft_op_key",
        ),
    )
    op.create_index(
        "ix_route_idempotency_records_actor_id",
        "route_idempotency_records",
        ["actor_id"],
    )
    op.create_index(
        "ix_route_idempotency_records_draft_id",
        "route_idempotency_records",
        ["draft_id"],
    )
    # 单槽列被历史表取代；batch 模式让 SQLite 也能重放这条迁移。
    with op.batch_alter_table("route_drafts") as batch:
        batch.drop_column("patch_key")
        batch.drop_column("patch_hash")
        batch.drop_column("nodes_key")
        batch.drop_column("nodes_hash")


def downgrade() -> None:
    with op.batch_alter_table("route_drafts") as batch:
        batch.add_column(sa.Column("patch_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("patch_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("nodes_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("nodes_hash", sa.String(length=64), nullable=True))
    op.drop_index("ix_route_idempotency_records_draft_id", table_name="route_idempotency_records")
    op.drop_index("ix_route_idempotency_records_actor_id", table_name="route_idempotency_records")
    op.drop_table("route_idempotency_records")
