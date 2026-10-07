"""변동성 기반 비중 산출 — 역변동성 및 ERC(동일 위험기여도).

리스크 패리티의 아이디어: 자산별 *금액*이 아니라 *위험 기여도*를 균등하게 맞춘다.
주식 60 / 채권 40 포트폴리오는 금액은 6:4지만 위험 기여도는 대략 9:1에 가깝다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy, apply_bounds, normalize


def risk_contributions(weights: pd.Series, cov: pd.DataFrame) -> pd.Series:
    """각 자산의 위험 기여도(합 = 포트폴리오 변동성)."""
    w = weights.reindex(cov.index).fillna(0.0).to_numpy(dtype=float)
    sigma = cov.to_numpy(dtype=float)
    port_var = float(w @ sigma @ w)
    if port_var <= 0:
        return pd.Series(0.0, index=cov.index)
    marginal = sigma @ w
    return pd.Series(w * marginal / np.sqrt(port_var), index=cov.index)


def inverse_volatility_weights(cov: pd.DataFrame) -> pd.Series:
    """역변동성 비중. 상관관계는 무시하는 리스크 패리티의 단순 근사."""
    vol = pd.Series(np.sqrt(np.diag(cov.to_numpy(dtype=float))), index=cov.index)
    inv = 1.0 / vol.replace(0.0, np.nan)
    inv = inv.replace([np.inf, -np.inf], np.nan).dropna()
    if inv.empty:
        return pd.Series(0.0, index=cov.index)
    return (inv / inv.sum()).reindex(cov.index).fillna(0.0)


def equal_risk_contribution_weights(
    cov: pd.DataFrame,
    budgets: pd.Series | None = None,
    tol: float = 1e-10,
    max_iter: int = 1000,
) -> pd.Series:
    """ERC(동일 위험기여도) 비중을 순환 좌표하강법으로 구한다.

    Spinu(2013)의 볼록 문제 ``min 0.5 wᵀΣw - Σ bᵢ ln wᵢ`` 를 푼다. 각 좌표의
    1차 조건이 2차 방정식이라 닫힌 해가 있어 scipy 없이도 안정적으로 수렴한다.

    Args:
        cov: 공분산 행렬.
        budgets: 목표 위험 예산(합 1). None이면 동일 배분.
    """
    assets = list(cov.index)
    n = len(assets)
    if n == 0:
        return pd.Series(dtype=float)
    if n == 1:
        return pd.Series([1.0], index=assets)

    sigma = np.array(cov.to_numpy(dtype=float), copy=True)
    # 수치 안정성: 대각이 0이거나 특이행렬인 경우를 막는다.
    diag_floor = max(float(np.mean(np.diag(sigma))) * 1e-8, 1e-16)
    np.fill_diagonal(sigma, np.maximum(np.diag(sigma), diag_floor))

    if budgets is None:
        b = np.full(n, 1.0 / n)
    else:
        b = budgets.reindex(assets).fillna(0.0).to_numpy(dtype=float)
        if b.sum() <= 0:
            b = np.full(n, 1.0 / n)
        b = b / b.sum()

    w = inverse_volatility_weights(cov).reindex(assets).fillna(1.0 / n).to_numpy(dtype=float)
    w = np.maximum(w, 1e-8)

    for _ in range(max_iter):
        w_prev = w.copy()
        for i in range(n):
            a = sigma[i, i]
            # sum_{j != i} Σ_ij w_j
            c = float(sigma[i] @ w) - a * w[i]
            # a*w² + c*w - b_i = 0 의 양의 근
            w[i] = (-c + np.sqrt(c * c + 4.0 * a * b[i])) / (2.0 * a)
        if np.max(np.abs(w - w_prev)) < tol:
            break

    total = w.sum()
    if total <= 0 or not np.isfinite(total):  # pragma: no cover - 안전장치
        return inverse_volatility_weights(cov)
    return pd.Series(w / total, index=assets)


def _covariance(history: pd.DataFrame, lookback: int, halflife: int | None) -> pd.DataFrame:
    """최근 `lookback`봉 수익률의 공분산. halflife 지정 시 지수가중."""
    window = history.iloc[-(lookback + 1):]
    rets = window.pct_change().dropna(how="all")
    rets = rets.dropna(axis=1, how="any")
    if rets.shape[0] < 2 or rets.shape[1] == 0:
        return pd.DataFrame(dtype=float)
    if halflife:
        return rets.ewm(halflife=halflife, min_periods=2).cov().iloc[-rets.shape[1]:]
    return rets.cov()


class RiskParity(Strategy):
    """변동성 기반 비중 전략.

    Args:
        lookback: 공분산 추정에 쓸 봉 개수.
        method: ``"erc"``(상관관계 반영) 또는 ``"inverse_vol"``(단순 역변동성).
        halflife: 지정 시 지수가중 공분산을 쓴다(최근 데이터에 가중).
        max_weight / min_weight: 개별 종목 비중 제한.
        target_volatility: 지정 시 포트폴리오 예상 변동성이 이 값(연율)을 넘지
            않도록 전체 비중을 축소한다. 남는 부분은 현금.
    """

    def __init__(
        self,
        lookback: int = 120,
        method: str = "erc",
        halflife: int | None = None,
        max_weight: float = 1.0,
        min_weight: float = 0.0,
        target_volatility: float | None = None,
        periods_per_year: int = 252,
        name: str = "risk_parity",
    ) -> None:
        if method not in {"erc", "inverse_vol"}:
            raise ValueError("method는 'erc' 또는 'inverse_vol'이어야 합니다")
        self.lookback = lookback
        self.method = method
        self.halflife = halflife
        self.max_weight = max_weight
        self.min_weight = min_weight
        self.target_volatility = target_volatility
        self.periods_per_year = periods_per_year
        self.name = name

    def warmup(self) -> int:
        return self.lookback + 1

    def target_weights(self, history: pd.DataFrame) -> pd.Series:
        cov = _covariance(history, self.lookback, self.halflife)
        if cov.empty:
            return pd.Series(0.0, index=history.columns)

        if self.method == "erc":
            w = equal_risk_contribution_weights(cov)
        else:
            w = inverse_volatility_weights(cov)

        w = apply_bounds(w, self.min_weight, self.max_weight)

        if self.target_volatility:
            w = scale_to_target_volatility(
                w, cov, self.target_volatility, self.periods_per_year
            )
        return normalize(w.reindex(history.columns).fillna(0.0))


def scale_to_target_volatility(
    weights: pd.Series,
    cov: pd.DataFrame,
    target_volatility: float,
    periods_per_year: int = 252,
) -> pd.Series:
    """예상 변동성이 목표치를 넘으면 전체 비중을 비례 축소한다(레버리지는 쓰지 않음)."""
    w = weights.reindex(cov.index).fillna(0.0).to_numpy(dtype=float)
    port_vol = float(np.sqrt(max(w @ cov.to_numpy(dtype=float) @ w, 0.0)) * np.sqrt(periods_per_year))
    if port_vol <= 0:
        return weights
    scale = min(1.0, target_volatility / port_vol)
    return weights * scale
