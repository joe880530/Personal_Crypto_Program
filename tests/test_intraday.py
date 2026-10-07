"""분봉 조회와 거래일 달력 검증.

실제 KIS 서버는 키가 있어야 하므로 가짜 전송 계층으로 **요청을 제대로 만드는지,
페이징을 제대로 되짚는지, 응답 형식이 바뀌면 드러나는지**를 본다.
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from stockbot.data.calendar import TradingCalendar
from stockbot.data.kis_quotes import (
    DAILY_CHART_PAGE,
    DAILY_CHART_TR,
    HOLIDAY_TR,
    TODAY_CHART_TR,
    KISQuotes,
    QuoteError,
)
from stockbot.execution.kis import KISBroker, KISCredentials

CREDS = KISCredentials(app_key="K", app_secret="S", account="12345678", paper=True)
TOKEN_OK = (200, {"access_token": "T", "expires_in": 86400})


def bar(stamp: dt.datetime, price: float) -> dict:
    return {
        "stck_bsop_date": stamp.strftime("%Y%m%d"),
        "stck_cntg_hour": stamp.strftime("%H%M%S"),
        "stck_oprc": f"{price:.0f}",
        "stck_hgpr": f"{price + 10:.0f}",
        "stck_lwpr": f"{price - 10:.0f}",
        "stck_prpr": f"{price + 5:.0f}",
        "cntg_vol": "100",
    }


class FakeMarket:
    """하루치 1분봉을 들고, KIS처럼 '기준시각 이전 120건'만 돌려준다."""

    def __init__(self, day: dt.date, holidays: dict | None = None):
        self.day = day
        self.bars = [
            dt.datetime.combine(day, dt.time(9, 0)) + dt.timedelta(minutes=i)
            for i in range(391)   # 09:00 ~ 15:30
        ]
        self.holidays = holidays or {}
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, params, body):
        self.calls.append({"url": url, "tr": headers.get("tr_id"), "params": params})
        if "tokenP" in url:
            return TOKEN_OK
        if "chk-holiday" in url:
            return 200, {"rt_cd": "0", "output": [
                {"bass_dt": d.strftime("%Y%m%d"), "opnd_yn": "Y" if open_ else "N"}
                for d, open_ in sorted(self.holidays.items())
            ]}

        anchor = dt.datetime.strptime(params["FID_INPUT_HOUR_1"], "%H%M%S").time()
        upto = [b for b in self.bars if b.time() <= anchor]
        limit = DAILY_CHART_PAGE if "dailychartprice" in url else 30
        page = upto[-limit:]
        # KIS는 최신순으로 돌려준다.
        return 200, {"rt_cd": "0", "output1": {}, "output2": [bar(b, 1000.0) for b in reversed(page)]}


def make(tmp_path, market):
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"), transport=market)
    return KISQuotes(broker, pace=0, sleep=lambda _: None)


# ------------------------------------------------------------------ 분봉
def test_today_minutes_uses_the_documented_tr_and_parses_ohlcv(tmp_path):
    market = FakeMarket(dt.date(2026, 9, 29))
    frame = make(tmp_path, market).today_minutes("069500")

    chart = [c for c in market.calls if "itemchartprice" in c["url"]][0]
    assert chart["tr"] == TODAY_CHART_TR
    assert chart["params"]["FID_INPUT_ISCD"] == "069500"
    assert chart["params"]["FID_COND_MRKT_DIV_CODE"] == "J"

    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame.index.is_monotonic_increasing, "시간순으로 정렬돼야 합니다"
    assert len(frame) == 30, "당일 분봉은 1회 30건이 한도입니다"


def test_day_minutes_pages_backwards_until_the_open(tmp_path):
    """1회 120건이라 되짚어 모아야 하루가 완성된다."""
    day = dt.date(2026, 9, 29)
    market = FakeMarket(day)
    frame = make(tmp_path, market).day_minutes("069500", day)

    assert len(frame) == 391, f"하루치가 다 모이지 않았습니다: {len(frame)}건"
    assert frame.index.min().time() == dt.time(9, 0)
    assert frame.index.max().time() == dt.time(15, 30)
    assert not frame.index.has_duplicates

    chart_calls = [c for c in market.calls if "dailychartprice" in c["url"]]
    assert chart_calls[0]["tr"] == DAILY_CHART_TR
    assert len(chart_calls) == 4, f"120건씩이면 4번이면 충분합니다: {len(chart_calls)}번"


def test_day_minutes_stops_when_the_same_page_comes_back(tmp_path):
    """더 과거가 없으면 API가 같은 구간을 되돌려준다. 그대로 두면 무한루프다."""
    day = dt.date(2026, 9, 29)

    class Stuck(FakeMarket):
        def __call__(self, method, url, headers, params, body):
            if "dailychartprice" in url:
                self.calls.append({"url": url, "tr": headers.get("tr_id"), "params": params})
                same = self.bars[:5]
                return 200, {"rt_cd": "0", "output2": [bar(b, 1000.0) for b in reversed(same)]}
            return super().__call__(method, url, headers, params, body)

    market = Stuck(day)
    frame = make(tmp_path, market).day_minutes("069500", day)
    assert len(frame) == 5
    assert len([c for c in market.calls if "dailychartprice" in c["url"]]) <= 2


def test_a_holiday_returns_nothing_rather_than_another_days_bars(tmp_path):
    """휴장일을 물으면 KIS는 빈 응답이 아니라 **직전 거래일 분봉**을 돌려준다.

    그대로 믿으면 두 가지가 한꺼번에 어긋난다. 그날 것을 받았다고 여겨 '빈 날'
    기록을 안 남기니 매번 다시 묻게 되고, 저장되는 값은 엉뚱한 날짜의 것이 된다.
    실제로 069500이 "38일 받음"이라고 하고 18일만 저장됐다(나머지 20일 휴장).
    """
    holiday = dt.date(2026, 3, 2)          # 삼일절(일요일)의 대체공휴일
    previous = dt.date(2026, 2, 27)        # KIS가 대신 돌려주는 직전 거래일

    class Substituting(FakeMarket):
        """무엇을 묻든 직전 거래일 분봉을 돌려준다."""

        def __call__(self, method, url, headers, params, body):
            if "dailychartprice" not in url:
                return super().__call__(method, url, headers, params, body)
            self.calls.append({"url": url, "tr": headers.get("tr_id"), "params": params})
            bars = [dt.datetime.combine(previous, dt.time(9, 0)) + dt.timedelta(minutes=i)
                    for i in range(391)]
            anchor = dt.datetime.strptime(params["FID_INPUT_HOUR_1"], "%H%M%S").time()
            page = [b for b in bars if b.time() <= anchor][-DAILY_CHART_PAGE:]
            return 200, {"rt_cd": "0", "output2": [bar(b, 1000.0) for b in reversed(page)]}

    market = Substituting(holiday)
    frame = make(tmp_path, market).day_minutes("069500", holiday)

    assert frame.empty, f"휴장일인데 {len(frame)}건을 그날 것으로 받았습니다"
    assert len([c for c in market.calls if "dailychartprice" in c["url"]]) == 1, (
        "그날 것이 아님을 알았으면 더 되짚을 이유가 없습니다"
    )


def test_only_the_asked_day_is_kept(tmp_path):
    """응답에 다른 날짜가 섞여 와도 물어본 날 것만 남아야 한다."""
    day = dt.date(2026, 9, 29)

    class Mixed(FakeMarket):
        def __call__(self, method, url, headers, params, body):
            if "dailychartprice" not in url:
                return super().__call__(method, url, headers, params, body)
            self.calls.append({"url": url, "tr": headers.get("tr_id"), "params": params})
            mine = dt.datetime.combine(day, dt.time(15, 30))
            other = dt.datetime.combine(day - dt.timedelta(days=1), dt.time(15, 30))
            return 200, {"rt_cd": "0", "output2": [bar(mine, 1000.0), bar(other, 2000.0)]}

    frame = make(tmp_path, Mixed(day)).day_minutes("069500", day)
    assert len(frame) == 1
    assert set(frame.index.date) == {day}


def test_missing_fields_fail_loudly(tmp_path):
    """형식이 바뀌면 0으로 때우지 말고 무엇이 왔는지 보여주며 멈춰야 한다."""
    day = dt.date(2026, 9, 29)

    class Renamed(FakeMarket):
        def __call__(self, method, url, headers, params, body):
            if "chartprice" in url:
                return 200, {"rt_cd": "0", "output2": [{"date": "20260929", "price": "1000"}]}
            return super().__call__(method, url, headers, params, body)

    with pytest.raises(QuoteError) as exc:
        make(tmp_path, Renamed(day)).today_minutes("069500")
    assert "stck_bsop_date" in str(exc.value)
    assert "받은 필드" in str(exc.value)


# ------------------------------------------------------------------ 사전 확인
def test_probe_reports_failure_instead_of_raising(tmp_path):
    """모의계좌가 과거 분봉을 막고 있을 수 있다. 그때 사유를 읽을 수 있어야 한다."""
    day = dt.date(2026, 9, 29)

    class Denied(FakeMarket):
        def __call__(self, method, url, headers, params, body):
            if "dailychartprice" in url:
                return 200, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "모의투자 미지원"}
            return super().__call__(method, url, headers, params, body)

    result = make(tmp_path, Denied(day)).probe_history("069500", day)
    assert result["ok"] is False
    assert "모의투자 미지원" in result["reason"]


def test_probe_treats_empty_success_as_failure(tmp_path):
    """rt_cd가 0인데 분봉이 0건이면 '된다'고 말하면 안 된다."""
    day = dt.date(2026, 9, 29)

    class Empty(FakeMarket):
        def __call__(self, method, url, headers, params, body):
            if "dailychartprice" in url:
                return 200, {"rt_cd": "0", "output2": []}
            return super().__call__(method, url, headers, params, body)

    result = make(tmp_path, Empty(day)).probe_history("069500", day)
    assert result["ok"] is False
    assert result["bars"] == 0


def test_probe_reports_the_range_it_got(tmp_path):
    day = dt.date(2026, 9, 29)
    result = make(tmp_path, FakeMarket(day)).probe_history("069500", day)
    assert result["ok"] is True
    assert result["bars"] == DAILY_CHART_PAGE
    assert result["last"].time() == dt.time(15, 30)


# ------------------------------------------------------------------ 달력
def test_weekend_needs_no_api_call(tmp_path):
    """주말은 물어볼 것도 없다. 휴장일 조회는 1일 1회 권고라 아껴야 한다."""
    market = FakeMarket(dt.date(2026, 9, 29))
    calendar = TradingCalendar(make(tmp_path, market), str(tmp_path / "days.json"))
    assert calendar.is_trading_day(dt.date(2026, 9, 27)) is False
    assert [c for c in market.calls if "chk-holiday" in c["url"]] == []


def test_holidays_are_fetched_once_and_cached_to_disk(tmp_path):
    day = dt.date(2026, 9, 29)
    market = FakeMarket(day, holidays={day: True, dt.date(2026, 9, 30): False})
    cache = tmp_path / "days.json"

    calendar = TradingCalendar(make(tmp_path, market), str(cache))
    assert calendar.is_trading_day(day) is True
    assert calendar.is_trading_day(dt.date(2026, 9, 30)) is False
    assert len([c for c in market.calls if "chk-holiday" in c["url"]]) == 1

    # 새 인스턴스가 파일만 읽고 다시 묻지 않아야 한다.
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["2026-09-30"] is False
    again = TradingCalendar(make(tmp_path, market), str(cache))
    assert again.is_trading_day(dt.date(2026, 9, 30)) is False


def test_phase_marks_the_session_boundaries(tmp_path):
    market = FakeMarket(dt.date(2026, 9, 29), holidays={dt.date(2026, 9, 29): True})
    calendar = TradingCalendar(make(tmp_path, market), str(tmp_path / "days.json"))
    at = lambda h, m: calendar.phase(dt.datetime(2026, 9, 29, h, m))
    assert at(8, 59) == "before_open"
    assert at(9, 0) == "open"
    assert at(15, 30) == "open"
    assert at(15, 31) == "after_close"
    assert calendar.phase(dt.datetime(2026, 9, 27, 10, 0)) == "holiday"


def test_previous_trading_day_skips_holidays(tmp_path):
    """전일 변동폭을 쓰는 전략이 휴장일을 '전일'로 잡으면 안 된다."""
    market = FakeMarket(dt.date(2026, 10, 5), holidays={
        dt.date(2026, 10, 2): False,   # 임시공휴일이라고 치자
        dt.date(2026, 10, 1): True,
    })
    calendar = TradingCalendar(make(tmp_path, market), str(tmp_path / "days.json"))
    # 10/5(월) 직전: 10/4 일, 10/3 토, 10/2 휴장 -> 10/1
    assert calendar.previous_trading_day(dt.date(2026, 10, 5)) == dt.date(2026, 10, 1)


def test_calendar_without_quotes_admits_it_does_not_know_holidays(tmp_path):
    calendar = TradingCalendar(None, str(tmp_path / "days.json"))
    assert calendar.knows_holidays() is False
    assert calendar.is_trading_day(dt.date(2026, 9, 29)) is True


def test_calendar_survives_a_denied_holiday_api(tmp_path):
    """모의계좌는 휴장일 TR을 지원하지 않는다("모의투자 TR 이 아닙니다").

    곁다리 기능이 예외를 올려보내면 정작 하려던 일(분봉 확인, 주문)이 통째로
    막힌다. 실제로 그렇게 막혔다. 주말만 거르는 쪽으로 물러서되, 공휴일을
    모른다는 사실은 밝혀야 한다.
    """
    day = dt.date(2026, 9, 29)

    class NoHolidayTR(FakeMarket):
        def __call__(self, method, url, headers, params, body):
            if "chk-holiday" in url:
                self.calls.append({"url": url, "tr": headers.get("tr_id"), "params": params})
                return 500, {"rt_cd": "1", "msg1": "모의투자 TR 이 아닙니다."}
            return super().__call__(method, url, headers, params, body)

    market = NoHolidayTR(day)
    calendar = TradingCalendar(make(tmp_path, market), str(tmp_path / "days.json"))

    assert calendar.is_trading_day(day) is True      # 평일이므로 열린 것으로 본다
    assert calendar.knows_holidays() is False
    assert "모의투자 TR" in (calendar.holiday_error or "")

    # 거부당한 뒤로는 다시 묻지 않는다. 매번 500을 맞을 이유가 없다.
    calendar.is_trading_day(dt.date(2026, 9, 30))
    calendar.is_trading_day(dt.date(2026, 10, 1))
    assert len([c for c in market.calls if "chk-holiday" in c["url"]]) == 1

    # 주말 판단과 국면 계산은 그대로 살아 있어야 한다.
    assert calendar.is_trading_day(dt.date(2026, 9, 27)) is False
    assert calendar.phase(dt.datetime(2026, 9, 29, 10, 0)) == "open"


def test_probe_runs_even_when_holidays_are_unavailable(tmp_path):
    """알고 싶은 것은 분봉이다. 달력이 안 된다고 그것까지 막히면 안 된다."""
    day = dt.date(2026, 9, 29)

    class NoHolidayTR(FakeMarket):
        def __call__(self, method, url, headers, params, body):
            if "chk-holiday" in url:
                return 500, {"rt_cd": "1", "msg1": "모의투자 TR 이 아닙니다."}
            return super().__call__(method, url, headers, params, body)

    quotes = make(tmp_path, NoHolidayTR(day))
    calendar = TradingCalendar(quotes, str(tmp_path / "days.json"))
    assert calendar.previous_trading_day(dt.date(2026, 9, 30)) == day
    assert quotes.probe_history("069500", day)["ok"] is True
