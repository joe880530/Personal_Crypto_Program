"""워크포워드 평가와 합격 판정 검증.

판정이 틀리면 떨어질 전략을 모의계좌에 올리게 된다. 기준 하나하나가
제대로 걸러내는지를 직접 만든 값으로 확인한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockbot.backtest.costs import PRESETS, CostModel
from stockbot.intraday.breakout import BreakoutParams
from stockbot.intraday.evaluate import (
    Criteria,
    Evaluation,
    Window,
    judge,
    walk_forward_breakout,
)

FREE = CostModel(commission_bps=0.0, slippage_bps=0.0, sell_tax_bps=0.0)


def synthetic(n: int, seed: int = 1, drift: float = 0.0) -> pd.DataFrame:
    """하루를 랜덤워크로 그려 시가·고가·저가·종가가 서로 맞물리게 만든다."""
    rng = np.random.default_rng(seed)
    base = 100 * np.exp(np.cumsum(rng.normal(drift, 0.011, n)))
    rows = []
    for price in base:
        path = price * np.exp(np.cumsum(rng.normal(0, 0.0035, 30)))
        rows.append((path[0], path.max(), path.min(), path[-1]))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                        index=pd.bdate_range("2018-01-01", periods=n))


def evaluation(**overrides) -> Evaluation:
    """판정만 시험하기 위한 최소 구성."""
    idx = pd.bdate_range("2020-01-01", periods=252)
    equity = pd.Series(np.linspace(1e7, 1.1e7, 252), index=idx)
    defaults = dict(
        windows=[], equity=equity,
        hold_equity=pd.Series(np.linspace(1e7, 1.05e7, 252), index=idx),
        n_candidates=3, total_trades=50, total_days=252,
    )
    defaults.update(overrides)
    return Evaluation(**defaults)


# ------------------------------------------------------------------ 구간 나누기
def test_selection_only_sees_the_training_window():
    """검증 구간을 보고 K를 고르면 워크포워드가 아니다.

    검증 구간을 통째로 망가뜨려도 고른 K가 그대로면, 선택이 그 구간을 보지
    않았다는 뜻이다.
    """
    ohlc = synthetic(900, seed=5)
    candidates = [BreakoutParams(k=k) for k in (0.3, 0.5, 0.8)]
    clean = walk_forward_breakout(ohlc, candidates, FREE, train=504, test=126)

    tampered = ohlc.copy()
    first_test_start = clean.windows[0].test_span[0]
    mask = tampered.index >= first_test_start
    tampered.loc[mask, ["open", "high", "low", "close"]] *= 3.0

    after = walk_forward_breakout(tampered, candidates, FREE, train=504, test=126)
    assert after.windows[0].selected == clean.windows[0].selected
    assert after.windows[0].train_sharpe == pytest.approx(clean.windows[0].train_sharpe)


def test_not_enough_data_says_what_to_change():
    with pytest.raises(ValueError, match="train/--test를 줄이세요"):
        walk_forward_breakout(synthetic(300), [BreakoutParams()], FREE, train=504, test=252)


def test_buy_and_hold_covers_the_same_span_as_the_strategy():
    """다른 기간끼리 비교하면 판정이 무의미해진다."""
    result = walk_forward_breakout(
        synthetic(1000, seed=9), [BreakoutParams(k=0.5)], FREE, train=504, test=252)
    assert result.hold_equity.index[0] == result.equity.index[0]
    assert result.hold_equity.index[-1] == result.equity.index[-1]


# ------------------------------------------------------------------ 과최적화 지표
def test_correlation_needs_at_least_three_windows():
    """두 점으로 그은 직선의 상관은 항상 ±1이다. 숫자가 나온다고 뜻이 있진 않다."""
    idx = pd.bdate_range("2020-01-01", periods=10)
    def window(train, test):
        return Window(train_span=(idx[0], idx[1]), test_span=(idx[2], idx[3]),
                      selected=BreakoutParams(), train_sharpe=train, test_sharpe=test,
                      test_returns=pd.Series(dtype=float), test_trades=0)

    assert np.isnan(evaluation(windows=[window(1.0, 0.5), window(0.2, 1.5)]).score_correlation())
    three = evaluation(windows=[window(1.0, 0.5), window(0.2, 1.5), window(0.6, 0.9)])
    assert np.isfinite(three.score_correlation())


def test_churn_is_not_reported_for_a_single_candidate():
    """후보가 하나면 '선택'이라는 행위가 없다. 0.00을 안정성으로 읽으면 거짓 안심이다."""
    result = walk_forward_breakout(
        synthetic(1100, seed=4), [BreakoutParams(k=0.5)], FREE, train=504, test=252)
    assert result.n_candidates == 1
    assert np.isnan(result.selection_churn())


# ------------------------------------------------------------------ 합격 판정
def test_each_criterion_can_fail_on_its_own():
    names = lambda checks: {c.name: c.passed for c in checks}

    # 샤프가 낮은 경우: 자산곡선을 들쭉날쭉하게 만든다.
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2020-01-01", periods=252)
    noisy = pd.Series(1e7 * np.cumprod(1 + rng.normal(0, 0.03, 252)), index=idx)
    assert names(judge(evaluation(equity=noisy)))["비용 차감 검증 샤프"] is False

    # 진입이 너무 잦은 경우
    assert names(judge(evaluation(total_trades=300)))["연간 진입 횟수"] is False

    # 낙폭이 깊은 경우
    deep = pd.Series(np.concatenate([np.linspace(1e7, 1e7, 100),
                                     np.linspace(1e7, 6e6, 152)]), index=idx)
    assert names(judge(evaluation(equity=deep)))["최대낙폭"] is False

    # buy&hold에 진 경우
    better_hold = pd.Series(np.linspace(1e7, 2e7, 252), index=idx)
    assert names(judge(evaluation(hold_equity=better_hold)))["buy&hold 대비 누적수익"] is False


def test_a_clean_result_passes_everything():
    idx = pd.bdate_range("2020-01-01", periods=252)
    steady = pd.Series(1e7 * np.cumprod(np.full(252, 1.0008)), index=idx)
    checks = judge(evaluation(
        equity=steady,
        hold_equity=pd.Series(np.linspace(1e7, 1.02e7, 252), index=idx),
        windows=[Window((idx[0], idx[1]), (idx[2], idx[3]), BreakoutParams(),
                        t, v, pd.Series(dtype=float), 0)
                 for t, v in [(1.0, 0.9), (0.8, 0.7), (1.2, 1.1)]],
    ))
    assert all(c.passed for c in checks), [c.name for c in checks if not c.passed]


def test_criteria_are_not_silently_relaxed():
    """기준값은 코드에 박아 둔다. 결과를 보고 고치면 판정이 아니라 변명이 된다."""
    default = Criteria()
    assert (default.min_test_sharpe, default.min_score_correlation) == (0.5, 0.0)
    assert (default.max_trades_per_year, default.max_drawdown) == (150.0, 0.25)
    assert default.must_beat_buy_and_hold is True


def test_costs_change_the_verdict():
    """비용을 빼먹으면 잦은 매매가 공짜로 보인다. 판정이 뒤집히는지 확인한다."""
    ohlc = synthetic(1300, seed=11, drift=0.0003)
    candidates = [BreakoutParams(k=k) for k in (0.2, 0.5)]
    free = walk_forward_breakout(ohlc, candidates, FREE, train=504, test=252)
    charged = walk_forward_breakout(ohlc, candidates, PRESETS["KR_ETF"], train=504, test=252)
    assert charged.summary()["total_return"] < free.summary()["total_return"]
