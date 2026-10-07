"""가격 데이터 소스 인터페이스."""

from __future__ import annotations

import abc
import dataclasses

import pandas as pd


@dataclasses.dataclass
class PriceData:
    """가격 묶음.

    Attributes:
        close: 종가(가능하면 배당·분할 반영 수정주가). 평가와 신호 계산의 기준.
        open: 시가. 익일 시가 체결 백테스트에 쓴다. 없으면 None.
        volume: 거래량. 유동성 필터에 쓴다.
    """

    close: pd.DataFrame
    open: pd.DataFrame | None = None
    volume: pd.DataFrame | None = None

    @property
    def tickers(self) -> list[str]:
        return list(self.close.columns)

    def slice(self, start: str | None = None, end: str | None = None) -> "PriceData":
        def cut(df):
            return None if df is None else df.loc[start:end]

        return PriceData(close=self.close.loc[start:end], open=cut(self.open), volume=cut(self.volume))

    def dropna_tickers(self, min_coverage: float = 0.9) -> "PriceData":
        """데이터가 너무 비어 있는 종목을 제거한다(상장 전 구간 등)."""
        coverage = self.close.notna().mean()
        keep = list(coverage[coverage >= min_coverage].index)

        def pick(df):
            return None if df is None else df[[c for c in keep if c in df.columns]]

        return PriceData(close=self.close[keep], open=pick(self.open), volume=pick(self.volume))


class PriceProvider(abc.ABC):
    """시장별 가격 조회기."""

    name: str = "provider"

    @abc.abstractmethod
    def fetch(self, tickers: list[str], start: str, end: str | None = None) -> PriceData:
        """[start, end] 구간의 일봉을 가져온다."""

    def latest_prices(self, tickers: list[str]) -> dict[str, float]:
        """가장 최근 종가. 실시간 호가가 필요하면 브로커 API를 쓸 것."""
        import datetime as dt

        start = (dt.date.today() - dt.timedelta(days=14)).isoformat()
        data = self.fetch(tickers, start)
        if data.close.empty:
            return {}
        last = data.close.ffill().iloc[-1]
        return {t: float(v) for t, v in last.items() if pd.notna(v)}
