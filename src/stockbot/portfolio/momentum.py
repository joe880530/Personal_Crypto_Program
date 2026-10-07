"""듀얼 모멘텀 — 상대 모멘텀(무엇을 살까) + 절대 모멘텀(살 때인가).

- 상대 모멘텀: 후보군 중 최근 성과 상위 N개를 고른다.
- 절대 모멘텀: 고른 자산의 성과가 안전자산(또는 0)보다 못하면 그 몫을 안전자산으로 뺀다.

절대 모멘텀이 하락장 방어의 핵심이다. 이것이 없으면 그냥 상대강도 전략이고,
전체 시장이 빠질 때 '덜 빠지는 종목'을 계속 들고 있게 된다.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

import numpy as np
import pandas as pd

from .base import CASH, Strategy, apply_bounds, blended_return, normalize


@dataclasses.dataclass(frozen=True)
class MomentumAllocation:
    """모멘텀 판단 결과.

    Attributes:
        selected: 상대 + 절대 모멘텀을 통과한 위험자산의 비중.
        safe_asset: 대피처 티커. None이면 현금.
        reserve: 안전자산/현금으로 갈 비중.
    """

    selected: pd.Series
    safe_asset: str | None
    reserve: float

    def parking_slot(self, available: Iterable[str]) -> str:
        """대피분이 실제로 갈 곳. 안전자산 가격이 없으면 현금으로 떨어진다."""
        if self.safe_asset and self.safe_asset in set(available):
            return self.safe_asset
        return CASH


class DualMomentum(Strategy):
    """듀얼 모멘텀 전략.

    Args:
        lookbacks: 모멘텀 측정 기간(봉 수) 목록. 기본은 1/3/6/12개월.
        lookback_weights: 각 기간의 가중치. None이면 동일 가중.
        top_n: 보유할 자산 개수.
        safe_asset: 절대 모멘텀 탈락분이 갈 곳(예: 'TLT', 'BIL'). None이면 현금.
        absolute_threshold: 절대 모멘텀 기준 수익률. 기본 0(원금 유지).
        universe: 후보군을 명시적으로 제한할 때 사용. None이면 전체 컬럼.
        weighting: ``"equal"``(균등) 또는 ``"score"``(점수 비례).
    """

    def __init__(
        self,
        lookbacks: list[int] | None = None,
        lookback_weights: list[float] | None = None,
        top_n: int = 3,
        safe_asset: str | None = None,
        absolute_threshold: float = 0.0,
        universe: list[str] | None = None,
        weighting: str = "equal",
        max_weight: float = 1.0,
        name: str = "dual_momentum",
    ) -> None:
        if weighting not in {"equal", "score"}:
            raise ValueError("weighting은 'equal' 또는 'score'여야 합니다")
        self.lookbacks = lookbacks or [21, 63, 126, 252]
        self.lookback_weights = lookback_weights
        self.top_n = top_n
        self.safe_asset = safe_asset
        self.absolute_threshold = absolute_threshold
        self.universe = universe
        self.weighting = weighting
        self.max_weight = max_weight
        self.name = name

    def warmup(self) -> int:
        return max(self.lookbacks) + 1

    def _candidates(self, history: pd.DataFrame) -> list[str]:
        cols = list(self.universe) if self.universe else list(history.columns)
        # 안전자산은 후보군이 아니라 '대피처'이므로 상대 모멘텀 경쟁에서 제외한다.
        if self.safe_asset and self.safe_asset in cols and self.universe is None:
            cols = [c for c in cols if c != self.safe_asset]
        return [c for c in cols if c in history.columns]

    def scores(self, history: pd.DataFrame) -> pd.Series:
        """후보군의 모멘텀 점수(높을수록 강함)."""
        cands = self._candidates(history)
        if not cands:
            return pd.Series(dtype=float)
        return blended_return(history[cands], self.lookbacks, self.lookback_weights).dropna()

    def allocate(self, history: pd.DataFrame) -> "MomentumAllocation":
        """선정된 위험자산 비중과 안전자산 대피분을 분리해서 반환한다.

        `target_weights`는 이 결과를 합쳐 쓰고, 결합 전략은 대피분을 건드리지
        않은 채 위험자산만 다시 사이징하기 위해 이걸 직접 쓴다.
        """
        score = self.scores(history)
        if score.empty:
            return MomentumAllocation(pd.Series(dtype=float), self.safe_asset, 1.0)

        selected = score.sort_values(ascending=False).head(self.top_n)
        slot = 1.0 / float(self.top_n)  # 선정 개수가 top_n보다 적으면 나머지는 대피분
        if self.weighting == "score":
            positive = selected.clip(lower=0.0)
            if positive.sum() > 0:
                alloc = positive / positive.sum() * (len(selected) * slot)
            else:
                alloc = pd.Series(slot, index=selected.index)
        else:
            alloc = pd.Series(slot, index=selected.index)

        # 절대 모멘텀: 기준 미달 자산의 몫을 대피분으로 돌린다.
        failed = selected[selected <= self.absolute_threshold].index
        alloc[failed] = 0.0
        alloc = alloc[alloc > 0]

        alloc = apply_bounds(alloc, 0.0, self.max_weight) if len(alloc) else alloc
        reserve = max(0.0, 1.0 - float(alloc.sum()))
        return MomentumAllocation(alloc, self.safe_asset, reserve)

    def target_weights(self, history: pd.DataFrame) -> pd.Series:
        alloc = self.allocate(history)
        weights = pd.Series(0.0, index=list(history.columns) + [CASH], dtype=float)
        for ticker, w in alloc.selected.items():
            weights[ticker] += w
        weights[alloc.parking_slot(history.columns)] += alloc.reserve
        return normalize(weights)


class MomentumRiskParity(Strategy):
    """모멘텀으로 *고르고*, 리스크 패리티로 *크기를 정하는* 결합 전략.

    "무엇을 살지"와 "얼마나 살지"는 서로 다른 문제다. 모멘텀은 방향 선택에,
    변동성 기반 비중은 위험 배분에 각각 강점이 있어 조합이 자연스럽다.
    """

    def __init__(
        self,
        momentum: DualMomentum,
        sizer,
        name: str = "momentum_risk_parity",
    ) -> None:
        self.momentum = momentum
        self.sizer = sizer
        self.name = name

    def warmup(self) -> int:
        return max(self.momentum.warmup(), self.sizer.warmup())

    def target_weights(self, history: pd.DataFrame) -> pd.Series:
        alloc = self.momentum.allocate(history)
        held = list(alloc.selected.index)

        weights = pd.Series(0.0, index=list(history.columns) + [CASH], dtype=float)
        if not held:
            weights[alloc.parking_slot(history.columns)] = alloc.reserve
            weights[CASH] += max(0.0, 1.0 - float(weights.sum()))
            return normalize(weights)

        # 절대 모멘텀이 결정한 투자 비율(gross)은 유지하고, 그 안에서만 위험을 배분한다.
        gross = float(alloc.selected.sum())
        sized = self.sizer.target_weights(history[held]).reindex(held).fillna(0.0)
        if sized.sum() <= 0:  # pragma: no cover - 안전장치
            sized = pd.Series(1.0 / len(held), index=held)
        sized = sized / sized.sum() * gross

        for ticker, w in sized.items():
            weights[ticker] += w
        weights[alloc.parking_slot(history.columns)] += alloc.reserve
        weights[CASH] += max(0.0, 1.0 - float(weights.sum()))
        return normalize(weights)
