"""워크포워드 검증.

절차는 단순하다. 앞 구간(학습)에서 후보 전략들을 비교해 하나를 고르고, 그
선택을 **바로 뒤 구간(검증)에서만** 평가한다. 창을 밀면서 반복하고, 검증
구간의 수익률만 이어 붙여 하나의 자산곡선을 만든다.

이렇게 얻은 곡선은 "그때그때 가진 데이터로 전략을 골랐다면 실제로 어땠을까"에
대한 답이다. 전체 기간 백테스트가 답하지 못하는 질문이다.

한 가지 분명히 할 것: 워크포워드는 과최적화를 **측정**할 뿐 없애지 못한다.
같은 데이터로 워크포워드 설정 자체를 여러 번 바꿔가며 돌리면, 그 결과도
결국 과최적화된다.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from .. import metrics
from ..backtest.costs import CostBook
from ..backtest.engine import Backtester
from ..portfolio.base import Strategy
from .splits import Window, make_windows

#: 전략 선택 기준으로 쓸 수 있는 지표. 값이 클수록 좋다는 규약을 따른다.
OBJECTIVES = {
    "sharpe": lambda s: s["sharpe"],
    "sortino": lambda s: s["sortino"],
    "calmar": lambda s: s["calmar"],
    "cagr": lambda s: s["cagr"],
    # 낙폭은 음수라 그대로 쓰면 '클수록 좋다'가 성립한다(-5% > -30%).
    "max_drawdown": lambda s: s["max_drawdown"],
}


@dataclasses.dataclass
class WindowResult:
    """구간 하나의 결과."""

    window: Window
    train_scores: dict[str, float]
    selected: str
    train_summary: dict[str, float]
    test_summary: dict[str, float]
    test_returns: pd.Series

    @property
    def train_score(self) -> float:
        return self.train_scores.get(self.selected, float("nan"))


@dataclasses.dataclass
class WalkForwardResult:
    """워크포워드 전체 결과."""

    windows: list[WindowResult]
    equity: pd.Series
    objective: str
    initial_cash: float
    periods_per_year: int = 252
    #: 구간마다 비교한 후보 전략의 수. 1이면 '선택'이라는 행위가 없었으므로
    #: 뒤집힘·효율을 과최적화 지표로 읽어서는 안 된다.
    n_candidates: int = 1

    def summary(self, risk_free: float = 0.0) -> dict[str, float]:
        """검증 구간만 이어 붙인 자산곡선의 성과."""
        out = metrics.summary(self.equity, risk_free, self.periods_per_year)
        out["windows"] = float(len(self.windows))
        return out

    @property
    def objective_fn(self):
        return OBJECTIVES[self.objective]

    def in_sample_mean(self) -> float:
        """학습 구간에서 선택된 전략이 받은 점수의 평균."""
        scores = [w.train_score for w in self.windows]
        return float(np.nanmean(scores)) if scores else float("nan")

    def out_of_sample_mean(self) -> float:
        """검증 구간에서 실제로 나온 점수의 평균."""
        scores = [self.objective_fn(w.test_summary) for w in self.windows]
        return float(np.nanmean(scores)) if scores else float("nan")

    def efficiency(self) -> float:
        """검증 성과 / 학습 성과.

        1에 가까우면 학습 구간의 성과가 재현됐다는 뜻이다. 0.5 아래면 절반은
        운이었다고 봐야 하고, 음수면 학습 구간에서 좋아 보인 것이 검증
        구간에서는 오히려 손해였다는 뜻이다.
        """
        in_sample = self.in_sample_mean()
        if not in_sample or np.isnan(in_sample) or in_sample == 0:
            return float("nan")
        return self.out_of_sample_mean() / in_sample

    def selection_counts(self) -> dict[str, int]:
        """어떤 전략이 몇 번 선택됐는지."""
        counts: dict[str, int] = {}
        for window in self.windows:
            counts[window.selected] = counts.get(window.selected, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def selection_churn(self) -> float:
        """구간이 바뀔 때 선택이 뒤집힌 비율.

        1에 가까우면 매번 다른 전략이 1등이라는 뜻이고, 그건 데이터가 전략을
        고르는 게 아니라 잡음이 고르고 있다는 신호다.
        """
        if len(self.windows) < 2:
            return float("nan")
        flips = sum(
            1
            for prev, cur in zip(self.windows, self.windows[1:])
            if prev.selected != cur.selected
        )
        return flips / (len(self.windows) - 1)

    def to_frame(self) -> pd.DataFrame:
        """구간별 결과 표."""
        rows = []
        for w in self.windows:
            rows.append(
                {
                    "train_end": w.window.train_end,
                    "selected": w.selected,
                    "train_score": w.train_score,
                    "test_score": self.objective_fn(w.test_summary),
                    "test_cagr": w.test_summary.get("cagr"),
                    "test_mdd": w.test_summary.get("max_drawdown"),
                }
            )
        return pd.DataFrame(rows)


def _run(
    prices: pd.DataFrame,
    strategy: Strategy,
    start: int,
    end: int,
    backtest_kwargs: dict,
    open_prices: pd.DataFrame | None,
):
    """[start, end) 구간에서 백테스트를 돌린다."""
    window_prices = prices.iloc[start:end]
    window_open = open_prices.iloc[start:end] if open_prices is not None else None
    return Backtester(
        prices=window_prices, strategy=strategy, open_prices=window_open, **backtest_kwargs
    ).run()


def walk_forward(
    prices: pd.DataFrame,
    candidates: dict[str, Strategy],
    train: int,
    test: int,
    step: int | None = None,
    mode: str = "rolling",
    objective: str = "sharpe",
    costs: CostBook | None = None,
    open_prices: pd.DataFrame | None = None,
    initial_cash: float = 10_000_000.0,
    risk_free: float = 0.0,
    **backtest_kwargs,
) -> WalkForwardResult:
    """워크포워드 검증을 수행한다.

    Args:
        prices: 종가 DataFrame.
        candidates: {이름: 전략}. 하나만 넣으면 선택 없이 그 전략의 구간별
            안정성만 본다.
        train / test / step / mode: 구간 분할 방식. `splits.make_windows` 참고.
        objective: 학습 구간에서 전략을 고르는 기준.
        나머지: `Backtester`에 그대로 전달된다.

    Returns:
        검증 구간만 이어 붙인 자산곡선과 구간별 상세.
    """
    if not candidates:
        raise ValueError("후보 전략이 비어 있습니다")
    if objective not in OBJECTIVES:
        raise ValueError(f"알 수 없는 objective: {objective!r}. 사용 가능: {sorted(OBJECTIVES)}")

    score_of = OBJECTIVES[objective]
    windows = make_windows(len(prices), train=train, test=test, step=step, mode=mode)
    if not windows:
        raise ValueError(
            f"구간을 만들 수 없습니다. 데이터 {len(prices)}봉으로는"
            f" 학습 {train}봉 + 검증 {test}봉을 담을 수 없습니다.\n"
            "  backtest.start를 앞당기거나 walkforward의 train/test를 줄이세요."
        )

    longest_warmup = max(s.warmup() for s in candidates.values())
    if longest_warmup >= train:
        raise ValueError(
            f"학습 구간({train}봉)이 전략의 준비 기간({longest_warmup}봉)보다 짧거나 같습니다.\n"
            "  train을 늘리거나 전략의 lookback을 줄이세요."
        )

    common = dict(costs=costs, initial_cash=initial_cash, **backtest_kwargs)
    periods = common.get("periods_per_year", 252)

    results: list[WindowResult] = []
    for window in windows:
        # 1) 학습 구간에서만 후보를 비교한다.
        train_summaries: dict[str, dict[str, float]] = {}
        for name, strategy in candidates.items():
            result = _run(
                prices, strategy, window.train_start, window.train_end, common, open_prices
            )
            train_summaries[name] = result.summary(risk_free)

        scores = {name: score_of(s) for name, s in train_summaries.items()}
        finite = {k: v for k, v in scores.items() if v == v}  # NaN 제외
        if not finite:
            continue
        selected = max(finite, key=lambda k: finite[k])

        # 2) 고른 전략을 검증 구간에서 평가한다.
        #    전략에는 준비 기간이 필요하므로 학습 구간부터 함께 돌리되,
        #    성과는 검증 구간만 떼어 센다. 전략은 언제나 과거만 보므로
        #    이렇게 해도 미래를 참조하지 않는다.
        full = _run(
            prices, candidates[selected], window.train_start, window.test_end, common, open_prices
        )
        offset = window.test_start - window.train_start
        test_equity = full.equity.iloc[offset - 1:]  # 직전 봉부터 떼어 첫 수익률을 살린다
        test_returns = test_equity.pct_change().dropna()

        results.append(
            WindowResult(
                window=window,
                train_scores=scores,
                selected=selected,
                train_summary=train_summaries[selected],
                test_summary=metrics.summary(test_equity, risk_free, periods),
                test_returns=test_returns,
            )
        )

    if not results:
        raise ValueError("모든 구간에서 성과를 계산하지 못했습니다. 데이터를 확인하세요.")

    stitched = pd.concat([r.test_returns for r in results])
    stitched = stitched[~stitched.index.duplicated(keep="first")].sort_index()
    equity = initial_cash * (1.0 + stitched).cumprod()
    # 첫 봉의 시작값을 곡선에 포함시켜야 총수익률이 맞는다.
    first_date = results[0].test_returns.index[0]
    start_stamp = prices.index[prices.index.get_loc(first_date) - 1]
    equity = pd.concat([pd.Series({start_stamp: initial_cash}), equity]).sort_index()

    return WalkForwardResult(
        windows=results,
        equity=equity,
        objective=objective,
        initial_cash=initial_cash,
        periods_per_year=periods,
        n_candidates=len(candidates),
    )
