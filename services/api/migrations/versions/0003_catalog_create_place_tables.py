"""CA-01：建地点与文化资料 3 张表（places / place_sources / place_facts）

Revision ID: 0003_catalog
Revises: 0002_planner
Create Date: 2026-11-01 21:11:12.492926

说明：
- 表结构以 docs/02-数据模型与状态机.md §2.2 为准，字段取值与 docs/api/openapi.yaml 的
  Place / PlaceDetail / PlaceFact / Source 对齐。
- 由 `alembic revision --autogenerate` 生成后**手工裁剪**：autogenerate 顺带发现了
  `audit_logs` 与 `notifications` 两张表在主分支上一直没有迁移（MD-05 / NT-01 当时只合了模型），
  那属于别人的卡，不能塞进这张卡的 PR。这里只保留 catalog 的三张表，
  缺失的两张另开一份迁移补（已在 PR 描述里记为待办）。
- 枚举一律 VARCHAR + CHECK（native_enum=False），理由见 models.py，与 0001 保持同一套写法。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_catalog"
down_revision: str | None = "0002_planner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "place_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        # 口述来源没有链接，url 允许为空。
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("publisher", sa.String(length=128), nullable=True),
        sa.Column("accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("license", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "places",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("alias", sa.String(length=128), nullable=True),
        # 经纬度用 Numeric(9,6)：坐标是标识符不是测量值，float 会有末位误差。
        sa.Column("lat", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("lon", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("crs", sa.String(length=16), nullable=False),
        sa.Column("district", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        # PG 上落 JSONB（能用 @> 查询、能建 GIN），SQLite 单测侧退化成 JSON。
        sa.Column(
            "culture_tags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("open_hours", sa.Text(), nullable=True),
        sa.Column("open_hours_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "verified",
                name="placestatus",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # 主题标签用 GIN：CA-03 的 `?tag=` 会翻译成 `culture_tags @> '["历史"]'`，普通 B-tree 用不上。
    op.create_index(
        "ix_places_culture_tags",
        "places",
        ["culture_tags"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(op.f("ix_places_district"), "places", ["district"], unique=False)

    op.create_table(
        "place_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column(
            "confidence",
            sa.Enum(
                "verified",
                "likely",
                "unverified",
                name="factconfidence",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # 产品红线：verified 的说法必须有出处。service 会先返回 422，
        # 这条 CHECK 兜住脚本/种子数据直接写库的情况。
        sa.CheckConstraint(
            "NOT (confidence = 'verified' AND source_id IS NULL)",
            name="ck_place_facts_verified_needs_source",
        ),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="CASCADE"),
        # 来源被删时把事实的来源置空，而不是连事实一起删——说法本身还有价值，
        # 只是从此降级为「待核实」（前端按 source_id IS NULL 处理）。
        sa.ForeignKeyConstraint(["source_id"], ["place_sources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_place_facts_place_id"), "place_facts", ["place_id"], unique=False)
    op.create_index(op.f("ix_place_facts_source_id"), "place_facts", ["source_id"], unique=False)


def downgrade() -> None:
    # 先删引用方再删被引用方，否则会撞外键。
    op.drop_index(op.f("ix_place_facts_source_id"), table_name="place_facts")
    op.drop_index(op.f("ix_place_facts_place_id"), table_name="place_facts")
    op.drop_table("place_facts")
    op.drop_index(op.f("ix_places_district"), table_name="places")
    op.drop_index("ix_places_culture_tags", table_name="places")
    op.drop_table("places")
    op.drop_table("place_sources")
