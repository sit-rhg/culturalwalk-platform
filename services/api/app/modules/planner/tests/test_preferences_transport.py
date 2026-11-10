"""D10：交通方式入参用真实形状（数组）测，别再用单字符串糊弄。

请求体里 `transport` 是 `list[TransportMode]`，落库时取第一个合法值。
之前单测一直塞 `"transport": "walk"`（字符串），等于只测了「历史数据兜底」
那条分支；真用户传 `["walk"]` 时如果代码直接 `str(list)`，会落成 `"['walk']"`
（17 个字符，超出 `itinerary_items.transport` 的 String(16)）——SQLite 不校验
长度，单测全绿，只有 PG 才炸。这个文件把两种形状都钉住。
"""

from __future__ import annotations

import pytest

from app.modules.planner.agents.base import (
    DEFAULT_TRANSPORT,
    TRANSPORT_VALUES,
    primary_transport,
    selected_transports,
)

COLUMN_LENGTH = 16  # itinerary_items.transport 的 String(16)


def test_list_keeps_order_and_drops_duplicates() -> None:
    assert selected_transports({"transport": ["metro", "walk", "metro"]}) == ("metro", "walk")


@pytest.mark.parametrize("bad", ["helicopter", "", "WALK", None, 3])
def test_invalid_values_are_filtered(bad: object) -> None:
    assert selected_transports({"transport": [bad]}) == ()


def test_legacy_single_string_still_works() -> None:
    """库里已经有按单值存的 preferences，读回来不能坏。"""
    assert selected_transports({"transport": "walk"}) == ("walk",)


def test_missing_or_weird_shapes_give_empty() -> None:
    assert selected_transports({}) == ()
    assert selected_transports({"transport": 42}) == ()


def test_primary_transport_is_single_value_that_fits_column() -> None:
    picked = primary_transport({"transport": ["bike", "walk"]})
    assert picked == "bike"
    assert "[" not in picked and "]" not in picked
    assert len(picked) <= COLUMN_LENGTH


def test_primary_transport_defaults_to_walk() -> None:
    assert primary_transport({}) == DEFAULT_TRANSPORT
    assert primary_transport({"transport": []}) == DEFAULT_TRANSPORT


def test_every_transport_value_fits_column() -> None:
    assert all(len(value) <= COLUMN_LENGTH for value in TRANSPORT_VALUES)
