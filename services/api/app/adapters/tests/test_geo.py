"""CA-04 地理编码 Adapter 的验收用例（先写红，再实现）。

这里只测 Adapter 本身：不连数据库、不连外网。
高德/腾讯的真实 HTTP 细节故意不实现——docs/09 §9.2 写明「供应商坐标系尚未核验，
无法确认时禁止地理编码」，所以真实 Provider 在核验前必须 fail-closed，
本文件只验它确实关着，以及关掉之后 fake 仍然可用。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.adapters.geo import (
    AXIS_SWAP_MESSAGE,
    Address,
    AmapProvider,
    FakeMapProvider,
    FallbackMapProvider,
    GeoPoint,
    MapProviderInvalidData,
    MapProviderUnavailable,
    PlaceCandidate,
    get_map_provider,
)
from app.core.config import Settings


def test_fake_returns_fixed_coordinates_for_known_landmark() -> None:
    """单测靠 fake 拿固定坐标：岳麓书院的点必须是写死的那一个，不能随实现漂移。"""
    provider = FakeMapProvider()
    candidates = provider.geocode("岳麓书院", region="长沙")

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.name == "岳麓书院"
    assert candidate.point.lat == pytest.approx(28.1867)
    assert candidate.point.lon == pytest.approx(112.9510)
    assert candidate.point.crs == "OGC:CRS84"
    assert candidate.point.datum == "WGS84"
    assert candidate.provider == "fake"


def test_fake_does_not_invent_coordinates_for_unknown_query() -> None:
    """没收录的查询返回空列表，而不是编一个坐标——宁可让人工补，也不给假数据。"""
    assert FakeMapProvider().geocode("不存在的某某祠") == []


def test_fake_is_deterministic() -> None:
    provider = FakeMapProvider()
    first = provider.geocode("橘子洲")
    second = provider.geocode("橘子洲")
    assert first == second


def test_fake_reverse_geocode_returns_nearest_known_address() -> None:
    address = FakeMapProvider().reverse_geocode(28.1870, 112.9512)

    assert isinstance(address, Address)
    assert "岳麓书院" in address.formatted
    assert address.point.datum == "WGS84"


def test_fake_reverse_geocode_marks_unknown_area_as_approximate() -> None:
    """离所有已知点都很远时给近似地址，但 precision 必须诚实标成 approx。"""
    address = FakeMapProvider().reverse_geocode(31.2300, 121.4700)

    assert address.precision == "approx"
    assert "未收录" in address.formatted


def test_geopoint_rejects_lat_out_of_range() -> None:
    with pytest.raises(ValidationError):
        GeoPoint(lat=91.0, lon=112.0)


def test_geopoint_rejects_lon_out_of_range() -> None:
    with pytest.raises(ValidationError):
        GeoPoint(lat=28.0, lon=181.0)


def test_geopoint_rejects_non_wgs84_datum() -> None:
    """GCJ-02 的坐标不许冒充 WGS84：datum 只能是显式的 WGS84。"""
    with pytest.raises(ValidationError):
        # 故意传非法 datum：这正是本用例要证明会被拒绝的输入，类型检查也该拦。
        GeoPoint(lat=28.0, lon=112.0, datum="GCJ-02")  # type: ignore[arg-type]


def test_geopoint_rejects_unknown_crs() -> None:
    with pytest.raises(ValidationError):
        GeoPoint(lat=28.0, lon=112.0, crs="EPSG:4326")  # type: ignore[arg-type]


def test_geopoint_rejects_suspected_axis_swap() -> None:
    """长沙的经度在 111~114、纬度在 27~29；纬 112 / 经 28 几乎肯定是写反了。"""
    with pytest.raises(ValidationError, match=AXIS_SWAP_MESSAGE):
        GeoPoint(lat=112.95, lon=28.18)


def test_fallback_degrades_when_primary_is_unavailable() -> None:
    provider = FallbackMapProvider(
        primary=AmapProvider(api_key="", datum_verified=False),
        fallback=FakeMapProvider(),
    )

    candidates = provider.geocode("天心阁")

    assert [c.name for c in candidates] == ["天心阁"]
    assert provider.last_degraded is True


def test_fallback_recovers_after_primary_failure() -> None:
    """降级只作用于本次调用，下一次主 Provider 恢复就应立即用回主 Provider。"""
    flaky = _FlakyProvider(failures=1)
    provider = FallbackMapProvider(primary=flaky, fallback=FakeMapProvider())

    assert provider.geocode("橘子洲")
    assert provider.last_degraded is True
    assert provider.geocode("橘子洲")
    assert provider.last_degraded is False


def test_fallback_degrades_on_timeout() -> None:
    provider = FallbackMapProvider(primary=_TimingOutProvider(), fallback=FakeMapProvider())

    assert provider.geocode("太平老街")
    assert provider.last_degraded is True


def test_fallback_does_not_swallow_invalid_data() -> None:
    """坏数据不能靠降级掩盖：校验失败要继续往上抛，交给调用方决定。"""
    provider = FallbackMapProvider(primary=_CorruptProvider(), fallback=FakeMapProvider())

    with pytest.raises(MapProviderInvalidData):
        provider.geocode("天心阁")


def test_default_settings_builds_fake_provider() -> None:
    assert isinstance(get_map_provider(Settings(map_provider="fake")), FakeMapProvider)


def test_amap_is_disabled_until_datum_is_verified() -> None:
    """核验前接真实 Provider 就是编数据，必须 fail-closed 并给稳定错误码。"""
    with pytest.raises(MapProviderUnavailable) as excinfo:
        get_map_provider(Settings(map_provider="amap", map_api_key="dummy"))

    assert excinfo.value.code == "MAP_PROVIDER_UNAVAILABLE"
    assert excinfo.value.http_status == 503


def test_tencent_is_disabled_until_datum_is_verified() -> None:
    with pytest.raises(MapProviderUnavailable):
        get_map_provider(Settings(map_provider="tencent", map_api_key="dummy"))


def test_unknown_provider_name_is_rejected() -> None:
    with pytest.raises(MapProviderUnavailable):
        get_map_provider(Settings(map_provider="baidu"))


def test_candidate_precision_must_be_declared() -> None:
    """precision 缺省要显式落成 provider，不允许留 None 让上游猜。"""
    candidate = PlaceCandidate(
        name="测试点",
        point=GeoPoint(lat=28.0, lon=112.0),
        provider="fake",
    )
    assert candidate.precision == "provider"


class _FlakyProvider:
    """第一次抛 MAP_PROVIDER_UNAVAILABLE，之后恢复正常。"""

    name = "flaky"

    def __init__(self, failures: int) -> None:
        self._remaining = failures

    def geocode(self, query: str, *, region: str | None = None, timeout: float = 3.0):
        if self._remaining > 0:
            self._remaining -= 1
            raise MapProviderUnavailable("模拟供应商 503")
        return FakeMapProvider().geocode(query, region=region, timeout=timeout)

    def reverse_geocode(self, lat: float, lon: float, *, timeout: float = 3.0):
        return FakeMapProvider().reverse_geocode(lat, lon, timeout=timeout)


class _TimingOutProvider:
    name = "slow"

    def geocode(self, query: str, *, region: str | None = None, timeout: float = 3.0):
        raise TimeoutError("模拟供应商超时")

    def reverse_geocode(self, lat: float, lon: float, *, timeout: float = 3.0):
        raise TimeoutError("模拟供应商超时")


class _CorruptProvider:
    name = "corrupt"

    def geocode(self, query: str, *, region: str | None = None, timeout: float = 3.0):
        raise MapProviderInvalidData("供应商返回的坐标越界")

    def reverse_geocode(self, lat: float, lon: float, *, timeout: float = 3.0):
        raise MapProviderInvalidData("供应商返回的地址为空")
