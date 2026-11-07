"""publications: 一条路线只能有一条 PUBLISHED

应用层的「先查有没有已发布记录，再插入」在并发下会同时通过：两个审核员各点一次
发布，各自读到空结果，然后各插一条，同一条路线就有两条 PUBLISHED，下线时只能
下掉其中一条，另一条继续对外可见。

docs/02 §5 要求一条路线同时只有一条发布记录，这里补上部分唯一索引作为最终防线。

Revision ID: 0014_publication_uniqueness
Revises: 0013_merge_demo
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_publication_uniqueness"
down_revision: str | Sequence[str] | None = "0013_merge_demo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_publications_route_published"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "publications",
        ["route_id"],
        unique=True,
        sqlite_where=sa.text("state = 'PUBLISHED'"),
        postgresql_where=sa.text("state = 'PUBLISHED'"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="publications")
