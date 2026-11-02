"""routes_create_route_nodes

Revision ID: 0005_routes
Revises: 0004_routes
Create Date: 2026-11-02 22:52:00.000000

建 route_nodes 表，并给 route_drafts 补 PUT /nodes 的幂等键。

编号说明：#79 的 identity 迁移还停在 0003_identity（P2 计划改成 0006_identity），
main 当前头是 0004_routes，所以本卡取 0005_routes。若 identity 先合入，
本文件改成 0006 并 rebase down_revision。

autogenerate 同样会带出 audit_logs / notifications 两张历史欠账表，
这里手工裁掉——本卡只碰 routes 自己的表结构。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_routes"
down_revision: str | None = "0004_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("route_drafts", sa.Column("nodes_key", sa.String(length=64), nullable=True))
    op.add_column("route_drafts", sa.Column("nodes_hash", sa.String(length=64), nullable=True))

    op.create_table(
        "route_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        # 节点是草稿的从属对象：删草稿必须连节点一起删，交给数据库级联兜底。
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        # 跨模块只存裸 ID：places 属于 catalog，不建物理外键（docs/02 §1）。
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("stay_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "transport",
            sa.Enum(
                "walk",
                "metro",
                "bus",
                "taxi",
                "bike",
                name="transportmode",
                native_enum=False,
                create_constraint=True,
                length=12,
            ),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("price_min", sa.Integer(), nullable=True),
        sa.Column("price_max", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["draft_id"], ["route_drafts.id"], ondelete="CASCADE"),
        # 同一草稿里位置唯一；整表替换写入 1..N，这条约束兜住绕过 service 的直插。
        sa.UniqueConstraint("draft_id", "position", name="uq_route_nodes_draft_position"),
        # 应用层校验之外再兜一层：绕过 API 的脚本也别写进「停留 0 分钟」或负价格。
        sa.CheckConstraint("position >= 1", name="ck_route_nodes_position"),
        sa.CheckConstraint(
            "stay_minutes >= 10 AND stay_minutes <= 480",
            name="ck_route_nodes_stay_minutes",
        ),
        sa.CheckConstraint("price_min IS NULL OR price_min >= 0", name="ck_route_nodes_price_min"),
        sa.CheckConstraint("price_max IS NULL OR price_max >= 0", name="ck_route_nodes_price_max"),
        sa.CheckConstraint(
            "price_min IS NULL OR price_max IS NULL OR price_max >= price_min",
            name="ck_route_nodes_price_range",
        ),
    )


def downgrade() -> None:
    op.drop_table("route_nodes")
    op.drop_column("route_drafts", "nodes_hash")
    op.drop_column("route_drafts", "nodes_key")
