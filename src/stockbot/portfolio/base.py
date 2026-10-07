"""전략 인터페이스.

핵심 규약: 전략은 `target_weights(history)`만 구현한다.
`history`는 **판단 시점까지 잘린** 가격 DataFrame이므로, 전략 코드가 미래를
들여다볼 방법이 구조적으로 없다(look-ahead bias 방지).
"""

from __future__ import annotations

import abc

import numpy as np
import pandas as pd

CASH = "CASH"


class Strategy(abc.ABC):
    """목표 비중을 산출하는 전략의 기반 클래스."""

    name: str = "strategy"

    @abc.abstractmethod
    def target_weights(self, history: pd.DataFrame) -> pd.Series:
        """판단 시점까지의 가격만 보고 목표 비중을 반환한다.

        Args:
            history: index=날짜(오름차순), columns=티커, 값=수정주가. 마지막 행이 판단 시점.

        Returns:
            index=티커(+`CASH`), 합이 1 이하인 비중 Series. 남는 비중은 현금으로 본다.
        """

    def warmup(self) -> int:
        """전략이 신호를 내기 위해 필요한 최소 봉 개수."""
        return 1

    def __repr__(self) -> str:  # pragma: no cover - 표시용
        return f"<{type(self).__name__} name={self.name!r}>"


def normalize(weights: pd.Series, max_total: float = 1.0) -> pd.Series:
    """음수 제거 + 합이 max_total을 넘지 않도록 정규화."""
    w = weights.astype(float).fillna(0.0).clip(lower=0.0)
    total = w.sum()
    if total > max_total and total > 0:
        w = w * (max_total / total)
    return w


def apply_bounds(
    weights: pd.Series, min_weight: float = 0.0, max_weight: float = 1.0
) -> pd.Series:
    """개별 종목 비중 상·하한을 적용하고 다시 정규화한다.

    상한에 걸린 초과분은 여유 있는 종목들에 비례 배분한다. 재분배 후에도 상한을
    넘는 종목이 생길 수 있으므로 수렴할 때까지 반복한다.
    """
    w = normalize(weights)
    active = w[w > 0].index
    if len(active) == 0:
        return w

    total = float(w.sum())
    for _ in range(100):
        capped = w.clip(upper=max_weight)
        excess = total - float(capped.sum())
        if excess <= 1e-12:
            w = capped
            break
        room = (max_weight - capped).clip(lower=0.0)
        room = room.where(capped > 0, 0.0)  # 원래 0이던 종목엔 배분하지 않는다
        if room.sum() <= 1e-12:
            w = capped
            break
        w = capped + room / room.sum() * excess
    else:  # pragma: no cover - 수렴 실패시 안전장치
        w = w.clip(upper=max_weight)

    if min_weight > 0:
        w[(w > 0) & (w < min_weight)] = 0.0
        w = normalize(w, max_total=total)
    return w


def blended_return(history: pd.DataFrame, lookbacks: list[int], weights: list[float] | None = None) -> pd.Series:
    """여러 기간의 총수익률을 가중 평균한 모멘텀 점수.

    단일 기간(예: 12개월)만 쓰면 특정 시점에 과민해지므로, 1/3/6/12개월을
    섞어 쓰는 방식이 실무에서 더 안정적이다.
    """
    if weights is None:
        weights = [1.0] * len(lookbacks)
    if len(weights) != len(lookbacks):
        raise ValueError("lookbacks와 weights의 길이가 달라야 합니다")

    scores = pd.Series(0.0, index=history.columns, dtype=float)
    weight_used = pd.Series(0.0, index=history.columns, dtype=float)
    for lb, wt in zip(lookbacks, weights):
        if len(history) <= lb:
            continue
        window = history.iloc[-(lb + 1):]
        start, end = window.iloc[0], window.iloc[-1]
        ret = (end / start - 1.0).where(start > 0, np.nan)
        valid = ret.notna()
        scores[valid] += ret[valid] * wt
        weight_used[valid] += wt

    scores = scores.where(weight_used > 0, np.nan)
    return scores / weight_used.replace(0.0, np.nan)
