"""로컬 파일 캐시.

같은 구간을 반복 내려받으면 느리고, 데이터 제공처에서 차단당하기도 한다.
CSV로 두는 이유는 추가 의존성 없이 사람이 직접 열어 확인할 수 있어서다.
"""

from __future__ import annotations

import pathlib

import pandas as pd


class PriceCache:
    """티커별 일봉 CSV 캐시."""

    def __init__(self, root: str | pathlib.Path = "data/cache") -> None:
        self.root = pathlib.Path(root)

    def _path(self, provider: str, ticker: str) -> pathlib.Path:
        # 티커에 '/'(예: 'USD/KRW')나 '^'가 들어갈 수 있어 파일명으로 정규화한다.
        from .csv_source import safe_filename

        return self.root / provider / f"{safe_filename(ticker)}.csv"

    def read(self, provider: str, ticker: str) -> pd.DataFrame | None:
        path = self._path(provider, ticker)
        if not path.exists():
            return None
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        return df.sort_index()

    def write(self, provider: str, ticker: str, df: pd.DataFrame) -> None:
        path = self._path(provider, ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.sort_index().to_csv(path)

    def merge(self, provider: str, ticker: str, fresh: pd.DataFrame) -> pd.DataFrame:
        """기존 캐시와 새 데이터를 합친다. 겹치는 날짜는 새 데이터를 우선한다."""
        old = self.read(provider, ticker)
        if old is None or old.empty:
            merged = fresh
        else:
            merged = pd.concat([old[~old.index.isin(fresh.index)], fresh])
        merged = merged.sort_index()
        self.write(provider, ticker, merged)
        return merged

    def covers(self, provider: str, ticker: str, start: str, end: str | None) -> bool:
        """요청 구간이 캐시에 이미 들어 있는가."""
        df = self.read(provider, ticker)
        if df is None or df.empty:
            return False
        if df.index.min() > pd.Timestamp(start):
            return False
        if end is not None and df.index.max() < pd.Timestamp(end):
            return False
        return True
