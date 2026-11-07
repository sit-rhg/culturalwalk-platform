# 公共费用汇总
"""预算口径只有这一份：缺任一端的价格算「未知」，不按 0 计入总额。

把缺失当 0 会让「预算 300」的路线看起来比实际便宜，所以单独计数，
前端可以提示「还有 N 站未填价格」。routes（草稿详情）与 community（公开路线）
都调这里，避免同一套规则写两遍、以后改口径漏改一处。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class CostTotals:
    min_total: int
    max_total: int
    unknown_count: int


def summarize_cost(prices: Iterable[tuple[int | None, int | None]]) -> CostTotals:
    """按各节点（最低价, 最高价）汇总；任一端缺失的节点计入 unknown_count。

    上限不能低于下限：A 站只有最低价、B 站只有最高价时，两项相加会得到
    「¥1000 - ¥50」这种倒挂区间。未知节点让上限不可信，但不该让它比下限还小，
    所以最后把上限抬到不低于下限，剩下的不确定性交给 unknown_count 表达。
    """
    min_total = 0
    max_total = 0
    unknown_count = 0
    for price_min, price_max in prices:
        if price_min is None or price_max is None:
            unknown_count += 1
        if price_min is not None:
            min_total += price_min
        if price_max is not None:
            max_total += price_max
    return CostTotals(
        min_total=min_total,
        max_total=max(max_total, min_total),
        unknown_count=unknown_count,
    )
