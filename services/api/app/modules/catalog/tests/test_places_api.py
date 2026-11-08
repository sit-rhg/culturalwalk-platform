"""CA-03 验收测试：GET /places 的筛选与游标分页，GET /places/{id} 的详情。

测试自带内存 SQLite，不依赖本机/CI 的 PostgreSQL（表用 create_all 现建）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker

from app.modules.catalog.models import FactConfidence, Place, PlaceFact, PlaceSource, PlaceStatus

ENDPOINT = "/api/v1/places"
BASE_TIME = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

PLACE_KEYS = {
    "id",
    "name",
    "district",
    "lat",
    "lon",
    "summary",
    "culture_tags",
    "status",
    "crs",
}
DETAIL_KEYS = PLACE_KEYS | {"facts", "open_hours", "open_hours_verified_at"}


def _add_place(
    session_factory: sessionmaker[Session],
    *,
    name: str,
    district: str | None = "岳麓区",
    alias: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    status: PlaceStatus = PlaceStatus.VERIFIED,
    lat: str = "28.185000",
    lon: str = "112.935000",
    created_at: datetime | None = None,
    open_hours: str | None = None,
    crs: str = "OGC:CRS84",
) -> uuid.UUID:
    """直接落库造数据：接口只读，不走 POST /places（那张卡不在 CA-03 里）。"""
    with session_factory() as db:
        place = Place(
            name=name,
            district=district,
            alias=alias,
            summary=summary,
            culture_tags=list(tags or []),
            status=status,
            lat=Decimal(lat),
            lon=Decimal(lon),
            crs=crs,
            created_at=created_at or BASE_TIME,
            open_hours=open_hours,
        )
        db.add(place)
        db.commit()
        return place.id


def _add_fact(
    session_factory: sessionmaker[Session],
    place_id: uuid.UUID,
    *,
    claim: str,
    confidence: FactConfidence,
    source: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    with session_factory() as db:
        source_id = None
        if source is not None:
            row = PlaceSource(**source)
            db.add(row)
            db.flush()
            source_id = row.id
        fact = PlaceFact(
            place_id=place_id,
            claim=claim,
            confidence=confidence,
            source_id=source_id,
            created_at=created_at or BASE_TIME,
        )
        db.add(fact)
        db.commit()
        return fact.id


def _items(response: Response) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _names(response: Response) -> list[str]:
    return [item["name"] for item in _items(response)]


def test_list_shape_matches_contract(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """契约形状：Place 只有 8 个字段，列表里不带 facts/open_hours。"""
    _add_place(
        session_factory,
        name="岳麓书院",
        summary="中国四大书院之一",
        tags=["历史", "建筑"],
    )

    items = _items(client.get(ENDPOINT))

    assert len(items) == 1
    item = items[0]
    assert set(item) == PLACE_KEYS
    assert item["name"] == "岳麓书院"
    assert item["district"] == "岳麓区"
    assert item["culture_tags"] == ["历史", "建筑"]
    assert item["status"] == "verified"
    assert isinstance(item["lat"], float)
    assert abs(item["lat"] - 28.185) < 1e-9
    assert client.get(ENDPOINT).json()["next_cursor"] is None


def test_list_and_detail_echo_the_place_crs(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """GCJ-02 的坐标必须原样回显坐标系，否则调用方会当 WGS84 画到偏几百米的位置。"""
    place_id = _add_place(session_factory, name="火星坐标点", crs="GCJ-02")

    assert _items(client.get(ENDPOINT))[0]["crs"] == "GCJ-02"
    assert client.get(f"{ENDPOINT}/{place_id}").json()["crs"] == "GCJ-02"


def test_empty_catalog(client: TestClient) -> None:
    response = client.get(ENDPOINT)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_order_is_newest_first(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    _add_place(session_factory, name="最早", created_at=BASE_TIME)
    _add_place(session_factory, name="中间", created_at=BASE_TIME + timedelta(hours=1))
    _add_place(session_factory, name="最新", created_at=BASE_TIME + timedelta(hours=2))

    assert _names(client.get(ENDPOINT)) == ["最新", "中间", "最早"]


def test_cursor_pagination_walks_all_pages(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """5 条 / 每页 2 条 = 3 页；不漏、不重、最后一页 next_cursor 为 null。"""
    for index in range(5):
        _add_place(
            session_factory, name=f"点位{index}", created_at=BASE_TIME + timedelta(minutes=index)
        )

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    for _ in range(5):
        params: dict[str, Any] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        response = client.get(ENDPOINT, params=params)
        collected += [item["id"] for item in _items(response)]
        pages += 1
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break

    assert pages == 3
    # 一次性取全量（limit=5）作为基准，确认翻页结果不多不少
    everything = client.get(ENDPOINT, params={"limit": 5}).json()["items"]
    assert collected == [row["id"] for row in everything]
    assert len(set(collected)) == 5


def test_pagination_is_stable_for_identical_timestamps(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """created_at 相同（同一秒入库）时按 id 兜底排序，翻页不能重复或丢。"""
    first = _add_place(session_factory, name="同秒甲", created_at=BASE_TIME)
    second = _add_place(session_factory, name="同秒乙", created_at=BASE_TIME)

    page1 = client.get(ENDPOINT, params={"limit": 1})
    page2 = client.get(ENDPOINT, params={"limit": 1, "cursor": page1.json()["next_cursor"]})

    ids = [page1.json()["items"][0]["id"], page2.json()["items"][0]["id"]]
    assert set(ids) == {str(first), str(second)}
    assert ids == sorted(ids, reverse=True)  # id 倒序兜底


def test_keyword_matches_name_alias_and_summary(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _add_place(session_factory, name="岳麓书院")
    _add_place(session_factory, name="爱晚亭", alias="岳麓山爱晚亭")
    _add_place(session_factory, name="橘子洲", summary="位于岳麓山下的小洲")
    _add_place(session_factory, name="天心阁")

    assert set(_names(client.get(ENDPOINT, params={"q": "岳麓"}))) == {
        "岳麓书院",
        "爱晚亭",
        "橘子洲",
    }


def test_keyword_is_trimmed_and_blank_is_ignored(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _add_place(session_factory, name="岳麓书院")
    _add_place(session_factory, name="天心阁")

    assert len(_names(client.get(ENDPOINT, params={"q": "   "}))) == 2


def test_keyword_matches_ascii_case_insensitively(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _add_place(session_factory, name="Old Town Walk")
    _add_place(session_factory, name="橘子洲")

    assert _names(client.get(ENDPOINT, params={"q": "old town"})) == ["Old Town Walk"]


def test_keyword_wildcards_are_literal(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """负例：q 里的 % 和 _ 是普通字符，不能当通配符把整张表捞出来。"""
    _add_place(session_factory, name="折扣 100% 特展")
    _add_place(session_factory, name="橘子洲")

    assert _names(client.get(ENDPOINT, params={"q": "100%"})) == ["折扣 100% 特展"]
    assert _names(client.get(ENDPOINT, params={"q": "_"})) == []


def test_district_filter(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    _add_place(session_factory, name="岳麓书院", district="岳麓区")
    _add_place(session_factory, name="天心阁", district="天心区")
    _add_place(session_factory, name="贾谊故居", district="天心区")

    assert set(_names(client.get(ENDPOINT, params={"district": "天心区"}))) == {
        "天心阁",
        "贾谊故居",
    }
    assert _names(client.get(ENDPOINT, params={"district": "雨花区"})) == []


def test_tag_filter_uses_json_array_membership(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _add_place(session_factory, name="橘子洲", tags=["红色", "历史"])
    _add_place(session_factory, name="岳麓书院", tags=["历史", "建筑"])
    _add_place(session_factory, name="湖南烈士公园", tags=["红色", "自然"])

    assert set(_names(client.get(ENDPOINT, params={"tag": "红色"}))) == {"橘子洲", "湖南烈士公园"}
    # 负例：不能子串误伤（"历史" 不能被 "史" 命中）
    assert _names(client.get(ENDPOINT, params={"tag": "史"})) == []
    assert _names(client.get(ENDPOINT, params={"tag": "美食"})) == []


def test_filters_are_combined_with_and(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _add_place(session_factory, name="橘子洲", district="岳麓区", tags=["红色"])
    _add_place(session_factory, name="湖南烈士公园", district="开福区", tags=["红色"])
    _add_place(session_factory, name="岳麓书院", district="岳麓区", tags=["历史"])

    assert _names(client.get(ENDPOINT, params={"district": "岳麓区", "tag": "红色"})) == ["橘子洲"]
    assert (
        _names(client.get(ENDPOINT, params={"district": "岳麓区", "tag": "红色", "q": "烈士"}))
        == []
    )


def test_invalid_cursor_is_rejected(client: TestClient) -> None:
    """负例：游标乱传 → 400 + 固定错误码（不是 500）。"""
    response = client.get(ENDPOINT, params={"cursor": "not-a-cursor"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CURSOR"


def test_limit_out_of_range_is_rejected(client: TestClient) -> None:
    assert client.get(ENDPOINT, params={"limit": 0}).status_code == 422
    assert client.get(ENDPOINT, params={"limit": 51}).status_code == 422
    assert client.get(ENDPOINT, params={"limit": 50}).status_code == 200


def test_detail_includes_facts_and_sources(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    place_id = _add_place(session_factory, name="岳麓书院", open_hours="08:00-18:00")
    _add_fact(
        session_factory,
        place_id,
        claim="岳麓书院始建于北宋开宝九年",
        confidence=FactConfidence.VERIFIED,
        source={
            "title": "岳麓书院：百年实事求是，千载弦歌不绝",
            "url": "http://ccpd.china.com.cn/2023-10/08/content_42543919.html",
            "publisher": "中国网",
        },
        created_at=BASE_TIME,
    )
    _add_fact(
        session_factory,
        place_id,
        claim="书院门前两棵古樟传为朱熹手植",
        confidence=FactConfidence.LIKELY,
        source={"title": "长沙市志", "url": None, "publisher": "地方志"},
        created_at=BASE_TIME + timedelta(minutes=1),
    )

    response = client.get(f"{ENDPOINT}/{place_id}")

    assert response.status_code == 200, response.text
    detail = response.json()
    assert set(detail) == DETAIL_KEYS
    assert detail["open_hours"] == "08:00-18:00"
    assert [fact["claim"] for fact in detail["facts"]] == [
        "岳麓书院始建于北宋开宝九年",
        "书院门前两棵古樟传为朱熹手植",
    ]
    verified, likely = detail["facts"]
    assert verified["confidence"] == "verified"
    assert verified["sources"] == [
        {
            "title": "岳麓书院：百年实事求是，千载弦歌不绝",
            "url": "http://ccpd.china.com.cn/2023-10/08/content_42543919.html",
            "publisher": "中国网",
        }
    ]
    # 书志类来源没有 URL，序列化成 null 而不是空串
    assert likely["sources"] == [{"title": "长沙市志", "url": None, "publisher": "地方志"}]


def test_detail_hides_facts_without_source(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """没有来源的说法不进公开内容（必读1「数据要求」、docs/01 §2.2）。"""
    place_id = _add_place(session_factory, name="无来源过滤点")
    _add_fact(
        session_factory,
        place_id,
        claim="有官方来源的说法",
        confidence=FactConfidence.VERIFIED,
        source={
            "title": "长沙市文旅广电局",
            "url": "http://wlgd.changsha.gov.cn/",
            "publisher": "长沙市文旅广电局",
        },
    )
    _add_fact(
        session_factory,
        place_id,
        claim="只在内部备忘、还没有出处的说法",
        confidence=FactConfidence.UNVERIFIED,
        created_at=BASE_TIME + timedelta(minutes=1),
    )

    response = client.get(f"{ENDPOINT}/{place_id}")

    assert response.status_code == 200, response.text
    assert [fact["claim"] for fact in response.json()["facts"]] == ["有官方来源的说法"]


def test_detail_timestamps_are_timezone_aware(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    place_id = _add_place(session_factory, name="天心阁", open_hours="09:00-17:00")
    with session_factory() as db:
        place = db.get(Place, place_id)
        assert place is not None
        place.open_hours_verified_at = datetime(2026, 10, 2, 9, 30)
        db.commit()

    detail = client.get(f"{ENDPOINT}/{place_id}").json()

    parsed = datetime.fromisoformat(detail["open_hours_verified_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_detail_not_found(client: TestClient) -> None:
    response = client.get(f"{ENDPOINT}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLACE_NOT_FOUND"


def test_detail_with_malformed_id_is_validation_error(client: TestClient) -> None:
    response = client.get(f"{ENDPOINT}/not-a-uuid")

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_FAILED"
    assert body["details"][0]["field"] == "place_id"


def test_list_hides_draft_places(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """公开列表只给已发布：草稿点位连"存在过"都不该被搜到。

    必读1 §5 第 9 条：公开读接口只能读已发布内容；草稿、审核中、已下线一律 404。
    """
    published = _add_place(session_factory, name="已核实点位")
    _add_place(session_factory, name="草稿点位", status=PlaceStatus.DRAFT)

    body = client.get(ENDPOINT).json()

    assert [item["id"] for item in body["items"]] == [str(published)]


def test_detail_of_draft_place_is_hidden(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """草稿详情按"不存在"处理：返回 404 而不是 200 + status=draft，避免泄露存在性。"""
    place_id = _add_place(session_factory, name="待核实点位", status=PlaceStatus.DRAFT)

    response = client.get(f"{ENDPOINT}/{place_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLACE_NOT_FOUND"
