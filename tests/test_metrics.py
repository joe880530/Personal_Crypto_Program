import numpy as np
import pandas as pd
import pytest

from stockbot import metrics


def test_cagr_matches_known_growth():
    """2년간 정확히 2배가 되면 CAGR은 sqrt(2)-1이어야 한다."""
    n = metrics.TRADING_DAYS * 2 + 1
    equity = pd.Series(np.linspace(1.0, 2.0, n) ** 1.0)
    equity = pd.Series(np.geomspace(1.0, 2.0, n))
    assert metrics.cagr(equity) == pytest.approx(2 ** 0.5 - 1, rel=1e-6)


def test_max_drawdown_finds_worst_peak_to_trough():
    equity = pd.Series([100, 120, 60, 80, 150])
    assert metrics.max_drawdown(equity) == pytest.approx(-0.5)


def test_max_drawdown_is_zero_for_monotonic_growth():
    assert metrics.max_drawdown(pd.Series([1, 2, 3, 4])) == pytest.approx(0.0)


def test_drawdown_duration_counts_longest_underwater_stretch():
    equity = pd.Series([100, 90, 95, 105, 100, 99, 98, 110])
    # 인덱스 1~2 (2봉), 인덱스 4~6 (3봉)
    assert metrics.max_drawdown_duration(equity) == 3


def test_sharpe_zero_volatility_returns_nan():
    equity = pd.Series([100.0] * 50)
    assert np.isnan(metrics.sharpe(metrics.to_returns(equity)))


def test_sortino_ignores_upside_volatility():
    """상승 변동성만 큰 시계열은 소르티노가 샤프보다 높아야 한다."""
    rets = pd.Series([0.05, 0.05, -0.001, 0.05, -0.001] * 20)
    assert metrics.sortino(rets) > metrics.sharpe(rets)


def test_sortino_beats_sharpe_by_root_two_on_symmetric_returns():
    """대칭 분포에서 소르티노는 샤프의 약 √2배여야 한다.

    하방 편차의 분모를 '음수인 날의 개수'로 두면 대칭 분포에서 값이
    표준편차와 같아져 소르티노가 샤프와 구별되지 않는다. 표준 정의는
    전체 관측 수로 나눈다. 그 차이를 여기서 고정한다.
    """
    rets = pd.Series(np.random.RandomState(0).normal(0.0005, 0.011, 20_000))
    ratio = metrics.sortino(rets) / metrics.sharpe(rets)
    assert ratio == pytest.approx(2 ** 0.5, rel=0.08)


def test_downside_deviation_ignores_gains_entirely():
    """목표를 넘는 움직임은 하방 편차에 전혀 들어가지 않는다."""
    calm = pd.Series([-0.01, 0.001] * 100)
    explosive_upside = pd.Series([-0.01, 0.50] * 100)
    assert metrics.downside_deviation(calm) == pytest.approx(
        metrics.downside_deviation(explosive_upside)
    )


def test_downside_deviation_is_zero_without_losses():
    assert metrics.downside_deviation(pd.Series([0.01, 0.02, 0.03])) == pytest.approx(0.0)


def test_sortino_penalises_downside_heavy_returns():
    """하락이 몰린 분포는 소르티노가 샤프보다 나빠야 한다."""
    rs = np.random.RandomState(1)
    rets = pd.Series(
        np.where(rs.rand(20_000) < 0.1, rs.normal(-0.05, 0.01, 20_000), rs.normal(0.004, 0.003, 20_000))
    )
    assert metrics.sortino(rets) < metrics.sharpe(rets)


def test_summary_has_all_keys():
    equity = pd.Series(np.geomspace(100, 150, 500))
    out = metrics.summary(equity)
    for key in ("cagr", "volatility", "sharpe", "max_drawdown", "calmar"):
        assert key in out


def test_empty_input_does_not_crash():
    assert np.isnan(metrics.cagr(pd.Series(dtype=float)))
    assert np.isnan(metrics.max_drawdown(pd.Series(dtype=float)))
