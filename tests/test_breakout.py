"""변동성 돌파 근사 백테스트 검증.

통계가 아니라 **계산이 맞는지**를 본다. 합성 데이터로 수익률이 얼마 나오는지는
데이터를 어떻게 만드느냐에 달려 있어 아무것도 증명하지 못한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockbot.backtest.costs import PRESETS, CostModel
from stockbot.intraday.breakout import (
    BreakoutParams,
    buy_and_hold,
    entry_levels,
    run_breakout,
)

FREE = CostModel(commission_bps=0.0, slippage_bps=0.0, sell_tax_bps=0.0)


def frame(rows: list[tuple]) -> pd.DataFrame:
    """(시가, 고가, 저가, 종가) 목록을 일봉표로."""
    idx = pd.bdate_range("2026-01-05", periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


# ------------------------------------------------------------------ 신호
def test_entry_level_uses_yesterday_not_today():
    """오늘 변동폭으로 오늘 판단하면 미래를 보는 것이다."""
    ohlc = frame([
        (100, 110, 90, 105),    # 변동폭 20
        (100, 101, 99, 100),    # 변동폭 2
        (100, 100, 100, 100),
    ])
    levels = entry_levels(ohlc, BreakoutParams(k=0.5))

    assert np.isnan(levels.iloc[0]), "첫날은 전일이 없으므로 판단할 수 없다"
    assert levels.iloc[1] == 100 + 0.5 * 20, "둘째 날은 첫날 변동폭(20)을 써야 한다"
    assert levels.iloc[2] == 100 + 0.5 * 2, "셋째 날은 둘째 날 변동폭(2)"


def test_larger_k_enters_less_often():
    """K는 문턱이다. 높일수록 덜 들어가고, 비용도 그만큼 준다."""
    rng = np.random.default_rng(7)
    n = 300
    base = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    # 하루를 랜덤워크로 그려 시가·고가·저가·종가가 서로 맞물리게 만든다.
    rows = []
    for price in base:
        path = price * np.exp(np.cumsum(rng.normal(0, 0.003, 20)))
        rows.append((path[0], path.max(), path.min(), path[-1]))
    ohlc = frame(rows)

    counts = [run_breakout(ohlc, BreakoutParams(k=k), FREE).n_trades for k in (0.2, 0.5, 1.0)]
    assert counts[0] > counts[1] > counts[2], f"K가 커지면 진입이 줄어야 합니다: {counts}"


# ------------------------------------------------------------------ 손익
def test_no_trade_when_the_high_never_reaches_the_level():
    ohlc = frame([
        (100, 110, 90, 100),        # 전일 변동폭 20 -> 기준가 110
        (100, 109, 95, 108),        # 고가 109 < 110 이므로 진입 없음
    ])
    result = run_breakout(ohlc, BreakoutParams(k=0.5), FREE)
    assert result.n_trades == 0
    assert result.equity.iloc[-1] == 10_000_000.0, "현금 그대로여야 합니다"


def test_profit_and_loss_matches_hand_calculation():
    """진입가 110에 사서 종가 121에 팔면 10%."""
    ohlc = frame([
        (100, 110, 90, 100),
        (100, 130, 95, 121),        # 기준가 110, 고가 130 -> 진입. 종가 121
    ])
    result = run_breakout(ohlc, BreakoutParams(k=0.5), FREE, initial_cash=1_000_000.0)

    assert result.n_trades == 1
    trade = result.trades.iloc[0]
    assert trade["entry"] == pytest.approx(110.0)
    assert trade["exit"] == pytest.approx(121.0)
    assert trade["net_return"] == pytest.approx(0.1)
    assert result.equity.iloc[-1] == pytest.approx(1_100_000.0)


def test_costs_are_charged_on_both_sides():
    """비용을 빼먹으면 잦은 매매가 공짜로 보인다. 그게 이 프로젝트의 핵심 위험이다."""
    ohlc = frame([
        (100, 110, 90, 100),
        (100, 130, 95, 121),
    ])
    free = run_breakout(ohlc, BreakoutParams(k=0.5), FREE)
    charged = run_breakout(ohlc, BreakoutParams(k=0.5), PRESETS["KR_ETF"])

    assert charged.trades.iloc[0]["net_return"] < free.trades.iloc[0]["net_return"]
    # 왕복 11bp(수수료 1.5 + 슬리피지 4.0, 양쪽)가 대략 빠져야 한다.
    gap = free.trades.iloc[0]["net_return"] - charged.trades.iloc[0]["net_return"]
    assert 0.0008 < gap < 0.0025, f"비용 차이가 이상합니다: {gap:.4%}"


def test_cash_rate_applies_only_on_days_without_a_trade():
    # 셋째 날 기준가는 둘째 날 변동폭으로 정해진다. 둘 다 안 걸리게 잡는다.
    ohlc = frame([
        (100, 110, 90, 100),        # 변동폭 20
        (100, 109, 95, 108),        # 기준가 110 > 고가 109 -> 진입 없음. 변동폭 14
        (100, 106, 99, 101),        # 기준가 107 > 고가 106 -> 진입 없음
    ])
    result = run_breakout(ohlc, BreakoutParams(k=0.5), FREE, cash_rate=0.03)
    assert result.n_trades == 0
    assert result.equity.iloc[-1] > 10_000_000.0, "쉬는 날에도 이자는 붙어야 합니다"


# ------------------------------------------------------------------ 보고 수치
def test_trades_per_year_is_reported_because_cost_scales_with_it():
    """연간 진입 횟수는 성과만큼 중요하다. 합격 기준에도 들어 있다."""
    ohlc = frame([(100, 110, 90, 100)] + [(100, 130, 95, 121)] * 251)
    result = run_breakout(ohlc, BreakoutParams(k=0.5), FREE)
    assert result.days == 251
    assert result.n_trades == 251
    assert result.trades_per_year == pytest.approx(252, abs=1)


def test_buy_and_hold_is_the_yardstick():
    """수고한 값어치가 있었는지 물으려면 그냥 들고 있는 쪽이 필요하다."""
    ohlc = frame([(100, 110, 90, 100), (100, 130, 95, 120)])
    held = buy_and_hold(ohlc, initial_cash=1_000_000.0)
    assert held.iloc[0] == pytest.approx(1_000_000.0)
    assert held.iloc[-1] == pytest.approx(1_200_000.0)


def test_too_little_data_says_how_much_is_needed():
    with pytest.raises(ValueError, match="최소"):
        run_breakout(frame([(100, 110, 90, 100)]), BreakoutParams(k=0.5), FREE)
