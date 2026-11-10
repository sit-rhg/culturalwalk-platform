"""moderation: 幂等键 → 首次响应快照（重放返回首响应而非当前实体）

Revision ID: 0018_moderation_idempotency
Revises: 0016_idempotency
Create Date: 2026-11-08

为什么单独建表：审核/发布流程的实体上有 claim_key/decision_key/publish_key 等列，
只够判断「是不是同一个请求」；重放时实体可能已经被后续流转改过（publish→unpublish、
claim 租约回收再领取），把当前实体序列化出去就成了「最新状态」，违反 docs/03 §1.2
「同 key 返回首次结果（含状态码）」。这里按 (actor_id, scope, key) 存首次成功的
响应体与状态码，重放原样返回。

scope 是 submit/withdraw/claim/release/decide/publish/unpublish 七个动作名。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018_moderation_idempotency"
down_revision: str | Sequence[str] | None = "0017_route_idempotency"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "moderation_idempotency_responses",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column(
            "response_body",
            sa.JSON().with_variant(JSONB, "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "actor_id", "scope", "key", name="uq_moderation_idempotency_actor_scope_key"
        ),
    )
    op.create_index(
        "ix_moderation_idempotency_responses_actor_id",
        "moderation_idempotency_responses",
        ["actor_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_moderation_idempotency_responses_actor_id",
        table_name="moderation_idempotency_responses",
    )
    op.drop_table("moderation_idempotency_responses")
