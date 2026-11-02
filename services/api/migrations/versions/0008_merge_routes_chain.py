"""merge_identity_routes_with_routes_chain

Revision ID: 0008_merge
Revises: 0005_merge, 0007_backfill
Create Date: 2026-11-02 23:50:00.000000

为什么又需要一次并头：
- 0005_merge 把 identity（0003_identity）和 routes 首表（0004_routes）并成一条线；
- 但 routes 的后续迁移（0005_routes / 0006_routes / 0007_backfill）是在**并头之前**
  从 0004_routes 往上长出来的，于是又出现第二条线。
- 结果：alembic 看到两个头（0005_merge 和 0007_backfill），`upgrade head` 失败。

这个迁移同样不改结构，只并头。以后新增迁移一律以 0008_merge 为 down_revision。
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0008_merge"
down_revision: str | Sequence[str] | None = ("0005_merge", "0007_backfill")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """合并头，无结构变更。"""


def downgrade() -> None:
    """回退合并，无结构变更。"""
