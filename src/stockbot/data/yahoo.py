"""미국 주식/ETF — yfinance.

수정주가(auto_adjust)를 쓴다. 배당을 빼고 백테스트하면 장기 수익률이
실제보다 크게 낮아져 전략 비교 자체가 왜곡된다.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from .base import PriceData, PriceProvider
from .cache import PriceCache


class YahooProvider(PriceProvider):
    """yfinance 기반 일봉 제공자.

    Args:
        cache: 지정 시 내려받은 데이터를 CSV로 캐시한다.
        auto_adjust: 배당·분할 반영 여부.
    """

    name = "yahoo"

    def __init__(self, cache: PriceCache | None = None, auto_adjust: bool = True) -> None:
        self.cache = cache
        self.auto_adjust = auto_adjust

    def fetch(self, tickers: list[str], start: str, end: str | None = None) -> PriceData:
        if not tickers:
            return PriceData(close=pd.DataFrame())

        end = end or dt.date.today().isoformat()
        need = list(tickers)
        cached: dict[str, pd.DataFrame] = {}

        if self.cache is not None:
            still_need = []
            for ticker in tickers:
                if self.cache.covers(self.name, ticker, start, end):
                    df = self.cache.read(self.name, ticker)
                    if df is not None:
                        cached[ticker] = df
                        continue
                still_need.append(ticker)
            need = still_need

        downloaded = self._download(need, start, end) if need else {}
        if self.cache is not None:
            for ticker, df in downloaded.items():
                cached[ticker] = self.cache.merge(self.name, ticker, df)
        else:
            cached.update(downloaded)

        closes = {t: df["close"] for t, df in cached.items() if "close" in df}
        opens = {t: df["open"] for t, df in cached.items() if "open" in df}
        volumes = {t: df["volume"] for t, df in cached.items() if "volume" in df}
        data = PriceData(
            close=pd.DataFrame(closes).sort_index(),
            open=pd.DataFrame(opens).sort_index() if opens else None,
            volume=pd.DataFrame(volumes).sort_index() if volumes else None,
        )
        return data.slice(start, end)

    def _download(self, tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - 환경 의존
            raise ImportError(
                "미국 시장 데이터에는 yfinance가 필요합니다: pip install yfinance"
            ) from exc

        raw = yf.download(
            tickers=" ".join(tickers),
            start=start,
            end=end,
            auto_adjust=self.auto_adjust,
            progress=False,
            group_by="ticker",
            threads=True,
        )
        if raw is None or raw.empty:
            return {}

        out: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            if isinstance(raw.columns, pd.MultiIndex):
                if ticker not in raw.columns.get_level_values(0):
                    continue
                df = raw[ticker]
            else:
                df = raw  # 단일 종목 조회 시엔 평면 컬럼으로 온다
            df = df.rename(columns=str.lower)[["open", "close", "volume"]].dropna(how="all")
            if not df.empty:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                out[ticker] = df
        return out
