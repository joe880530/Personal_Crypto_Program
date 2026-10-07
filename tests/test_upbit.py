"""업비트 시세 조회 검증.

응답 형식은 2026-10-07에 실제 호출로 확인한 것을 그대로 흉내 냈다. 특히
세 가지를 못으로 박아 둔다 — 셋 다 추측했으면 틀렸을 것들이다.

1. 종가는 `trade_price`다 (`close`가 아니다).
2. count는 200이 한도인데 넘겨 요청해도 **오류 없이 잘린다**.
3. `to`는 UTC이고 그 시각을 포함하지 않는다.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse

import pandas as pd
import pytest

from stockbot.data.upbit import (
    MAX_COUNT,
    UpbitError,
    UpbitProvider,
    UpbitQuotes,
    UpbitTransientError,
    _to_param,
)

UTC = dt.timezone.utc


def candle(stamp: dt.datetime, close: float) -> dict:
    """실제 응답 한 건과 같은 모양."""
    return {
        "market": "KRW-BTC",
        "candle_date_time_utc": stamp.strftime("%Y-%m-%dT%H:%M:%S"),
        "candle_date_time_kst": (stamp + dt.timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S"),
        "opening_price": close - 1000,
        "high_price": close + 2000,
        "low_price": close - 3000,
        "trade_price": close,              # <- 종가
        "timestamp": 1791337952589,
        "candle_acc_trade_price": 99489902.65653,
        "candle_acc_trade_volume": 0.86092696,
        "unit": 1,
    }


class FakeUpbit:
    """업비트처럼 동작하는 가짜 서버.

    `to` 이전(미포함) 구간을 최신순으로 돌려주고, count가 200을 넘으면
    **오류 없이 200으로 자른다** — 실제로 그렇게 동작한다.
    """

    def __init__(self, days: int = 700, start=dt.datetime(2024, 1, 1, tzinfo=UTC)):
        self.bars = [start + dt.timedelta(days=i) for i in range(days)]
        self.requests: list[dict] = []
        self.hard_cap = MAX_COUNT

    def __call__(self, url: str):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.requests.append(query)
        if "market/all" in url:
            return 200, [
                {"market": "KRW-BTC", "korean_name": "비트코인",
                 "market_event": {"warning": False, "caution": {"PRICE_FLUCTUATIONS": False}}},
                {"market": "KRW-XYZ", "korean_name": "문제코인",
                 "market_event": {"warning": True, "caution": {"PRICE_FLUCTUATIONS": True}}},
            ]

        count = int(query.get("count", 1))
        to = query.get("to")
        upto = (dt.datetime.strptime(to, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                if to else self.bars[-1] + dt.timedelta(days=1))
        older = [b for b in self.bars if b < upto]          # to 는 포함하지 않는다
        page = older[-min(count, self.hard_cap):]           # 200 초과는 조용히 잘린다
        return 200, [candle(b, 100_000_000 + i) for i, b in enumerate(reversed(page))]


def quotes(transport) -> UpbitQuotes:
    return UpbitQuotes(transport=transport, pace=0, sleep=lambda _: None)


# ------------------------------------------------------------------ 필드
def test_close_comes_from_trade_price():
    """`close`가 아니라 `trade_price`다. 이름만 보고 짐작하면 틀린다."""
    frame = quotes(FakeUpbit()).candles("KRW-BTC", unit=1, count=3)
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame["close"].iloc[-1] == 100_000_000
    assert frame["open"].iloc[-1] == 100_000_000 - 1000
    assert frame.index.is_monotonic_increasing, "시간순으로 정렬돼야 합니다"


def test_a_renamed_field_fails_loudly():
    """형식이 바뀌면 0으로 때우지 말고 무엇이 왔는지 보여주며 멈춰야 한다."""
    def renamed(url):
        row = candle(dt.datetime(2026, 1, 1, tzinfo=UTC), 1.0)
        row["price"] = row.pop("trade_price")
        return 200, [row]

    with pytest.raises(UpbitError) as exc:
        quotes(renamed).candles("KRW-BTC", unit=1)
    assert "trade_price" in str(exc.value)
    assert "받은 필드" in str(exc.value)


# ------------------------------------------------------------------ count
def test_more_than_the_cap_is_never_requested():
    """200을 넘겨 요청하면 오류가 아니라 조용히 잘린다. 애초에 넘기지 않는다."""
    fake = FakeUpbit()
    quotes(fake).candles("KRW-BTC", unit=1, count=500)
    assert int(fake.requests[-1]["count"]) == MAX_COUNT


def test_paging_follows_what_arrived_not_what_was_asked():
    """조용히 잘리는 응답에서 '요청한 만큼 왔다'고 가정하면 구간이 빈다.

    KIS에서 "38일 받았다"고 하고 18일만 저장된 것과 같은 종류의 사고다.
    여기서는 가짜 서버가 한 번에 50건만 주도록 해서, 그래도 구간이 온전히
    채워지는지 본다.
    """
    fake = FakeUpbit(days=400)
    fake.hard_cap = 50                     # 200을 달라고 해도 50만 온다
    frame = quotes(fake).history("KRW-BTC", dt.datetime(2024, 1, 1, tzinfo=UTC), unit=None)

    assert len(frame) == 400, f"구간이 빕니다: {len(frame)}건"
    assert not frame.index.has_duplicates


def test_paging_stops_when_the_same_page_returns():
    """상장일 이전으로 가면 같은 구간이 계속 온다. 그대로 두면 무한루프다."""
    class Stuck(FakeUpbit):
        def __call__(self, url):
            if "market/all" in url:
                return super().__call__(url)
            self.requests.append({})
            return 200, [candle(self.bars[0], 1.0)]

    fake = Stuck(days=10)
    frame = quotes(fake).history("KRW-BTC", dt.datetime(2020, 1, 1, tzinfo=UTC))
    assert len(frame) == 1
    assert len(fake.requests) <= 3, f"무한히 맴돌았습니다: {len(fake.requests)}회"


# ------------------------------------------------------------------ to
def test_to_is_sent_as_utc():
    """타임존 표기 없이 보내면 업비트가 UTC로 읽는다. 9시간이 어긋난다."""
    kst = dt.timezone(dt.timedelta(hours=9))
    assert _to_param(dt.datetime(2026, 10, 1, 9, 0, tzinfo=kst)) == "2026-10-01T00:00:00Z"
    assert _to_param(dt.datetime(2026, 10, 1, 0, 0)) == "2026-10-01T00:00:00Z"


def test_an_aware_start_can_be_compared_with_the_response():
    """응답의 시각에는 타임존 표기가 없다. 표기가 붙은 값과 그냥 비교하면
    pandas가 거부한다(실제로 여기서 한 번 걸렸다)."""
    frame = quotes(FakeUpbit(days=30)).history(
        "KRW-BTC", dt.datetime(2024, 1, 10, tzinfo=UTC))
    assert len(frame) == 21, f"2024-01-10부터 30일 중 21일: {len(frame)}"
    assert frame.index.min() == pd.Timestamp("2024-01-10")


# ------------------------------------------------------------------ 끊긴 호출
def test_a_dropped_call_is_retried():
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise UpbitTransientError("업비트와의 통신이 끊겼습니다: timed out")
        return FakeUpbit()(url)

    frame = quotes(flaky).candles("KRW-BTC", unit=1, count=2)
    assert not frame.empty
    assert calls["n"] == 3


def test_a_rate_limit_is_retried_then_given_up_on():
    def limited(url):
        return 429, {"error": {"name": "too_many_requests"}}

    with pytest.raises(UpbitError) as exc:
        quotes(limited).candles("KRW-BTC", unit=1)
    assert "한도" in str(exc.value)


# ------------------------------------------------------------------ 유의종목
def test_flagged_markets_are_surfaced():
    """업비트는 유의·주의 지정을 API로 알려준다. 주식에는 없던 장치다."""
    flagged = quotes(FakeUpbit()).flagged()
    assert "KRW-XYZ" in flagged
    assert "유의종목" in flagged["KRW-XYZ"]
    assert "KRW-BTC" not in flagged, "멀쩡한 종목까지 걸면 쓸모가 없다"


# ------------------------------------------------------------------ Provider
def test_provider_returns_backtestable_frames():
    data = UpbitProvider(quotes(FakeUpbit(days=100))).fetch(["KRW-BTC"], "2024-01-01")
    assert list(data.close.columns) == ["KRW-BTC"]
    assert len(data.close) == 100
    assert data.open is not None and data.volume is not None
