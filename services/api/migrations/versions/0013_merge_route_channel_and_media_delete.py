"""merge route channel and media delete heads

两条链都在 0010 之后分叉：0011_route_channel（来源双通道）与
0011_node_media → 0012_media_delete（媒体软删除）。这里合并成一个头。

Revision ID: 0013_merge_demo
Revises: 0011_route_channel, 0012_media_delete
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0013_merge_demo"
down_revision: str | Sequence[str] | None = ("0011_route_channel", "0012_media_delete")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
