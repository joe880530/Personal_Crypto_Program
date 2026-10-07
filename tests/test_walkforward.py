"""워크포워드 검증.

이 도구의 존재 이유는 "전체 기간 백테스트 성과를 믿어도 되는가"에 답하는
것이다. 그러니 도구 자체가 미래를 참조하면 아무 의미가 없다. 그 부분을
가장 강하게 검사한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockbot.backtest import PRESETS, CostBook
from stockbot.portfolio import DualMomentum, EqualWeight, FixedWeight, RiskParity
from stockbot.validation import make_windows, walk_forward
from stockbot.validation.splits import describe_coverage

from .conftest import make_prices


def _zero_cost() -> CostBook:
    return CostBook(PRESETS["ZERO"])


# ------------------------------------------------------------------ 구간 분할
def test_test_windows_follow_train_and_do_not_overlap():
    windows = make_windows(1000, train=500, test=100)
    for window in windows:
        assert window.test_start == window.train_end  # 검증은 학습 바로 뒤
        assert window.train_length == 500
        assert window.test_length == 100
    for prev, cur in zip(windows, windows[1:]):
        assert prev.test_end <= cur.test_start  # 검증 구간이 겹치지 않는다


def test_rolling_mode_moves_train_start():
    windows = make_windows(1000, train=400, test=100, mode="rolling")
    assert windows[0].train_start == 0
    assert windows[-1].train_start > 0


def test_anchored_mode_keeps_start_and_grows_train():
    windows = make_windows(1000, train=400, test=100, mode="anchored")
    assert all(w.train_start == 0 for w in windows)
    assert windows[-1].train_length > windows[0].train_length


def test_no_windows_when_data_too_short():
    assert make_windows(100, train=500, test=100) == []


def test_step_controls_window_spacing():
    sparse = make_windows(2000, train=500, test=100, step=400)
    dense = make_windows(2000, train=500, test=100, step=100)
    assert len(dense) > len(sparse)


def test_invalid_split_arguments_rejected():
    with pytest.raises(ValueError, match="mode"):
        make_windows(1000, train=100, test=10, mode="sliding")
    with pytest.raises(ValueError, match="양수"):
        make_windows(1000, train=0, test=10)


def test_coverage_description_mentions_window_count():
    idx = pd.bdate_range("2015-01-01", periods=1000)
    text = describe_coverage(make_windows(1000, train=500, test=100), idx)
    assert "5개 구간" in text


# ------------------------------------------------------------------ 미래 참조
def test_result_is_unaffected_by_data_after_the_last_test_window():
    """마지막 검증 구간 이후의 가격을 바꿔도 결과가 달라지면 안 된다.

    워크포워드가 미래를 참조하면, 과최적화를 재는 자가 스스로 과최적화된
    셈이라 도구 전체가 무의미해진다.
    """
    prices = make_prices(n=900, seed=5)
    tampered = prices.copy()
    tampered.iloc[800:] *= 5.0  # 어떤 구간에도 쓰이지 않아야 할 뒷부분

    common = dict(
        candidates={"균등": EqualWeight(), "리스크패리티": RiskParity(lookback=100)},
        train=400, test=200, costs=_zero_cost(), rebalance="M",
    )
    original = walk_forward(prices, **common)
    changed = walk_forward(tampered, **common)

    assert [w.selected for w in original.windows] == [w.selected for w in changed.windows]
    pd.testing.assert_series_equal(original.equity, changed.equity)


def test_selection_uses_only_training_data():
    """학습 구간 점수는 검증 구간 가격이 바뀌어도 동일해야 한다."""
    prices = make_prices(n=800, seed=9)
    tampered = prices.copy()
    tampered.iloc[500:] *= 3.0  # 첫 구간의 검증 부분

    common = dict(
        candidates={"균등": EqualWeight(), "리스크패리티": RiskParity(lookback=100)},
        train=500, test=150, costs=_zero_cost(), rebalance="M",
    )
    original = walk_forward(prices, **common)
    changed = walk_forward(tampered, **common)

    assert original.windows[0].train_scores == changed.windows[0].train_scores
    assert original.windows[0].selected == changed.windows[0].selected


# ------------------------------------------------------------------ 결과 구조
@pytest.fixture
def wf_result():
    prices = make_prices(n=1200, seed=11)
    return walk_forward(
        prices,
        candidates={
            "균등": EqualWeight(),
            "리스크패리티": RiskParity(lookback=100),
            "듀얼모멘텀": DualMomentum(lookbacks=[21, 63], top_n=2, safe_asset="STEADY"),
        },
        train=500, test=200, costs=_zero_cost(), rebalance="M",
    )


def test_equity_covers_only_test_periods(wf_result):
    """이어 붙인 자산곡선은 검증 구간만 담아야 한다."""
    first_test = wf_result.windows[0].test_returns.index[0]
    last_test = wf_result.windows[-1].test_returns.index[-1]
    assert wf_result.equity.index[-1] == last_test
    assert wf_result.equity.index[1] == first_test  # [0]은 시작값 기준 봉


def test_equity_starts_at_initial_cash(wf_result):
    assert wf_result.equity.iloc[0] == pytest.approx(wf_result.initial_cash)


def test_equity_has_no_duplicate_dates(wf_result):
    assert not wf_result.equity.index.duplicated().any()
    assert wf_result.equity.index.is_monotonic_increasing


def test_selection_counts_sum_to_window_count(wf_result):
    assert sum(wf_result.selection_counts().values()) == len(wf_result.windows)


def test_churn_is_zero_when_one_candidate_always_wins():
    """후보가 하나면 선택이 바뀔 수 없다."""
    prices = make_prices(n=1200, seed=2)
    result = walk_forward(
        prices, candidates={"균등": EqualWeight()},
        train=500, test=200, costs=_zero_cost(), rebalance="M",
    )
    assert result.selection_churn() == pytest.approx(0.0)
    assert set(result.selection_counts()) == {"균등"}


def test_efficiency_is_ratio_of_means(wf_result):
    expected = wf_result.out_of_sample_mean() / wf_result.in_sample_mean()
    assert wf_result.efficiency() == pytest.approx(expected)


def test_frame_has_one_row_per_window(wf_result):
    frame = wf_result.to_frame()
    assert len(frame) == len(wf_result.windows)
    assert {"selected", "train_score", "test_score"} <= set(frame.columns)


# ------------------------------------------------------------------ 오류 처리
def test_rejects_empty_candidates():
    with pytest.raises(ValueError, match="후보 전략이 비어"):
        walk_forward(make_prices(n=500), candidates={}, train=200, test=100)


def test_rejects_unknown_objective():
    with pytest.raises(ValueError, match="objective"):
        walk_forward(
            make_prices(n=500), candidates={"a": EqualWeight()},
            train=200, test=100, objective="profit",
        )


def test_short_data_explains_what_to_change():
    with pytest.raises(ValueError, match="구간을 만들 수 없습니다"):
        walk_forward(
            make_prices(n=300), candidates={"a": EqualWeight()}, train=500, test=100
        )


def test_warmup_longer_than_train_is_rejected():
    """전략이 준비 기간도 못 채우는 학습 구간은 의미가 없다."""
    with pytest.raises(ValueError, match="준비 기간"):
        walk_forward(
            make_prices(n=1000),
            candidates={"느린전략": RiskParity(lookback=400)},
            train=300, test=100,
        )


@pytest.mark.parametrize("objective", ["sharpe", "sortino", "calmar", "cagr", "max_drawdown"])
def test_every_objective_runs(objective):
    prices = make_prices(n=1000, seed=4)
    result = walk_forward(
        prices,
        candidates={"균등": EqualWeight(), "리스크패리티": RiskParity(lookback=100)},
        train=500, test=200, objective=objective, costs=_zero_cost(), rebalance="M",
    )
    assert len(result.windows) >= 1


def test_deterministic_strategy_beats_noise_in_selection():
    """확실히 나은 전략이 있으면 매 구간 그게 선택돼야 한다."""
    idx = pd.bdate_range("2018-01-01", periods=1200)
    rs = np.random.RandomState(0)
    prices = pd.DataFrame(
        {
            "WIN": 100 * np.cumprod(1 + rs.normal(0.0008, 0.004, 1200)),   # 꾸준한 상승
            "LOSE": 100 * np.cumprod(1 + rs.normal(-0.0005, 0.02, 1200)),  # 손실 + 고변동
        },
        index=idx,
    )
    result = walk_forward(
        prices,
        candidates={"승자만": FixedWeight({"WIN": 1.0}), "패자만": FixedWeight({"LOSE": 1.0})},
        train=500, test=200, costs=_zero_cost(), rebalance="M",
    )
    assert set(result.selection_counts()) == {"승자만"}
    assert result.selection_churn() == pytest.approx(0.0)


# --- 후보가 1개일 때 보고서가 거짓 안심을 주지 않아야 한다 -------------------
# 실제로 사용자가 후보 없이 돌렸을 때 "선택 뒤집힘 0.00 · 선택이 안정적"이
# 찍혔다. 뒤집힐 대상이 없어서 0인 것을 '안정적'이라고 칭찬한 것이고, 검증한
# 적 없는 것을 검증했다고 믿게 만든다.


@pytest.fixture
def single_prices():
    return make_prices(n=600, seed=23)


def _single_candidate_result(prices):
    from stockbot.portfolio.fixed import EqualWeight

    return walk_forward(
        prices, {"균등": EqualWeight()}, train=120, test=60, objective="sharpe"
    )


def test_single_candidate_is_recorded_as_such(single_prices):
    result = _single_candidate_result(single_prices)
    assert result.n_candidates == 1


def test_report_does_not_call_a_single_candidate_selection_stable(single_prices):
    from stockbot import reporting

    text = reporting.format_walkforward(
        _single_candidate_result(single_prices), single_prices.index
    )
    assert "선택이 안정적" not in text, "후보 1개인데 선택이 안정적이라고 칭찬했습니다"
    assert "후보가 1개라 '선택'이 없었습니다" in text
    assert "후보가 1개인 실행입니다" in text
    assert "candidates:" in text, "후보를 추가하는 방법을 알려줘야 합니다"


def test_report_keeps_churn_verdict_when_there_are_real_candidates(single_prices):
    from stockbot import reporting
    from stockbot.portfolio.fixed import EqualWeight
    from stockbot.portfolio.risk_parity import RiskParity

    result = walk_forward(
        single_prices,
        {"균등": EqualWeight(), "리스크패리티": RiskParity(lookback=60)},
        train=120,
        test=60,
        objective="sharpe",
    )
    assert result.n_candidates == 2
    text = reporting.format_walkforward(result, single_prices.index)
    assert "후보가 1개" not in text
    assert "선택 뒤집힘 비율" in text


def test_efficiency_above_one_is_not_called_reproduced_well():
    """검증이 학습보다 좋은 것은 튼튼함이 아니라 구간 난이도 차이다."""
    from stockbot.reporting import _efficiency_verdict

    assert "구간 난이도" in _efficiency_verdict(1.67)
    assert "잘 재현됨" not in _efficiency_verdict(1.67)
    # 1 근처는 기존 문구를 유지한다.
    assert "잘 재현됨" in _efficiency_verdict(0.95)
