import numpy as np
import pandas as pd
import pytest

from stockbot.portfolio import (
    CASH,
    DualMomentum,
    EqualWeight,
    FixedWeight,
    MomentumRiskParity,
    RiskParity,
    apply_bounds,
    equal_risk_contribution_weights,
    inverse_volatility_weights,
    risk_contributions,
)


def _cov(values):
    idx = [f"A{i}" for i in range(len(values))]
    return pd.DataFrame(values, index=idx, columns=idx)


# ------------------------------------------------------------------ 리스크 패리티
def test_erc_equalizes_risk_contributions():
    cov = _cov([[0.04, 0.006, 0.0], [0.006, 0.01, 0.0], [0.0, 0.0, 0.0025]])
    w = equal_risk_contribution_weights(cov)
    rc = risk_contributions(w, cov)
    shares = rc / rc.sum()
    assert shares.max() - shares.min() < 1e-6
    assert w.sum() == pytest.approx(1.0)
    assert (w > 0).all()


def test_erc_respects_custom_risk_budgets():
    cov = _cov([[0.04, 0.0], [0.0, 0.01]])
    budgets = pd.Series({"A0": 0.7, "A1": 0.3})
    w = equal_risk_contribution_weights(cov, budgets)
    rc = risk_contributions(w, cov)
    shares = rc / rc.sum()
    assert shares["A0"] == pytest.approx(0.7, abs=1e-6)


def test_erc_equals_inverse_vol_when_uncorrelated_and_equal_budget():
    """상관관계가 0이면 ERC는 역변동성 해와 일치한다."""
    cov = _cov([[0.04, 0.0, 0.0], [0.0, 0.01, 0.0], [0.0, 0.0, 0.0025]])
    erc = equal_risk_contribution_weights(cov)
    inv = inverse_volatility_weights(cov)
    pd.testing.assert_series_equal(erc, inv, atol=1e-8, check_names=False)


def test_inverse_vol_gives_more_weight_to_calmer_asset():
    cov = _cov([[0.04, 0.0], [0.0, 0.01]])
    w = inverse_volatility_weights(cov)
    assert w["A1"] > w["A0"]


def test_risk_parity_strategy_produces_valid_weights(prices):
    w = RiskParity(lookback=120).target_weights(prices)
    assert w.sum() == pytest.approx(1.0, abs=1e-9)
    assert (w >= 0).all()
    # 저변동 자산이 고변동 자산보다 큰 비중을 받아야 한다
    assert w["FLAT"] > w["GROW"]


def test_risk_parity_target_volatility_scales_down(prices):
    full = RiskParity(lookback=120).target_weights(prices)
    capped = RiskParity(lookback=120, target_volatility=0.01).target_weights(prices)
    assert capped.sum() < full.sum()


def test_max_weight_bound_is_enforced(prices):
    w = RiskParity(lookback=120, max_weight=0.30).target_weights(prices)
    assert w.max() <= 0.30 + 1e-9


def test_apply_bounds_redistributes_excess():
    w = pd.Series({"A": 0.8, "B": 0.1, "C": 0.1})
    out = apply_bounds(w, max_weight=0.4)
    assert out.max() <= 0.4 + 1e-9
    assert out.sum() == pytest.approx(1.0)


# ------------------------------------------------------------------ 모멘텀
def test_dual_momentum_picks_strongest_assets(prices):
    """상승 추세 자산이 선택되고 하락 자산은 제외돼야 한다."""
    w = DualMomentum(top_n=2, safe_asset="STEADY").target_weights(prices)
    assert w["GROW"] > 0
    # 후반부 하락 추세인 CYCLE은 선택되지 않아야 한다
    assert w["CYCLE"] == pytest.approx(0.0)


def test_absolute_momentum_parks_everything_when_all_fall():
    """전 종목이 하락하면 안전자산으로 전량 대피해야 한다."""
    idx = pd.bdate_range("2020-01-01", periods=300)
    falling = pd.DataFrame(
        {
            "A": np.linspace(100, 50, 300),
            "B": np.linspace(100, 60, 300),
            "SAFE": np.linspace(100, 101, 300),
        },
        index=idx,
    )
    w = DualMomentum(lookbacks=[60, 120], top_n=2, safe_asset="SAFE").target_weights(falling)
    assert w["SAFE"] == pytest.approx(1.0)
    assert w["A"] == pytest.approx(0.0)


def test_without_safe_asset_failed_momentum_goes_to_cash():
    idx = pd.bdate_range("2020-01-01", periods=300)
    falling = pd.DataFrame({"A": np.linspace(100, 50, 300), "B": np.linspace(100, 60, 300)}, index=idx)
    w = DualMomentum(lookbacks=[60, 120], top_n=2).target_weights(falling)
    assert w[CASH] == pytest.approx(1.0)


def test_momentum_risk_parity_preserves_safe_asset_allocation():
    """모멘텀이 대피시킨 몫은 리스크 패리티가 다시 배분하면 안 된다."""
    idx = pd.bdate_range("2020-01-01", periods=400)
    px = pd.DataFrame(
        {
            "WIN": np.linspace(100, 200, 400),
            "LOSE": np.linspace(100, 60, 400),
            "SAFE": np.linspace(100, 103, 400),
        },
        index=idx,
    )
    mom = DualMomentum(lookbacks=[60, 120], top_n=2, safe_asset="SAFE")
    combo = MomentumRiskParity(mom, RiskParity(lookback=100))
    w = combo.target_weights(px)
    # top_n=2 중 1개만 통과 -> 위험자산 50%, 대피 50%
    assert w["WIN"] == pytest.approx(0.5, abs=1e-6)
    assert w["SAFE"] == pytest.approx(0.5, abs=1e-6)


# ------------------------------------------------------------------ 고정/균등
def test_fixed_weight_rejects_oversized_allocation():
    with pytest.raises(ValueError, match="비중 합"):
        FixedWeight({"A": 0.7, "B": 0.5})


def test_fixed_weight_rejects_negative():
    with pytest.raises(ValueError, match="음수"):
        FixedWeight({"A": -0.1, "B": 0.5})


def test_fixed_weight_leftover_becomes_cash(prices):
    w = FixedWeight({"GROW": 0.6}).target_weights(prices)
    assert w["GROW"] == pytest.approx(0.6)
    assert w[CASH] == pytest.approx(0.4)


def test_fixed_weight_missing_ticker_falls_back_to_cash(prices):
    w = FixedWeight({"GROW": 0.5, "NOT_LISTED": 0.5}).target_weights(prices)
    assert w[CASH] == pytest.approx(0.5)


def test_equal_weight_splits_evenly(prices):
    w = EqualWeight().target_weights(prices)
    assert w.drop(index=[CASH]).max() == pytest.approx(0.25)


def test_equal_weight_skips_not_yet_listed(prices):
    px = prices.copy()
    px["NEW"] = np.nan  # 아직 상장 전
    w = EqualWeight().target_weights(px)
    assert w["NEW"] == pytest.approx(0.0)
    assert w["GROW"] == pytest.approx(0.25)
