#!/usr/bin/env python3
"""네트워크 없이 프로그램을 시험해볼 합성 가격 데이터를 만든다.

    python scripts/make_sample_data.py --out data/csv

실제 시장 데이터가 아니다. 파이프라인이 도는지 확인하는 용도이며,
이 데이터로 나온 성과는 아무 의미가 없다.
"""

from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd

# (티커, 연수익률, 연변동성, 설명)
ASSETS = [
    ("SPY", 0.09, 0.16, "미국 대형주"),
    ("QQQ", 0.12, 0.22, "미국 기술주"),
    ("TLT", 0.03, 0.13, "미국 장기국채"),
    ("GLD", 0.05, 0.15, "금"),
    ("069500", 0.06, 0.18, "국내 대형주"),
    ("114260", 0.025, 0.03, "국내 단기국채"),
]


def build(years: int, seed: int) -> dict[str, pd.DataFrame]:
    rs = np.random.RandomState(seed)
    n = years * 252
    index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)

    # 자산 간 상관관계를 넣는다. 전부 독립이면 분산투자 효과가 비현실적으로 커진다.
    market = rs.normal(0, 1, n)
    frames = {}
    for i, (ticker, mu, sigma, _) in enumerate(ASSETS):
        beta = 0.7 if i < 2 else (-0.2 if "TLT" in ticker else 0.1)
        noise = rs.normal(0, 1, n)
        shocks = beta * market + np.sqrt(max(1 - beta**2, 0.05)) * noise
        daily = mu / 252 + sigma / np.sqrt(252) * shocks
        close = 100 * np.cumprod(1 + daily)
        open_ = close * (1 + rs.normal(0, 0.001, n))
        frames[ticker] = pd.DataFrame(
            {"open": open_, "close": close, "volume": rs.randint(1e5, 1e7, n)}, index=index
        )

    # 환율. 달러 자산을 원화로 환산하려면 필요하다.
    fx = 1200 * np.cumprod(1 + rs.normal(0.00005, 0.004, n))
    frames["USD_KRW"] = pd.DataFrame({"open": fx, "close": fx}, index=index)
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description="샘플 가격 데이터 생성")
    parser.add_argument("--out", default="data/csv", help="CSV 저장 디렉터리")
    parser.add_argument("--years", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20240101)
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for ticker, df in build(args.years, args.seed).items():
        df.to_csv(out / f"{ticker}.csv")

    print(f"{len(ASSETS)}개 종목 + 환율 × {args.years}년치 합성 데이터를 {out}에 저장했습니다.")
    print("\n다음으로:")
    print("  cp config/portfolio.sample.yaml config/portfolio.yaml")
    print("  stockbot backtest -c config/portfolio.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
