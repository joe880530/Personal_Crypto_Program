"""장이 닫혀 있을 때 주문을 내지 않는지 검증.

무인으로 도는 작업에서 가장 위험한 것은 실패가 아니라 **결과를 모르는 것**이다.
닫힌 장에 들어간 주문이 거부될지 다음 장으로 넘어갈지 확정적으로 알 수 없으므로
아예 내지 않는다. 한 달을 거르는 쪽이 낫다 — 비중 이탈은 다음 달에 밴드가
바로잡지만, 나간 주문은 되돌릴 방법이 없다.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from stockbot import cli


class FakeCalendar:
    def __init__(self, phase: str, knows: bool):
        self._phase = phase
        self._knows = knows

    def phase(self, now):
        return self._phase

    def knows_holidays(self):
        return self._knows


class FakeQuotes:
    def __init__(self, last_trade: dt.date | None = None, blow_up: bool = False):
        self.last_trade = last_trade
        self.blow_up = blow_up
        self.asked = 0

    def today_minutes(self, ticker):
        self.asked += 1
        if self.blow_up:
            raise RuntimeError("KIS 서버에 접속하지 못했습니다")
        if self.last_trade is None:
            return pd.DataFrame()
        stamp = pd.Timestamp(dt.datetime.combine(self.last_trade, dt.time(9, 1)))
        return pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex([stamp]))


class Config:
    class execution:
        broker = "kis"
        state_path = ".state/paper.json"

    class asset:
        ticker = "069500"

    assets = [asset]


@pytest.fixture
def check(monkeypatch):
    def run(phase, knows, quotes=None):
        monkeypatch.setattr("stockbot.data.kis_quotes.KISQuotes",
                            lambda broker: quotes or FakeQuotes())
        monkeypatch.setattr("stockbot.data.calendar.TradingCalendar",
                            lambda q, cache_path=None: FakeCalendar(phase, knows))
        return cli._closed_reason(Config(), object())

    return run


@pytest.mark.parametrize("phase, expect", [
    ("holiday", "휴장일"),
    ("before_open", "개장 전"),
    ("after_close", "마감"),
])
def test_a_closed_market_stops_the_order(check, phase, expect):
    reason = check(phase, knows=True)
    assert reason and expect in reason


def test_an_open_market_lets_the_order_through(check):
    assert check("open", knows=True) is None


def test_a_holiday_the_calendar_cannot_see_is_caught_by_the_tape(check):
    """모의투자는 휴장일 TR을 못 써서 달력이 주말만 거른다.

    그 상태에서 삼일절 대체공휴일은 평일=개장으로 보인다. 체결이 어제 것이면
    오늘 장이 서지 않은 것이다.
    """
    yesterday = dt.date.today() - dt.timedelta(days=1)
    reason = check("open", knows=False, quotes=FakeQuotes(last_trade=yesterday))
    assert reason and str(yesterday) in reason


def test_todays_tape_lets_the_order_through(check):
    assert check("open", knows=False, quotes=FakeQuotes(last_trade=dt.date.today())) is None


def test_the_calendar_is_trusted_when_it_knows_holidays(check):
    """공휴일까지 아는 달력이 '열렸다'면 더 물어볼 이유가 없다. 호출 낭비다."""
    quotes = FakeQuotes(last_trade=dt.date.today() - dt.timedelta(days=1))
    assert check("open", knows=True, quotes=quotes) is None
    assert quotes.asked == 0


@pytest.mark.parametrize("quotes", [
    FakeQuotes(blow_up=True),        # 조회가 실패했다
    FakeQuotes(last_trade=None),     # 빈 응답이 왔다
])
def test_what_we_could_not_check_is_not_treated_as_closed(check, quotes):
    """확인하지 못한 것을 '닫혔다'로 읽으면, 멀쩡히 열린 날 리밸런싱이 걸러진다.

    막는 것도 비용이다. 증거가 있을 때만 막는다.
    """
    assert check("open", knows=False, quotes=quotes) is None


def test_a_local_paper_broker_is_not_gated():
    """로컬 모의 브로커는 장 시간과 무관하다. 테스트와 연습을 막으면 안 된다."""
    class Local(Config):
        class execution:
            broker = "paper"
            state_path = ".state/paper.json"

    assert cli._closed_reason(Local(), object()) is None
