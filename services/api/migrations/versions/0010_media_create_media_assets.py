"""ME-01：建媒体资产表 media_assets

Revision ID: 0010_media
Revises: 0009_planner
Create Date: 2026-11-03 00:20:00.000000

说明：
- 资产从 `POST /media/intent` 生成时就在 quarantine 区，扫描通过才挪到 clean 区；
  `object_key` 唯一，保证一次上传只对应一个对象。
- `(owner_id, idempotency_key)` 唯一：同一用户重复申请同一个 key 时，服务端要么
  返回首次结果、要么 409，不会悄悄建出第二条资产。
- 两个枚举列与 models.py 完全一致（Enum + native_enum=False + create_constraint），
  这样 PostgreSQL 上是 VARCHAR + CHECK，SQLite 上也能建表，`alembic check` 不漂移。
- autogenerate 顺带把 `agent_steps.step_index` 的 server_default 漂移也带了出来
  （0009 写了 DEFAULT '0'，模型没声明）；那属于 planner 模块，不在本卡范围内，
  已从本迁移里手工剔除，改由 PL 系列单独处理。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_media"
down_revision: str | None = "0009_planner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column(
            "zone",
            sa.Enum(
                "quarantine",
                "clean",
                name="mediazone",
                native_enum=False,
                create_constraint=True,
                length=12,
            ),
            nullable=False,
        ),
        sa.Column(
            "scan_state",
            sa.Enum(
                "pending",
                "scanning",
                "clean",
                "rejected",
                name="scanstate",
                native_enum=False,
                create_constraint=True,
                length=12,
            ),
            nullable=False,
        ),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size >= 0", name="ck_media_assets_size"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_media_assets_owner_idempotency"
        ),
    )
    op.create_index(op.f("ix_media_assets_owner_id"), "media_assets", ["owner_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_media_assets_owner_id"), table_name="media_assets")
    op.drop_table("media_assets")
