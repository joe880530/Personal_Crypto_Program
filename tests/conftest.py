"""테스트 공통 픽스처. 네트워크 없이 재현 가능한 합성 가격을 쓴다."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def make_prices(n: int = 800, seed: int = 42) -> pd.DataFrame:
    """성격이 뚜렷이 다른 4개 자산의 합성 일봉.

    - GROW: 고수익·고변동 (주식형)
    - STEADY: 저수익·저변동 (채권형)
    - CYCLE: 추세가 중간에 뒤집히는 자산 (모멘텀 검증용)
    - FLAT: 거의 움직이지 않는 자산
    """
    rs = np.random.RandomState(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)
    half = n // 2
    cycle_mu = np.concatenate([np.full(half, 0.0015), np.full(n - half, -0.0015)])
    data = {
        "GROW": 100 * np.cumprod(1 + rs.normal(0.0006, 0.013, n)),
        "STEADY": 100 * np.cumprod(1 + rs.normal(0.0001, 0.003, n)),
        "CYCLE": 100 * np.cumprod(1 + rs.normal(0, 0.010, n) + cycle_mu),
        "FLAT": 100 * np.cumprod(1 + rs.normal(0.0, 0.001, n)),
    }
    return pd.DataFrame(data, index=idx)


@pytest.fixture
def prices() -> pd.DataFrame:
    return make_prices()


@pytest.fixture
def short_prices() -> pd.DataFrame:
    return make_prices(n=300, seed=7)
