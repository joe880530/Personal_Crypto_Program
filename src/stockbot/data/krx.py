"""국내 주식/ETF — FinanceDataReader.

티커는 6자리 종목코드를 쓴다(예: 삼성전자 '005930', KODEX 200 '069500').
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from .base import PriceData, PriceProvider
from .cache import PriceCache


class KrxProvider(PriceProvider):
    """FinanceDataReader 기반 국내 일봉 제공자."""

    name = "krx"

    def __init__(self, cache: PriceCache | None = None) -> None:
        self.cache = cache

    def fetch(self, tickers: list[str], start: str, end: str | None = None) -> PriceData:
        if not tickers:
            return PriceData(close=pd.DataFrame())
        end = end or dt.date.today().isoformat()

        frames: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            if self.cache is not None and self.cache.covers(self.name, ticker, start, end):
                df = self.cache.read(self.name, ticker)
                if df is not None:
                    frames[ticker] = df
                    continue
            df = self._download(ticker, start, end)
            if df is None or df.empty:
                continue
            frames[ticker] = self.cache.merge(self.name, ticker, df) if self.cache else df

        closes = {t: df["close"] for t, df in frames.items() if "close" in df}
        opens = {t: df["open"] for t, df in frames.items() if "open" in df}
        volumes = {t: df["volume"] for t, df in frames.items() if "volume" in df}
        return PriceData(
            close=pd.DataFrame(closes).sort_index(),
            open=pd.DataFrame(opens).sort_index() if opens else None,
            volume=pd.DataFrame(volumes).sort_index() if volumes else None,
        ).slice(start, end)

    def _download(self, ticker: str, start: str, end: str) -> pd.DataFrame | None:
        try:
            import FinanceDataReader as fdr
        except ImportError as exc:  # pragma: no cover - 환경 의존
            raise ImportError(
                "국내 시장 데이터에는 finance-datareader가 필요합니다: "
                "pip install finance-datareader"
            ) from exc

        df = fdr.DataReader(ticker, start, end)
        if df is None or df.empty:
            return None
        df = df.rename(columns=str.lower)
        cols = [c for c in ("open", "close", "volume") if c in df.columns]
        df = df[cols].dropna(how="all")
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df


class FxProvider(PriceProvider):
    """환율 제공자. 국내+해외 혼합 포트폴리오의 통화 통일에 쓴다."""

    name = "fx"

    def __init__(self, cache: PriceCache | None = None) -> None:
        self.cache = cache

    def fetch(self, tickers: list[str], start: str, end: str | None = None) -> PriceData:
        """tickers는 'USD/KRW' 형식의 통화쌍."""
        try:
            import FinanceDataReader as fdr
        except ImportError as exc:  # pragma: no cover - 환경 의존
            raise ImportError(
                "환율 조회에는 finance-datareader가 필요합니다: pip install finance-datareader"
            ) from exc

        end = end or dt.date.today().isoformat()
        closes = {}
        for pair in tickers:
            if self.cache is not None and self.cache.covers(self.name, pair, start, end):
                cached = self.cache.read(self.name, pair)
                if cached is not None:
                    closes[pair] = cached["close"]
                    continue
            df = fdr.DataReader(pair, start, end)
            if df is None or df.empty:
                continue
            df = df.rename(columns=str.lower)
            df.index = pd.to_datetime(df.index).tz_localize(None)
            frame = df[["close"]]
            if self.cache is not None:
                frame = self.cache.merge(self.name, pair, frame)
            closes[pair] = frame["close"]
        return PriceData(close=pd.DataFrame(closes).sort_index()).slice(start, end)
