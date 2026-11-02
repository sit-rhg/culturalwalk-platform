"""merge_identity_and_routes

Revision ID: 0005_merge
Revises: 0003_identity, 0004_routes
Create Date: 2026-11-02 23:40:00.000000

为什么需要这个空迁移：
identity（0003_identity）和 routes（0004_routes，接在 0003_catalog 后面）
各自从 0002_planner 分出两条线，同时存在时 Alembic 会报 "Multiple head revisions"，
`alembic upgrade head` 直接失败（CI 的「Migration 可重放」这一步就是这么红的）。

这个迁移不改任何表结构，只做一件事：把两条线并成一个头。
以后再有人新增迁移，down_revision 写 "0005_merge" 即可，不要各自另起一条线。
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0005_merge"
down_revision: str | Sequence[str] | None = ("0003_identity", "0004_routes")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """合并头，无结构变更。"""


def downgrade() -> None:
    """回退合并，无结构变更。"""
