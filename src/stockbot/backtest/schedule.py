"""리밸런싱 시점 결정."""

from __future__ import annotations

import pandas as pd

_FREQ_ALIASES = {
    "D": "D",
    "DAILY": "D",
    "W": "W",
    "WEEKLY": "W",
    "M": "M",
    "MONTHLY": "M",
    "Q": "Q",
    "QUARTERLY": "Q",
    "Y": "Y",
    "YEARLY": "Y",
    "A": "Y",
    "NEVER": "NEVER",
}

_PERIOD_FREQ = {"W": "W", "M": "M", "Q": "Q", "Y": "Y"}


def normalize_freq(freq: str) -> str:
    key = str(freq).strip().upper()
    if key not in _FREQ_ALIASES:
        raise ValueError(f"지원하지 않는 리밸런싱 주기: {freq!r}. 사용 가능: {sorted(set(_FREQ_ALIASES))}")
    return _FREQ_ALIASES[key]


def rebalance_flags(index: pd.DatetimeIndex, freq: str) -> pd.Series:
    """각 봉이 리밸런싱 *신호* 시점인지 표시한다.

    신호는 각 기간의 **마지막 거래일**에 나온다. 실제 체결은 엔진이 다음 봉으로
    미루므로 미래 정보를 쓰지 않는다.
    """
    f = normalize_freq(freq)
    if f == "NEVER":
        flags = pd.Series(False, index=index)
        if len(index):
            flags.iloc[0] = True  # 최초 1회만 매수 후 보유
        return flags
    if f == "D":
        return pd.Series(True, index=index)

    periods = index.to_period(_PERIOD_FREQ[f])
    # 다음 봉이 다른 기간이면 이번 봉이 그 기간의 마지막 거래일이다.
    is_last = pd.Series(periods != periods.to_series().shift(-1).to_numpy(), index=index)
    is_last.iloc[-1] = True
    return is_last


def needs_rebalance(
    current: pd.Series, target: pd.Series, band: float = 0.0
) -> bool:
    """밴드 리밸런싱 판정.

    band=0이면 예정된 시점마다 무조건 실행한다. band>0이면 어떤 종목이든
    목표 비중과의 절대 괴리가 band를 넘을 때만 실행한다. 밴드는 불필요한
    거래를 줄여 비용을 아끼는 대신 추적오차를 감수하는 장치다.
    """
    if band <= 0:
        return True
    idx = current.index.union(target.index)
    diff = (target.reindex(idx).fillna(0.0) - current.reindex(idx).fillna(0.0)).abs()
    return bool(diff.max() > band)
