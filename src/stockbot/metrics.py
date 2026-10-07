"""성과 지표 계산.

모든 함수는 일별(daily) 수익률 또는 자산곡선(equity curve)을 입력으로 받는다.
연율화 기준일수는 `periods_per_year`로 조절한다(일봉 기준 252).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def to_returns(equity: pd.Series) -> pd.Series:
    """자산곡선 -> 기간 수익률."""
    return equity.pct_change().dropna()


def cagr(equity: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """연평균 복리 수익률."""
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return float("nan")
    years = (len(equity) - 1) / periods_per_year
    if years <= 0:
        return float("nan")
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)


def annual_volatility(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    if len(returns) < 2:
        return float("nan")
    return float(returns.std(ddof=1) * np.sqrt(periods_per_year))


def sharpe(
    returns: pd.Series, risk_free: float = 0.0, periods_per_year: int = TRADING_DAYS
) -> float:
    """샤프 비율. risk_free는 연율 기준(예: 0.03)."""
    if len(returns) < 2:
        return float("nan")
    excess = returns - risk_free / periods_per_year
    sd = excess.std(ddof=1)
    if sd == 0 or np.isnan(sd):
        return float("nan")
    return float(excess.mean() / sd * np.sqrt(periods_per_year))


def downside_deviation(
    returns: pd.Series, target: float = 0.0, periods_per_year: int = TRADING_DAYS
) -> float:
    """하방 편차(연율). 목표 수익률을 밑도는 부분만 위험으로 센다.

    분모는 **전체 관측 수**다(Sortino & Price의 원 정의). 음수인 날의 개수로
    나누면 '하락한 날들이 평균적으로 얼마나 나빴나'가 되어, 대칭 분포에서
    값이 표준편차와 같아지고 소르티노가 샤프와 구별되지 않는다.
    """
    if len(returns) < 2:
        return float("nan")
    shortfall = (returns - target).clip(upper=0.0)
    return float(np.sqrt((shortfall**2).mean()) * np.sqrt(periods_per_year))


def sortino(
    returns: pd.Series, risk_free: float = 0.0, periods_per_year: int = TRADING_DAYS
) -> float:
    """소르티노 비율. 상승 변동성은 위험으로 치지 않는다.

    샤프는 크게 오른 날도 위험으로 세기 때문에, 상승이 가파른 전략에 불리하다.
    소르티노는 목표를 밑도는 움직임만 분모에 넣는다.
    """
    if len(returns) < 2:
        return float("nan")
    daily_target = risk_free / periods_per_year
    excess = returns - daily_target
    dd = downside_deviation(returns, daily_target, periods_per_year)
    if not dd or np.isnan(dd) or dd == 0:
        return float("nan")
    return float(excess.mean() * periods_per_year / dd)


def drawdown(equity: pd.Series) -> pd.Series:
    """고점 대비 낙폭 시계열(음수)."""
    peak = equity.cummax()
    return equity / peak - 1.0


def max_drawdown(equity: pd.Series) -> float:
    """최대 낙폭(MDD). 음수로 반환한다."""
    if equity.empty:
        return float("nan")
    return float(drawdown(equity).min())


def max_drawdown_duration(equity: pd.Series) -> int:
    """최대 낙폭 회복까지 걸린 최장 기간(봉 개수)."""
    if equity.empty:
        return 0
    dd = drawdown(equity)
    longest = current = 0
    for value in dd:
        current = current + 1 if value < 0 else 0
        longest = max(longest, current)
    return longest


def calmar(equity: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """CAGR / |MDD|. 낙폭 대비 수익 효율."""
    mdd = max_drawdown(equity)
    if not mdd or np.isnan(mdd) or mdd == 0:
        return float("nan")
    return float(cagr(equity, periods_per_year) / abs(mdd))


def summary(
    equity: pd.Series,
    risk_free: float = 0.0,
    periods_per_year: int = TRADING_DAYS,
) -> dict[str, float]:
    """주요 지표를 한 번에 계산한다."""
    rets = to_returns(equity)
    return {
        "start_value": float(equity.iloc[0]) if len(equity) else float("nan"),
        "end_value": float(equity.iloc[-1]) if len(equity) else float("nan"),
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) > 1 else float("nan"),
        "cagr": cagr(equity, periods_per_year),
        "volatility": annual_volatility(rets, periods_per_year),
        "sharpe": sharpe(rets, risk_free, periods_per_year),
        "sortino": sortino(rets, risk_free, periods_per_year),
        "max_drawdown": max_drawdown(equity),
        "max_drawdown_days": float(max_drawdown_duration(equity)),
        "calmar": calmar(equity, periods_per_year),
        "best_day": float(rets.max()) if len(rets) else float("nan"),
        "worst_day": float(rets.min()) if len(rets) else float("nan"),
        "positive_days": float((rets > 0).mean()) if len(rets) else float("nan"),
    }
