"""media: 资产软删标记（ME-03）

Revision ID: 0012_media_delete
Revises: 0011_node_media
Create Date: 2026-11-03

为什么软删而不是物理删：媒体被冻结的路线版本快照引用时，物理删会让历史版本
指向一条不存在的资产；保留行 + deleted_at 也让审计和幂等键排查有据可查。
对象存储里的字节由后续清理任务按 deleted_at 回收，不在请求事务里做。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_media_delete"
down_revision: str | None = "0011_node_media"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "media_assets",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_assets", "deleted_at")
