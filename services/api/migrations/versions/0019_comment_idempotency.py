"""community: 评论写接口按 Idempotency-Key 去重（B12）

Revision ID: 0019_comment_idempotency
Revises: 0016_idempotency
Create Date: 2026-11-08

为什么要落两列在评论行上：评论接口一直收下 Idempotency-Key 就丢，网络重试会让
同一条评论插两行。把 key 和请求体指纹记在评论行上，重试时先按
(user_id, idempotency_key) 查回原行；同 key 不同 body 报 409，而不是悄悄换内容。
key 可空，普通评论不受影响——唯一约束里 NULL 互不相等，不会挡住正常插入。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_comment_idempotency"
down_revision: str | Sequence[str] | None = "0018_moderation_idempotency"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("comments") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("request_hash", sa.String(length=64), nullable=True))
        batch.create_unique_constraint(
            "uq_comments_user_idempotency_key", ["user_id", "idempotency_key"]
        )


def downgrade() -> None:
    with op.batch_alter_table("comments") as batch:
        batch.drop_constraint("uq_comments_user_idempotency_key", type_="unique")
        batch.drop_column("request_hash")
        batch.drop_column("idempotency_key")
