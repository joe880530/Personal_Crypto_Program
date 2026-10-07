"""CSV/DataFrame 기반 오프라인 소스.

테스트와 재현 가능한 백테스트에 쓴다. 네트워크에 의존하지 않기 때문에
"어제와 같은 데이터로 돌렸는데 결과가 다르다" 같은 문제를 피할 수 있다.
"""

from __future__ import annotations

import pathlib

import pandas as pd

from .base import PriceData, PriceProvider


def safe_filename(ticker: str) -> str:
    """티커를 파일명으로 쓸 수 있게 만든다. 'USD/KRW' -> 'USD_KRW'."""
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in ticker)


class CsvProvider(PriceProvider):
    """디렉터리 안의 `{티커}.csv`를 읽는다.

    각 CSV는 날짜 index와 최소한 `close` 컬럼을 가져야 한다(`open`, `volume` 선택).
    `USD/KRW`처럼 파일명에 쓸 수 없는 문자가 든 티커는 `USD_KRW.csv`로 찾는다.
    """

    name = "csv"

    def __init__(self, root: str | pathlib.Path) -> None:
        self.root = pathlib.Path(root)

    def _resolve(self, ticker: str) -> pathlib.Path | None:
        for candidate in (self.root / f"{ticker}.csv", self.root / f"{safe_filename(ticker)}.csv"):
            if candidate.exists():
                return candidate
        return None

    def fetch(self, tickers: list[str], start: str, end: str | None = None) -> PriceData:
        closes, opens, volumes = {}, {}, {}
        for ticker in tickers:
            path = self._resolve(ticker)
            if path is None:
                continue
            df = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
            df.columns = [c.lower() for c in df.columns]
            if "close" not in df.columns:
                raise ValueError(f"{path}에 'close' 컬럼이 없습니다")
            closes[ticker] = df["close"]
            if "open" in df.columns:
                opens[ticker] = df["open"]
            if "volume" in df.columns:
                volumes[ticker] = df["volume"]
        return _assemble(closes, opens, volumes).slice(start, end)


class FrameProvider(PriceProvider):
    """이미 메모리에 있는 DataFrame을 그대로 쓰는 소스(테스트용)."""

    name = "frame"

    def __init__(
        self,
        close: pd.DataFrame,
        open: pd.DataFrame | None = None,
        volume: pd.DataFrame | None = None,
    ) -> None:
        self._data = PriceData(close=close, open=open, volume=volume)

    def fetch(self, tickers: list[str], start: str, end: str | None = None) -> PriceData:
        keep = [t for t in tickers if t in self._data.close.columns]

        def pick(df):
            if df is None:
                return None
            return df[[c for c in keep if c in df.columns]]

        return PriceData(
            close=self._data.close[keep], open=pick(self._data.open), volume=pick(self._data.volume)
        ).slice(start, end)


def _assemble(closes: dict, opens: dict, volumes: dict) -> PriceData:
    return PriceData(
        close=pd.DataFrame(closes).sort_index(),
        open=pd.DataFrame(opens).sort_index() if opens else None,
        volume=pd.DataFrame(volumes).sort_index() if volumes else None,
    )
