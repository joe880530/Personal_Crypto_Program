"""분봉 보관소.

KIS는 분봉을 **1년만** 보관한다. 지금부터 매일 모아두면 1년 뒤에는 2년치가 되고,
그때야 워크포워드 구간이 충분해져 일중 전략을 제대로 판별할 수 있다. 지금 1년치로
재면 구간이 2~3개뿐이라 과최적화를 가려낼 수 없다(월간 검증은 9개 구간으로도
학습↔검증 상관이 -0.109였다).

저장은 PriceCache를 그대로 쓴다. 형식이 같아 두 벌이 되지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pandas as pd

from .cache import PriceCache

PROVIDER = "krx_minute"

COLUMNS = ["open", "high", "low", "close", "volume"]


class MinuteStore:
    """종목별 분봉을 쌓아 둔다.

    데이터가 없던 날(휴장일 등)도 기억한다. 안 그러면 매번 다시 물어보게 되고,
    1년치를 채우는 동안 헛호출이 수백 번 쌓인다.
    """

    def __init__(self, root: str = "data/minutes") -> None:
        self.root = pathlib.Path(root)
        self.cache = PriceCache(str(self.root))

    # ------------------------------------------------------------ 읽기
    def read(self, ticker: str) -> pd.DataFrame:
        frame = self.cache.read(PROVIDER, ticker)
        return _empty() if frame is None or frame.empty else frame.sort_index()

    def stored_days(self, ticker: str) -> set[dt.date]:
        """이미 받아 둔 날짜. 빈 날(휴장 등)도 포함해 다시 묻지 않는다."""
        frame = self.read(ticker)
        days = set(frame.index.normalize().date) if not frame.empty else set()
        return days | self._empty_days(ticker)

    def span(self, ticker: str) -> tuple[dt.date, dt.date] | None:
        frame = self.read(ticker)
        if frame.empty:
            return None
        return frame.index.min().date(), frame.index.max().date()

    # ------------------------------------------------------------ 쓰기
    def save(self, ticker: str, day: dt.date, bars: pd.DataFrame) -> int:
        """하루치를 저장한다. 0건이면 '그날은 없음'으로 기록한다."""
        if bars is None or bars.empty:
            self._mark_empty(ticker, day)
            return 0
        missing = [c for c in COLUMNS if c not in bars.columns]
        if missing:
            raise ValueError(f"{ticker} {day} 분봉에 {missing} 열이 없습니다")
        self.cache.merge(PROVIDER, ticker, bars[COLUMNS])
        return len(bars)

    # ------------------------------------------------------------ 빈 날 기록
    def _empty_path(self, ticker: str) -> pathlib.Path:
        from .csv_source import safe_filename

        return self.root / PROVIDER / f"{safe_filename(ticker)}.empty.json"

    def _empty_days(self, ticker: str) -> set[dt.date]:
        try:
            raw = json.loads(self._empty_path(ticker).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        out = set()
        for item in raw:
            try:
                out.add(dt.date.fromisoformat(item))
            except (TypeError, ValueError):
                continue
        return out

    def _mark_empty(self, ticker: str, day: dt.date) -> None:
        days = self._empty_days(ticker) | {day}
        path = self._empty_path(ticker)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(sorted(d.isoformat() for d in days)), encoding="utf-8")
        except OSError:
            pass  # 기록 실패는 치명적이지 않다. 다음에 다시 물어보면 된다.


def _empty() -> pd.DataFrame:
    frame = pd.DataFrame(columns=COLUMNS, dtype=float)
    frame.index = pd.DatetimeIndex([], name="datetime")
    return frame
