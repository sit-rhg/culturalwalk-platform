"""planner: agent_steps 的 (run_id, step_index) 唯一约束

D5 的第二半：生成任务重试时，一次 run 里每个序号只应该有一行步骤。
代码里已经在重跑前先删旧步骤，但"先删再写"在并发下依然能留下重复行
（重试的 worker 与正常收尾同时落库）；唯一约束是最后一道闸。

加约束前先清历史重复：同一 run 同一序号只留 id 最小的一行，
否则线上已有的脏数据会让 CREATE UNIQUE INDEX 直接失败。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_planner_agent_step_unique"
down_revision: str | Sequence[str] | None = "0019_comment_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_agent_steps_run_step"


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        # PG 的 DELETE ... USING 一次扫完，不用把重复行读进内存。
        op.execute(
            """
            DELETE FROM agent_steps a
            USING agent_steps b
            WHERE a.run_id = b.run_id
              AND a.step_index = b.step_index
              AND a.id > b.id
            """
        )
    else:
        op.execute(
            """
            DELETE FROM agent_steps
            WHERE id NOT IN (
                SELECT keep_id FROM (
                    SELECT MIN(id) AS keep_id
                    FROM agent_steps
                    GROUP BY run_id, step_index
                ) AS kept
            )
            """
        )
    op.create_index(INDEX_NAME, "agent_steps", ["run_id", "step_index"], unique=True)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="agent_steps")
