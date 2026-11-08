# 行程费用 · 价格解析与可信度判定
"""把「一句说法」变成「一个价格区间」，并判断这个数字能不能拿去给用户看。

只有三件事，一件都不碰数据库：

1. **抠价格**：从文案里认出「免费」「门票 60 元」「票价 30–60 元」，统一换算成「分」；
2. **定可信度**：来源已核实才敢说 `verified`，只是「较可信」就说 `likely`，
   剩下的一律 `unverified` —— 前端看到不是 `verified` 就显示「待核实」；
3. **算汇总**：「已知项求和」与「未知项单独计数」分开算，**绝不把缺失当成 0**。

文案写法有优先级，改之前先看这条：**免费 > 区间 > 单价**。
「免费开放」「免票」直接判 0；否则先找「30–60 元」这种区间；最后才找「票 60 元」这种单价。
顺序错了会把「免费，原价 60 元」认成 60 元——这类错在页面上看不出来，只会悄悄多收钱。

单独成文件是因为这几条规则最容易被顺手改坏：改一个正则就可能把
「免费开放」认成没有价格，而这类错在页面上表现为「一片空白」，很难被发现。
和落库、接口分开之后，测试只要喂字符串就能把规则钉死。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol

# 只有这两档能拿数字给用户看；unverified 一律显示「待核实」。
TRUSTED_CONFIDENCE: Final[frozenset[str]] = frozenset({"verified", "likely"})
VERIFIED: Final[str] = "verified"
LIKELY: Final[str] = "likely"
UNVERIFIED: Final[str] = "unverified"

# 契约里 confidence 只有这三个值，排序是为了「优先采用最可信的那条说法」。
_CONFIDENCE_RANK: Final[dict[str, int]] = {VERIFIED: 0, LIKELY: 1, UNVERIFIED: 2}

# 「免费」的几种常见写法。文档里长沙不少点位是免费开放的，认不出来就会被标成
# 「待核实」——对用户来说这是更差的结果：明明免费，却说要核实。
_FREE_WORDS: Final[tuple[str, ...]] = (
    "免费开放",
    "免费参观",
    "免费",
    "免票",
    "无需门票",
    "不收门票",
)

# 区间：30-60 元 / 30~60 元 / 30 至 60 元 / 30–60 元（全角半角横线都认）。
_PRICE_RANGE_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-–—~～至到]\s*(\d+(?:\.\d+)?)\s*(?:元|块)"
)
# 单价：60 元 / 60元/人 / 门票 60 元
_PRICE_SINGLE_RE: Final[re.Pattern[str]] = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)")

# 展示文案（中文，直接进接口的 reason 字段，前端原样显示）。
REASON_VERIFIED: Final[str] = "门票价格来自已核实来源"
REASON_LIKELY: Final[str] = "门票价格来自较可信来源，尚未最终核实"
REASON_UNTRUSTED: Final[str] = "价格来源未核实，仅供参考"
REASON_UNKNOWN: Final[str] = "没有价格来源，待核实"
REASON_PLACE_MISSING: Final[str] = "点位资料缺失，待核实"


class ClaimLike(Protocol):
    """价格判定只依赖这几个字段，方便测试直接塞轻量对象。"""

    claim: str
    confidence: str
    source_urls: Sequence[str]
    # 来源的编号（catalog 里 place_sources.id）。URL 会改版/失效，编号不会，
    # 所以行程项要留的是编号；URL 只用于给人看。
    source_ids: Sequence[str]


class PricedLike(Protocol):
    """汇总只读这两个字段，行程项实体天然满足。"""

    price_min: int | None
    price_max: int | None


@dataclass(frozen=True)
class PriceQuote:
    """一条点位算出来的价格结论。

    `min_cents`/`max_cents` 为 `None` 表示「没有可用的价格」，而不是「0 元」——
    这两件事在页面上的差别是「待核实」和「免费」，混起来就是假数据。
    """

    min_cents: int | None
    max_cents: int | None
    confidence: str
    source_urls: tuple[str, ...] = ()
    reason: str = REASON_UNKNOWN
    # 采用的那条说法背后的来源编号（可以是多条：同一句话可能引了两份资料）。
    # 放在最后，避免改动已有按位置传参的调用点。
    source_ids: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        """有没有价格数字可显示。"""
        return self.min_cents is not None and self.max_cents is not None

    @property
    def pending(self) -> bool:
        """要不要打「待核实」。按文档口径：不是 verified 就得打。"""
        return self.confidence != VERIFIED


@dataclass(frozen=True)
class CostTotal:
    """整份行程的费用汇总（契约 CostSummary）。"""

    min_total: int = 0
    max_total: int = 0
    currency: str = "CNY"
    unknown_count: int = 0


def parse_price_cents(text: str) -> tuple[int, int] | None:
    """从一段中文文案里抠出价格区间（单位：分）。认不出就返回 None。

    先认「免费」再认数字：`免费开放（原价 60 元）`这种写法如果先认数字，
    就会得出 60 元，与事实相反。
    """
    if not text:
        return None
    if any(word in text for word in _FREE_WORDS):
        return (0, 0)
    range_match = _PRICE_RANGE_RE.search(text)
    if range_match is not None:
        low = _to_cents(range_match.group(1))
        high = _to_cents(range_match.group(2))
        if low is not None and high is not None:
            # 写成「60–30 元」时按小到大摆正，别原样输出一个不可能的反向区间。
            return (min(low, high), max(low, high))
    single_match = _PRICE_SINGLE_RE.search(text)
    if single_match is not None:
        value = _to_cents(single_match.group(1))
        if value is not None:
            return (value, value)
    return None


def _to_cents(raw: str) -> int | None:
    """元 → 分。用 Decimal 而不是 float*100：8.7 这种值乘 100 会飘成 869.99…"""
    try:
        amount = Decimal(raw) * 100
    except InvalidOperation:  # 正则已经限定成数字，防御性兜底
        return None
    return int(amount.quantize(Decimal("1")))


def quote_from_claims(claims: Iterable[ClaimLike]) -> PriceQuote:
    """在一条点位的若干条说法里，挑出最可信的那个价格。

    取舍顺序写死：verified → likely → unverified。同一档里取第一条能解析出价格的，
    不做加权也不做平均——两种说法给出不同票价时，平均出来的数字两边都不对。
    """
    ordered = sorted(claims, key=lambda claim: _CONFIDENCE_RANK.get(claim.confidence, 9))
    fallback: PriceQuote | None = None
    for claim in ordered:
        prices = parse_price_cents(claim.claim)
        if prices is None:
            continue
        urls = tuple(str(url) for url in claim.source_urls if url)
        ids = tuple(str(sid) for sid in getattr(claim, "source_ids", ()) if sid)
        if claim.confidence in TRUSTED_CONFIDENCE:
            reason = REASON_VERIFIED if claim.confidence == VERIFIED else REASON_LIKELY
            return PriceQuote(
                min_cents=prices[0],
                max_cents=prices[1],
                confidence=claim.confidence,
                source_urls=urls,
                reason=reason,
                source_ids=ids,
            )
        if fallback is None:
            # 有数字但来源不可信：数字给了，但必须挂 unverified + 说明，
            # 让前端显示「待核实」。藏着不给数字，用户反而没法判断量级。
            fallback = PriceQuote(
                min_cents=prices[0],
                max_cents=prices[1],
                confidence=UNVERIFIED,
                source_urls=urls,
                reason=REASON_UNTRUSTED,
                source_ids=ids,
            )
    if fallback is not None:
        return fallback
    return PriceQuote(
        min_cents=None,
        max_cents=None,
        confidence=UNVERIFIED,
        reason=REASON_UNKNOWN,
    )


def summarize(items: Iterable[PricedLike], *, people: int = 1) -> CostTotal:
    """把行程里每一项的价格加总。

    缺价格的行不计入金额、单独计数：文档明确要求「缺失项单独计数，不假装是 0」，
    否则用户看到「0 元」会以为这一天真的不花钱。

    单价是**每人**的票价，行程是多少人就得乘多少人：不乘的话两个人去博物馆
    只收一张票的钱，预算校验也就永远不会触发（D1）。
    """
    headcount = max(1, int(people or 1))
    min_total = 0
    max_total = 0
    unknown = 0
    for item in items:
        if item.price_min is None or item.price_max is None:
            unknown += 1
            continue
        min_total += item.price_min
        max_total += item.price_max
    return CostTotal(
        min_total=min_total * headcount,
        max_total=max_total * headcount,
        unknown_count=unknown,
    )
