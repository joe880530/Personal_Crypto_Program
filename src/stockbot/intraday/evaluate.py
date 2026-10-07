"""변동성 돌파 워크포워드 + 합격 판정.

**합격선을 돌리기 전에 정해 두었다.** 결과를 본 뒤에 기준을 정하면 반드시
자기합리화가 된다. 백테스트에서 떨어진 것을 "실전은 다를 수도 있다"며 밀어붙이는
것이 이 바닥에서 돈을 잃는 가장 흔한 경로다.

기준과 근거:
    비용 차감 검증 샤프 > 0.5   비용을 넣고도 남는가
    학습<->검증 샤프 상관 > 0   최소한 음수는 아닐 것(지난 월간 검증은 -0.109였다)
    연간 진입 < 150회           왕복 11bp x 150 = 연 17% 비용이 상한
    최대낙폭 < 25%              현재 균등비중이 -17%
    buy&hold 대비 우위          수고한 값어치가 있었나
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from .. import metrics
from ..backtest.costs import CostModel
from ..validation.splits import make_windows
from .breakout import BreakoutParams, BreakoutResult, buy_and_hold, run_breakout

TRADING_DAYS = 252


@dataclasses.dataclass(frozen=True)
class Criteria:
    """미리 정해 둔 합격선. 결과를 보고 고치지 말 것."""

    min_test_sharpe: float = 0.5
    min_score_correlation: float = 0.0
    max_trades_per_year: float = 150.0
    max_drawdown: float = 0.25
    must_beat_buy_and_hold: bool = True


@dataclasses.dataclass
class Window:
    """구간 하나: 학습에서 고른 K가 검증에서 어땠는가."""

    train_span: tuple[pd.Timestamp, pd.Timestamp]
    test_span: tuple[pd.Timestamp, pd.Timestamp]
    selected: BreakoutParams
    train_sharpe: float
    test_sharpe: float
    test_returns: pd.Series
    test_trades: int


@dataclasses.dataclass
class Evaluation:
    """워크포워드 전체 결과와 판정 근거."""

    windows: list[Window]
    equity: pd.Series
    hold_equity: pd.Series
    n_candidates: int
    total_trades: int
    total_days: int

    @property
    def trades_per_year(self) -> float:
        return self.total_trades / self.total_days * TRADING_DAYS if self.total_days else 0.0

    def summary(self) -> dict[str, float]:
        return metrics.summary(self.equity, periods_per_year=TRADING_DAYS)

    def hold_summary(self) -> dict[str, float]:
        return metrics.summary(self.hold_equity, periods_per_year=TRADING_DAYS)

    def score_correlation(self) -> float:
        """학습 점수와 검증 점수의 상관.

        이 값이 0 이하면 '과거에 좋았던 설정을 고르는' 행위가 미래와 무관하다는
        뜻이다. 성과가 좋아 보여도 재현을 기대할 근거가 없다.
        """
        pairs = [(w.train_sharpe, w.test_sharpe) for w in self.windows
                 if np.isfinite(w.train_sharpe) and np.isfinite(w.test_sharpe)]
        if len(pairs) < 3:
            return float("nan")
        train, test = zip(*pairs)
        if np.std(train) == 0 or np.std(test) == 0:
            return float("nan")
        return float(np.corrcoef(train, test)[0, 1])

    def selection_churn(self) -> float:
        """구간이 바뀔 때 고른 K가 뒤집히는 비율. 후보가 1개면 해당 없음."""
        if self.n_candidates < 2 or len(self.windows) < 2:
            return float("nan")
        flips = sum(
            1 for a, b in zip(self.windows, self.windows[1:]) if a.selected != b.selected
        )
        return flips / (len(self.windows) - 1)


@dataclasses.dataclass
class Check:
    """기준 하나에 대한 판정."""

    name: str
    value: float
    threshold: str
    passed: bool
    note: str = ""


def judge(evaluation: Evaluation, criteria: Criteria | None = None) -> list[Check]:
    """미리 정한 기준에 대고 합격/불합격을 매긴다."""
    criteria = criteria or Criteria()
    summary = evaluation.summary()
    hold = evaluation.hold_summary()
    correlation = evaluation.score_correlation()

    checks = [
        Check("비용 차감 검증 샤프", summary["sharpe"],
              f"> {criteria.min_test_sharpe}",
              summary["sharpe"] > criteria.min_test_sharpe),
        Check("학습<->검증 상관", correlation,
              f"> {criteria.min_score_correlation}",
              bool(np.isfinite(correlation)) and correlation > criteria.min_score_correlation,
              "" if np.isfinite(correlation) else "구간이 3개 미만이라 계산 불가"),
        Check("연간 진입 횟수", evaluation.trades_per_year,
              f"< {criteria.max_trades_per_year:.0f}",
              evaluation.trades_per_year < criteria.max_trades_per_year),
        Check("최대낙폭", summary["max_drawdown"],
              f"> -{criteria.max_drawdown:.0%}",
              summary["max_drawdown"] > -criteria.max_drawdown),
    ]
    if criteria.must_beat_buy_and_hold:
        checks.append(Check(
            "buy&hold 대비 누적수익", summary["total_return"] - hold["total_return"],
            "> 0", summary["total_return"] > hold["total_return"],
            f"전략 {summary['total_return']:+.1%} vs 보유 {hold['total_return']:+.1%}",
        ))
    return checks


def walk_forward_breakout(
    ohlc: pd.DataFrame,
    candidates: list[BreakoutParams],
    costs: CostModel,
    train: int = 756,
    test: int = 252,
    step: int | None = None,
    mode: str = "rolling",
    initial_cash: float = 10_000_000.0,
    cash_rate: float = 0.0,
) -> Evaluation:
    """앞 구간에서 K를 고르고, 바로 뒤 구간에서만 평가한다."""
    if not candidates:
        raise ValueError("후보가 비어 있습니다")

    windows = make_windows(len(ohlc), train=train, test=test, step=step, mode=mode)
    if not windows:
        raise ValueError(
            f"구간을 만들 수 없습니다. 일봉 {len(ohlc)}개로는 학습 {train}개 +"
            f" 검증 {test}개를 담을 수 없습니다.\n"
            "  조회 시작일을 앞당기거나 --train/--test를 줄이세요."
        )

    results: list[Window] = []
    stitched: list[pd.Series] = []
    trades = 0

    for window in windows:
        train_slice = ohlc.iloc[window.train_start:window.train_end]
        test_slice = ohlc.iloc[window.test_start:window.test_end]

        scored: list[tuple[float, BreakoutParams]] = []
        for params in candidates:
            try:
                run = run_breakout(train_slice, params, costs, initial_cash, cash_rate)
            except ValueError:
                continue                      # 이 구간엔 데이터가 모자란 설정
            scored.append((_sharpe(run), params))
        if not scored:
            continue

        train_sharpe, selected = max(scored, key=lambda pair: _nan_low(pair[0]))
        tested = run_breakout(test_slice, selected, costs, initial_cash, cash_rate)

        returns = tested.equity.pct_change().dropna()
        results.append(Window(
            train_span=(train_slice.index[0], train_slice.index[-1]),
            test_span=(test_slice.index[0], test_slice.index[-1]),
            selected=selected,
            train_sharpe=train_sharpe,
            test_sharpe=_sharpe(tested),
            test_returns=returns,
            test_trades=tested.n_trades,
        ))
        stitched.append(returns)
        trades += tested.n_trades

    if not results:
        raise ValueError("평가할 수 있는 구간이 없습니다")

    joined = pd.concat(stitched).sort_index()
    equity = initial_cash * (1 + joined).cumprod()
    # 같은 구간을 그냥 들고만 있었다면. 비교 대상이 없으면 판정이 무의미하다.
    span = ohlc.loc[equity.index[0]:equity.index[-1]]
    return Evaluation(
        windows=results,
        equity=equity,
        hold_equity=buy_and_hold(span, initial_cash),
        n_candidates=len(candidates),
        total_trades=trades,
        total_days=len(equity),
    )


def _sharpe(result: BreakoutResult) -> float:
    returns = result.equity.pct_change().dropna()
    return metrics.sharpe(returns, 0.0, TRADING_DAYS) if len(returns) > 1 else float("nan")


def _nan_low(value: float) -> float:
    """NaN을 최하위로 보내 '계산 안 된 것'이 뽑히지 않게 한다."""
    return -np.inf if not np.isfinite(value) else value
