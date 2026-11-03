# 公共费用汇总 · 单测
"""预算口径写错会直接影响用户判断，把三条边界（空、全缺、混合）钉住。"""

from __future__ import annotations

from app.core.costing import summarize_cost


def test_empty_route_has_zero_cost() -> None:
    totals = summarize_cost([])

    assert (totals.min_total, totals.max_total, totals.unknown_count) == (0, 0, 0)


def test_nodes_without_prices_count_as_unknown_not_zero() -> None:
    totals = summarize_cost([(None, None), (None, None), (None, None)])

    assert totals.min_total == 0
    assert totals.max_total == 0
    # 缺任一端都算「未填价格」，不能悄悄当 0
    assert totals.unknown_count == 3


def test_partial_price_still_contributes_its_known_end() -> None:
    # 只填一侧时另一侧不猜；已知的那一端照常计入，但整站仍算「未填全」。
    totals = summarize_cost([(None, 500), (800, None)])

    assert totals.min_total == 800
    assert totals.max_total == 500
    assert totals.unknown_count == 2


def test_mixed_prices_only_sum_known_values() -> None:
    totals = summarize_cost([(1000, 2000), (None, None), (500, None)])

    assert totals.min_total == 1500
    assert totals.max_total == 2000
    assert totals.unknown_count == 2


def test_single_node_sums_both_ends() -> None:
    totals = summarize_cost([(6000, 6000)])

    assert (totals.min_total, totals.max_total, totals.unknown_count) == (6000, 6000, 0)
