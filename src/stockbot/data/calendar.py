"""거래일과 장 운영시간.

무인 운용에서 이게 없으면 휴장일에도 깨어나 "데이터가 없다"며 실패하고, 그
실패 메일이 매번 오면 사람은 곧 메일을 안 보게 된다. 진짜 고장과 그냥 쉬는 날을
프로그램이 구분해야 한다.

휴장일은 KIS 조회로 알아내되 **파일에 캐시한다**. 공식 문서가 1일 1회 호출을
권고하기 때문이다(원장 서비스와 연결돼 있다).
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

from ..execution.kis import KISError
from .kis_quotes import SESSION_CLOSE, SESSION_OPEN, KISQuotes

#: 장이 열리지 않는 요일. 주말은 물어볼 것도 없으므로 호출을 아낀다.
_WEEKEND = {5, 6}


class TradingCalendar:
    """거래일 판단. 조회 결과는 파일에 남겨 재사용한다.

    Args:
        quotes: 휴장일을 물어볼 대상. None이면 주말만 걸러낸다(공휴일 모름).
        cache_path: 조회 결과를 남길 파일.
    """

    def __init__(self, quotes: KISQuotes | None = None,
                 cache_path: str = ".state/trading_days.json") -> None:
        self.quotes = quotes
        self.path = pathlib.Path(cache_path)
        self._days: dict[dt.date, bool] = {}
        #: 휴장일 조회가 이 계좌에서 되는가. 모의계좌는 이 TR을 지원하지 않는다
        #: ("모의투자 TR 이 아닙니다", HTTP 500). 한 번 거부당하면 다시 묻지 않는다.
        self._holiday_api: bool | None = None if quotes else False
        self.holiday_error: str | None = None
        self._load()

    # ------------------------------------------------------------ 캐시
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for key, value in raw.items():
            try:
                self._days[dt.date.fromisoformat(key)] = bool(value)
            except ValueError:
                continue  # 손상된 줄은 버리고 다시 받는다

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({d.isoformat(): v for d, v in sorted(self._days.items())}),
                encoding="utf-8",
            )
        except OSError:
            pass  # 캐시 실패는 치명적이지 않다. 다음에 다시 물어보면 된다.

    # ------------------------------------------------------------ 판단
    def is_trading_day(self, day: dt.date) -> bool:
        """그날 장이 열리는가."""
        if day.weekday() in _WEEKEND:
            return False
        if day in self._days:
            return self._days[day]
        if self.quotes is None or self._holiday_api is False:
            # 공휴일을 모르는 채로 "열린다"고 답한다. 조용히 틀리지 않도록
            # knows_holidays()가 False를 돌려주어 호출한 쪽이 알 수 있게 한다.
            return True
        try:
            fetched = self.quotes.holidays(day)
        except KISError as exc:
            # 휴장일을 모른다고 해서 달력 전체가 못 쓸 것은 아니다. 주말만
            # 거르는 쪽으로 물러선다. 여기서 예외를 올려보내면 곁다리 기능이
            # 정작 하려던 일(분봉 확인, 주문)을 통째로 막는다.
            self._holiday_api = False
            self.holiday_error = str(exc)
            return True
        self._holiday_api = True
        self._days.update(fetched)
        self._save()
        return self._days.get(day, True)

    def knows_holidays(self) -> bool:
        """공휴일까지 반영된 판단인가. False면 주말만 거른 것이다.

        한 번이라도 물어보기 전에는 알 수 없으므로, 확인을 강제하지 않는다.
        호출한 쪽은 is_trading_day를 한 번 부른 뒤에 이 값을 읽어야 한다.
        """
        return self._holiday_api is True

    def session(self, day: dt.date) -> tuple[dt.datetime, dt.datetime]:
        """그날의 정규장 시작·종료 시각."""
        return (
            dt.datetime.combine(day, SESSION_OPEN),
            dt.datetime.combine(day, SESSION_CLOSE),
        )

    def phase(self, now: dt.datetime) -> str:
        """지금이 장의 어느 국면인가.

        Returns:
            'holiday' 휴장일 / 'before_open' 개장 전 /
            'open' 장중 / 'after_close' 마감 후
        """
        if not self.is_trading_day(now.date()):
            return "holiday"
        start, end = self.session(now.date())
        if now < start:
            return "before_open"
        if now > end:
            return "after_close"
        return "open"

    def is_open(self, now: dt.datetime) -> bool:
        return self.phase(now) == "open"

    def previous_trading_day(self, day: dt.date, limit: int = 15) -> dt.date:
        """직전 거래일. 변동성 돌파처럼 '전일'이 필요한 전략이 쓴다."""
        probe = day - dt.timedelta(days=1)
        for _ in range(limit):
            if self.is_trading_day(probe):
                return probe
            probe -= dt.timedelta(days=1)
        raise ValueError(f"{day} 이전 {limit}일 안에 거래일이 없습니다")

    def trading_days(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """[start, end] 안의 거래일 목록."""
        out, probe = [], start
        while probe <= end:
            if self.is_trading_day(probe):
                out.append(probe)
            probe += dt.timedelta(days=1)
        return out
