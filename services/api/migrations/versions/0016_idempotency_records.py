"""catalog: 幂等键记录表（同 key 重试返回首次结果）

Revision ID: 0016_idempotency
Revises: 0015_route_audience
Create Date: 2026-11-08

为什么单独建表：docs/03 §1.2 要求同一个 Idempotency-Key 在 24h 内重试时返回首次
结果（含状态码），而 catalog 两个写端点此前只校验了头格式、没落任何记录，
重试会真的创建两份数据。表按 (actor_id, scope, key) 唯一：key 是客户端生成的，
若不按发起人隔离，别人拿同一个 key 就能读到你的响应。

object_id 允许为空是「先预占、创建完再回填」用的：预占行在事务提交前对别人不可见，
所以并发同 key 的败者只会撞唯一约束或读到已经完整的记录。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_idempotency"
down_revision: str | Sequence[str] | None = "0015_route_audience"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(length=48), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("object_type", sa.String(length=32), nullable=False),
        sa.Column("object_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("actor_id", "scope", "key", name="uq_idempotency_actor_scope_key"),
    )
    op.create_index("ix_idempotency_records_actor_id", "idempotency_records", ["actor_id"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_records_actor_id", table_name="idempotency_records")
    op.drop_table("idempotency_records")
