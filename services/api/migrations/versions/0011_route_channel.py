"""route_drafts 增加 channel：区分平台精选与用户投稿

Revision ID: 0011_route_channel
Revises: 0010_moderation_media
Create Date: 2026-11-03 11:58:00.000000

为什么加这一列：发现页的「来源」筛选一直筛不出平台精选 —— community 模块里
`RouteFilter.matches()` 明说「routes 表没有 channel 字段，所有路线都按用户投稿
处理」，于是官方路线被标成用户投稿、筛 official 得到空页。这一列把来源落在
数据上，而不是靠作者身份猜。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_route_channel"
down_revision: str | None = "0010_moderation_media"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 用 batch 模式：SQLite 不支持 ALTER 加约束，关掉 batch 会在本地跑不起来；
    # 生产是 PostgreSQL，batch 在这里等价于普通 ALTER，没有额外代价。
    with op.batch_alter_table("route_drafts", recreate="auto") as batch:
        # 默认值必须是 user：历史草稿都是用户投的，谁都没有官方身份。
        batch.add_column(
            sa.Column("channel", sa.String(length=10), nullable=False, server_default="user")
        )
        batch.create_check_constraint("ck_route_drafts_channel", "channel IN ('official', 'user')")


def downgrade() -> None:
    with op.batch_alter_table("route_drafts", recreate="auto") as batch:
        batch.drop_constraint("ck_route_drafts_channel", type_="check")
        batch.drop_column("channel")
