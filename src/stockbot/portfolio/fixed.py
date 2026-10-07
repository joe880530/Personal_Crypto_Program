"""고정 비중 전략 — 목표 비중을 직접 정하고 리밸런싱으로 유지한다.

가장 단순하지만 장기 검증이 가장 잘 된 방식이다. 새 전략을 만들 때는 항상
이걸 벤치마크로 두고 "이걸 이겼는가"를 먼저 물어야 한다.
"""

from __future__ import annotations

import pandas as pd

from .base import CASH, Strategy, normalize


class FixedWeight(Strategy):
    """정해진 비중을 그대로 목표로 삼는다.

    Args:
        weights: {티커: 비중}. 합이 1 미만이면 나머지는 현금.
    """

    def __init__(self, weights: dict[str, float], name: str = "fixed_weight") -> None:
        if not weights:
            raise ValueError("weights가 비어 있습니다")
        self._weights = pd.Series(weights, dtype=float)
        if (self._weights < 0).any():
            raise ValueError("비중에 음수는 허용하지 않습니다(공매도 미지원)")
        total = float(self._weights.sum())
        if total > 1.0 + 1e-9:
            raise ValueError(f"비중 합이 1을 초과합니다: {total:.4f}")
        self.name = name

    def warmup(self) -> int:
        return 1

    def target_weights(self, history: pd.DataFrame) -> pd.Series:
        weights = pd.Series(0.0, index=list(history.columns) + [CASH], dtype=float)
        for ticker, w in self._weights.items():
            if ticker in weights.index:
                weights[ticker] = w
            else:
                # 가격 데이터가 없는 종목은 살 수 없으므로 현금으로 둔다.
                weights[CASH] += w
        weights[CASH] += max(0.0, 1.0 - float(weights.sum()))
        return normalize(weights)


class EqualWeight(Strategy):
    """전체 유니버스 균등 비중. 단순 벤치마크용."""

    def __init__(self, universe: list[str] | None = None, name: str = "equal_weight") -> None:
        self.universe = universe
        self.name = name

    def target_weights(self, history: pd.DataFrame) -> pd.Series:
        cols = [c for c in (self.universe or history.columns) if c in history.columns]
        # 해당 시점에 가격이 있는 종목만 대상으로 한다(상장 전 종목 제외).
        live = [c for c in cols if pd.notna(history[c].iloc[-1])]
        weights = pd.Series(0.0, index=list(history.columns) + [CASH], dtype=float)
        if not live:
            weights[CASH] = 1.0
            return weights
        weights[live] = 1.0 / len(live)
        return normalize(weights)
