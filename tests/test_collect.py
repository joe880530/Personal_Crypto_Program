"""분봉 수집 명령 검증.

KIS 서버 없이 돌린다. 여기서 보는 것은 **한 건 실패했을 때 어떻게 되는가**다.
1년치 백필은 500번 넘게 호출하고 그중 몇 번은 반드시 끊긴다. 그때 전체가
멈춰도 안 되고, 조용히 성공으로 끝나도 안 된다.
"""

from __future__ import annotations

import argparse
import datetime as dt

import pandas as pd
import pytest

from stockbot import cli

CONFIG = """
start: "2024-01-01"
initial_capital: 10000000
assets:
  - {ticker: "069500", market: KR_ETF, currency: KRW, name: "KODEX 200"}
  - {ticker: "114260", market: KR_ETF, currency: KRW, name: "KODEX 국고채3년"}
strategy: {type: equal}
execution:
  broker: kis
"""

DAYS = [dt.date(2026, 9, 21), dt.date(2026, 9, 22), dt.date(2026, 9, 23)]


class FakeQuotes:
    """정해둔 날짜만 실패시키는 가짜 시세원."""

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.asked: list[tuple[str, dt.date]] = []

    def day_minutes(self, ticker, day):
        self.asked.append((ticker, day))
        if day in self.fail_on:
            raise TimeoutError("The read operation timed out")
        idx = pd.to_datetime([f"{day} 09:00", f"{day} 09:01"])
        return pd.DataFrame(
            {"open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
             "close": [1.0, 1.0], "volume": [1, 1]}, index=idx)


class FakeCalendar:
    def __init__(self, *a, **kw):
        pass

    def trading_days(self, start, end):
        return [d for d in DAYS if start <= d <= end]

    def phase(self, when):
        return "closed"


@pytest.fixture
def collect_env(tmp_path, monkeypatch):
    cfg = tmp_path / "portfolio.yaml"
    cfg.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(cli, "build_broker", lambda config: object())
    monkeypatch.setattr("stockbot.data.calendar.TradingCalendar", FakeCalendar)

    def run(quotes):
        monkeypatch.setattr("stockbot.data.kis_quotes.KISQuotes", lambda broker: quotes)
        args = argparse.Namespace(
            config=str(cfg), store=str(tmp_path / "minutes"), verbose=False,
            start="2026-09-21", end="2026-09-23", days=30,
        )
        return cli.cmd_collect(args)

    return run


def test_one_dropped_day_does_not_stop_the_rest(collect_env, capsys):
    """260일 백필이 하루 실패로 멈추면 다시 처음부터 받아야 한다."""
    quotes = FakeQuotes(fail_on=[DAYS[0]])
    assert collect_env(quotes) == 0

    out = capsys.readouterr().out
    assert "새로 받은 날 4일" in out, out          # 2종목 × 3일 중 실패 2건을 뺀 수
    assert "못 받은 날 2일" in out, out
    assert "다음 실행에서 다시 받습니다" in out


def test_dropped_days_are_retried_next_run(collect_env, capsys):
    """실패한 날을 '빈 날'로 적어버리면 영영 안 받는다. 비워둬야 다음에 받는다."""
    collect_env(FakeQuotes(fail_on=[DAYS[0]]))
    capsys.readouterr()

    again = FakeQuotes()
    assert collect_env(again) == 0
    assert DAYS[0] in {day for _, day in again.asked}, "실패한 날을 다시 물어야 합니다"
    assert DAYS[1] not in {day for _, day in again.asked}, "받아둔 날을 또 물으면 안 됩니다"
    assert "못 받은 날" not in capsys.readouterr().out


def test_getting_nothing_at_all_is_a_failure(collect_env, capsys):
    """매일 도는 작업에서 한 건도 못 받았으면 스케줄러가 알려줘야 한다.

    0을 돌려주면 DSM은 성공으로 보고 조용히 넘어간다. 그러면 수집이 몇 주째
    멈춰 있어도 모른다.
    """
    assert collect_env(FakeQuotes(fail_on=DAYS)) == 1
    assert "한 건도 받지 못했습니다" in capsys.readouterr().err


def test_nothing_to_do_is_not_a_failure(collect_env):
    """받을 날이 없으면 성공이다. 매일 도는 작업이 휴일마다 메일을 보내면 안 된다."""
    assert collect_env(FakeQuotes()) == 0
    assert collect_env(FakeQuotes()) == 0
