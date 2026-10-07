"""평가액 기록과, 그 기록에 매달려 있던 낙폭 중단 장치 검증.

기록이 없어 못 하던 일이 두 가지였다. 성과를 따질 수 없었고, 낙폭 중단 장치가
발동할 수 없었다. 두 번째가 더 나쁘다 — 설정과 README에 있는데 실제로는 없는
장치라, 있다고 믿고 안심하게 만든다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from stockbot.execution.equity_log import EquityLog
from stockbot.execution.guards import RiskGuard
from stockbot.execution.order import Account, Order, OrderType, Position, Side

DAY = dt.date(2026, 10, 1)


def log(tmp_path) -> EquityLog:
    return EquityLog(tmp_path / "equity.csv")


def test_a_day_is_recorded_and_read_back(tmp_path):
    book = log(tmp_path)
    book.append(DAY, 10_500_000, 500_000, {"069500": 120})

    rows = book.rows()
    assert len(rows) == 1
    assert rows[0]["date"] == DAY
    assert rows[0]["equity"] == 10_500_000
    assert rows[0]["invested"] == 10_000_000
    assert "069500:120" in rows[0]["holdings"]


def test_the_same_day_twice_does_not_become_two_rows(tmp_path):
    """하루에 두 번 돌면 그날이 두 줄이 된다. 수익률이 조용히 틀어진다."""
    book = log(tmp_path)
    book.append(DAY, 10_000_000, 0)
    book.append(DAY, 10_200_000, 0)

    rows = book.rows()
    assert len(rows) == 1, f"{DAY}가 두 줄이 됐습니다"
    assert rows[0]["equity"] == 10_200_000, "나중 값이 남아야 합니다"


def test_rows_come_back_in_date_order(tmp_path):
    book = log(tmp_path)
    for offset in (2, 0, 1):
        book.append(DAY + dt.timedelta(days=offset), 10_000_000 + offset, 0)
    assert [r["date"].day for r in book.rows()] == [1, 2, 3]


def test_a_broken_line_does_not_throw_away_the_rest(tmp_path):
    """몇 달치 기록이다. 한 줄 깨졌다고 전부 버리면 안 된다."""
    book = log(tmp_path)
    book.append(DAY, 10_000_000, 0)
    book.append(DAY + dt.timedelta(days=1), 10_100_000, 0)
    with book.path.open("a", encoding="utf-8") as fh:
        fh.write("깨진줄,,,,\n")

    assert len(book.rows()) == 2


def test_no_history_means_unknown_not_zero(tmp_path):
    """고점을 0으로 때우면 첫날부터 낙폭 100%로 읽힌다."""
    assert log(tmp_path).peak() is None


def test_peak_is_the_highest_ever_not_the_latest(tmp_path):
    book = log(tmp_path)
    book.append(DAY, 10_000_000, 0)
    book.append(DAY + dt.timedelta(days=1), 12_000_000, 0)
    book.append(DAY + dt.timedelta(days=2), 9_000_000, 0)
    assert book.peak() == 12_000_000


# ------------------------------------------------- 낙폭 중단 장치가 살아났는가
def account_at(value: float) -> tuple[Account, dict]:
    """평가액이 value가 되는 계좌와 시세. 절반은 현금으로 둔다.

    한 종목에 몰아 두면 비중 한도에 먼저 걸려서, 정작 보려는 낙폭 장치까지
    가지 못한다(실제로 그렇게 헛돌았다).
    """
    account = Account(cash=value / 2, positions={"069500": Position("069500", 100, 1.0)})
    return account, {"069500": value / 2 / 100}


def buy() -> Order:
    return Order("069500", Side.BUY, 1, order_type=OrderType.MARKET)


def drawdown_guard() -> RiskGuard:
    """낙폭 장치만 켠다. 다른 장치에 걸리면 무엇이 막았는지 알 수 없다."""
    return RiskGuard(max_drawdown_stop=0.25, max_position_weight=0.9)


def test_the_drawdown_stop_fires_once_a_peak_is_known():
    """고점 1,000만에서 -30%면 한도 25%를 넘는다. 신규 매수가 막혀야 한다."""
    guard = drawdown_guard()
    account, prices = account_at(7_000_000)

    report = guard.check([buy()], account, prices, peak_equity=10_000_000)
    assert not report.approved, "낙폭 한도를 넘었는데 매수가 통과했습니다"
    assert any("낙폭" in v.message for v in report.violations)


def test_the_drawdown_stop_is_silent_within_the_limit():
    guard = drawdown_guard()
    account, prices = account_at(9_000_000)

    report = guard.check([buy()], account, prices, peak_equity=10_000_000)
    assert report.approved, "낙폭 10%는 한도 안인데 막혔습니다"


def test_without_a_peak_the_stop_cannot_fire():
    """이것이 고치기 전의 상태다. 설정에 써 있어도 영원히 발동하지 않았다.

    peak_equity 를 넘기지 않으면 조건문이 통째로 건너뛰어진다. 이 테스트는
    그 동작을 못으로 박아두는 것이 아니라, **그래서 기록이 필요하다**는 것을
    남겨두기 위한 것이다.
    """
    guard = drawdown_guard()
    account, prices = account_at(1_000_000)      # 고점 대비 -90%라 해도

    report = guard.check([buy()], account, prices)   # 고점을 모르면
    assert report.approved, "고점을 모르는 채로 막으면 첫 거래부터 막힌다"


def test_the_pipeline_hands_the_guard_a_peak(tmp_path):
    """장치가 살아 있으려면 기록이 실제로 연결돼 있어야 한다.

    guards.py 가 아무리 맞아도 pipeline 이 고점을 안 넘기면 소용없다.
    실제로 그 상태였다.
    """
    from stockbot import pipeline

    class Cfg:
        class execution:
            state_path = str(tmp_path / "acct.json")

    assert pipeline.equity_peak(Cfg()) is None

    EquityLog(pipeline.equity_log_path(Cfg())).append(DAY, 11_000_000, 0)
    assert pipeline.equity_peak(Cfg()) == 11_000_000
