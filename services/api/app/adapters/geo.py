# CulturalWalk API · 地图/地理编码 Adapter
"""地图 Provider 的唯一接入边界。

为什么单独一层：
- docs（VibeCoding 底层架构 §9.2）把 canonical 坐标定成 WGS84 / `OGC:CRS84`，
  供应商常见的 GCJ-02、BD-09 不得冒充 WGS84；换算只能发生在这里，
  业务层和持久化层不许散落偏移公式。
- 供应商坐标系、合同与商用许可当前是「待核实」，所以真实 Provider 在核验前
  fail-closed：宁可暂时没有地理编码，也不能拿猜的 datum 写进数据库。
- 调用方只依赖 `MapProvider` 协议：单测拿 FakeMapProvider 的固定坐标，
  外网挂了、供应商挂了都不影响测试。

范围说明：docs §9.1 里的 `route_preview` / `navigation_link` / `tile_config`
本卡没有实现——路线预览需要道路 API 的商用许可，导航链接与瓦片配置属于公开配置，
都要等供应商条款核验后再接；先只落地 geocode / reverse_geocode 两个内部端口。
"""

from __future__ import annotations

from math import hypot
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import Settings, get_settings
from app.core.errors import AppError

# 中国境内的大致经纬范围：长沙纬度约 27~29、经度约 111~114。
# 用「纬 112 / 经 28」这种组合做写反检测，比单看越界准 —— 两值都在合法区间内、
# 但互相落在对方的地理带里，只有轴序颠倒这一种解释。
_CHINA_LAT_RANGE = (18.0, 54.0)
_CHINA_LON_RANGE = (73.0, 136.0)
AXIS_SWAP_MESSAGE = "经纬度疑似写反：长沙一带的纬度应在 27~29、经度应在 111~114"


class MapProviderUnavailable(AppError):
    """供应商不可用、未配置或尚未核验。稳定错误码，前端据此降级。"""

    http_status = 503
    code = "MAP_PROVIDER_UNAVAILABLE"


class MapProviderInvalidData(AppError):
    """供应商返回了越界坐标、未知 datum 之类的坏数据。不降级，直接上报。"""

    http_status = 502
    code = "MAP_PROVIDER_INVALID_DATA"


class GeoPoint(BaseModel):
    """canonical 坐标：十进制度 + 显式 crs/datum。

    datum 不给默认值以外的选项，就是为了让「GCJ-02 冒充 WGS84」在解析层直接失败；
    缺 datum 的输入也不接受（pydantic 会报 missing）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lat: float = Field(ge=-90, le=90, description="纬度，WGS84 十进制度")
    lon: float = Field(ge=-180, le=180, description="经度，WGS84 十进制度")
    crs: Literal["OGC:CRS84"] = "OGC:CRS84"
    datum: Literal["WGS84"] = "WGS84"

    @model_validator(mode="before")
    @classmethod
    def _reject_suspected_axis_swap(cls, data: object) -> object:
        # 放在 before 里，是因为 lat=112 这种值会先被 Field(le=90) 拦掉，
        # 那样报错就成了「数值太大」，看不出真正原因是经纬写反。
        if isinstance(data, dict):
            lat, lon = data.get("lat"), data.get("lon")
            if _looks_swapped(lat, lon):
                raise ValueError(AXIS_SWAP_MESSAGE)
        return data


def _looks_swapped(lat: object, lon: object) -> bool:
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return False
    return (
        _CHINA_LON_RANGE[0] <= lat <= _CHINA_LON_RANGE[1]
        and _CHINA_LAT_RANGE[0] <= lon <= _CHINA_LAT_RANGE[1]
        and not (
            _CHINA_LAT_RANGE[0] <= lat <= _CHINA_LAT_RANGE[1]
            and _CHINA_LON_RANGE[0] <= lon <= _CHINA_LON_RANGE[1]
        )
    )


class PlaceCandidate(BaseModel):
    """地理编码的一条候选结果。precision 必须显式声明，不许留空让上游猜。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    address: str | None = None
    point: GeoPoint
    provider: str
    provider_place_id: str | None = None
    precision: Literal["provider", "manual", "approx"] = "provider"


class Address(BaseModel):
    """反地理编码结果。近似结果要如实标 approx，前端据此决定是否提示人工核对。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    formatted: str
    point: GeoPoint
    provider: str
    precision: Literal["provider", "approx"] = "provider"


@runtime_checkable
class MapProvider(Protocol):
    """供应商无关的内部端口，签名与 docs §9.1 一致。"""

    name: str

    def geocode(
        self, query: str, *, region: str | None = None, timeout: float = 3.0
    ) -> list[PlaceCandidate]: ...

    def reverse_geocode(self, lat: float, lon: float, *, timeout: float = 3.0) -> Address: ...


# 长沙几个地标的固定坐标：给单测和离线演示用，不联网、不随供应商漂移。
_LANDMARKS: tuple[tuple[str, str, float, float], ...] = (
    ("岳麓书院", "长沙市岳麓区麓山路 273 号", 28.1867, 112.9510),
    ("橘子洲", "长沙市岳麓区橘子洲头", 28.1760, 112.9610),
    ("天心阁", "长沙市天心区天心路 17 号", 28.1850, 112.9820),
    ("太平老街", "长沙市天心区太平街", 28.1940, 112.9740),
    ("湖南省博物馆", "长沙市开福区东风路 50 号", 28.2130, 112.9940),
)

# 离已知点 0.01°（约 1.1 km）以内才敢说「就是这里」，否则只能给近似地址。
_NEAR_DEGREES = 0.01


class FakeMapProvider:
    """离线 Provider：固定坐标、确定性输出，没有就返回空，绝不编造。"""

    name = "fake"

    def geocode(
        self, query: str, *, region: str | None = None, timeout: float = 3.0
    ) -> list[PlaceCandidate]:
        needle = query.strip()
        for name, address, lat, lon in _LANDMARKS:
            if needle == name:
                return [
                    PlaceCandidate(
                        name=name,
                        address=f"{address}（{region}）" if region else address,
                        point=GeoPoint(lat=lat, lon=lon),
                        provider=self.name,
                        provider_place_id=f"fake:{name}",
                    )
                ]
        return []

    def reverse_geocode(self, lat: float, lon: float, *, timeout: float = 3.0) -> Address:
        del timeout
        point = GeoPoint(lat=lat, lon=lon)
        nearest = min(_LANDMARKS, key=lambda item: hypot(item[2] - lat, item[3] - lon))
        if hypot(nearest[2] - lat, nearest[3] - lon) <= _NEAR_DEGREES:
            return Address(
                formatted=f"{nearest[0]}（{nearest[1]}）",
                point=point,
                provider=self.name,
            )
        return Address(
            formatted=f"长沙市附近（未收录该坐标的具体地址：{lat:.4f}, {lon:.4f}）",
            point=point,
            provider=self.name,
            precision="approx",
        )


class _UnverifiedProvider:
    """真实 Provider 的公共骨架：核验前一律不可用。

    不是「懒得写 HTTP」：供应商返回的是 GCJ-02 还是 WGS84、偏移多少、
    商用条款允许到什么程度，docs §9.2 明确要求接入前留档，当前全是待核实。
    这层骨架保证一旦配置写错（.env 里填了 amap），错误是稳定的 503，
    而不是把疑似偏移坐标悄悄写进库里。
    """

    name = "unverified"

    def __init__(self, *, api_key: str, datum_verified: bool = False) -> None:
        self._api_key = api_key
        self._datum_verified = datum_verified

    def _ensure_usable(self) -> None:
        if not self._datum_verified:
            raise MapProviderUnavailable(
                f"{self.name} 的坐标系/许可尚未核验（docs §9.2），核验前禁止地理编码"
            )
        if not self._api_key:
            raise MapProviderUnavailable(f"{self.name} 未配置 API key")
        raise MapProviderUnavailable(f"{self.name} 的 HTTP 适配待 ADR-0013 冻结坐标基准后接入")

    def geocode(
        self, query: str, *, region: str | None = None, timeout: float = 3.0
    ) -> list[PlaceCandidate]:
        del query, region, timeout
        self._ensure_usable()
        raise AssertionError("unreachable")  # pragma: no cover - _ensure_usable 必抛

    def reverse_geocode(self, lat: float, lon: float, *, timeout: float = 3.0) -> Address:
        del lat, lon, timeout
        self._ensure_usable()
        raise AssertionError("unreachable")  # pragma: no cover


class AmapProvider(_UnverifiedProvider):
    name = "amap"


class TencentProvider(_UnverifiedProvider):
    name = "tencent"


class FallbackMapProvider:
    """主 Provider 挂了就退到 fallback，并把「这次是降级结果」记在实例上。

    只吞两种失败：供应商不可用和超时 —— 这是外部依赖的正常故障模式。
    坏数据（MapProviderInvalidData）不吞：那说明契约对不上，掩盖它只会让
    错误坐标顺利入库。
    """

    def __init__(self, *, primary: MapProvider, fallback: MapProvider) -> None:
        self._primary = primary
        self._fallback = fallback
        self.last_degraded = False

    @property
    def name(self) -> str:
        return f"{self._primary.name}+fallback:{self._fallback.name}"

    def geocode(
        self, query: str, *, region: str | None = None, timeout: float = 3.0
    ) -> list[PlaceCandidate]:
        try:
            candidates = self._primary.geocode(query, region=region, timeout=timeout)
        except (MapProviderUnavailable, TimeoutError):
            self.last_degraded = True
            return self._fallback.geocode(query, region=region, timeout=timeout)
        self.last_degraded = False
        return candidates

    def reverse_geocode(self, lat: float, lon: float, *, timeout: float = 3.0) -> Address:
        try:
            address = self._primary.reverse_geocode(lat, lon, timeout=timeout)
        except (MapProviderUnavailable, TimeoutError):
            self.last_degraded = True
            return self._fallback.reverse_geocode(lat, lon, timeout=timeout)
        self.last_degraded = False
        return address


def get_map_provider(settings: Settings | None = None) -> MapProvider:
    """按配置装配 Provider。fake 是唯一开箱即用的；真实 Provider 核验前拒绝装配。"""
    resolved = settings or get_settings()
    name = resolved.map_provider.strip().lower()

    if name == "fake":
        return FakeMapProvider()

    if name in {"amap", "tencent"}:
        provider_cls = AmapProvider if name == "amap" else TencentProvider
        if not resolved.map_datum_verified:
            raise MapProviderUnavailable(
                f"{name} 的坐标系/许可尚未核验（docs §9.2），核验前只允许 map_provider=fake"
            )
        if not resolved.map_api_key:
            raise MapProviderUnavailable(f"{name} 未配置 MAP_API_KEY")
        return provider_cls(api_key=resolved.map_api_key, datum_verified=True)

    raise MapProviderUnavailable(f"未知的地图 Provider：{name}（可选 fake | amap | tencent）")


# 便于调用方按名字取一组候选，而不必关心实现细节。
__all__ = [
    "AXIS_SWAP_MESSAGE",
    "Address",
    "AmapProvider",
    "FallbackMapProvider",
    "FakeMapProvider",
    "GeoPoint",
    "MapProvider",
    "MapProviderInvalidData",
    "MapProviderUnavailable",
    "PlaceCandidate",
    "TencentProvider",
    "get_map_provider",
]
