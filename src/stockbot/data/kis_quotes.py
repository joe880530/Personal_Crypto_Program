"""한국투자증권 분봉 시세.

일중 전략은 분 단위 데이터 없이는 검증도 운용도 안 된다. 엔드포인트와 TR ID는
한국투자증권 공식 예제(github.com/koreainvestment/open-trading-api)에서 확인했다.

    당일 분봉   GET /uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice
                FHKST03010200 · **당일만** · 1회 30건
    과거 분봉   GET /uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice
                FHKST03010230 · 최대 1년 보관 · 1회 120건
    휴장일      GET /uapi/domestic-stock/v1/quotations/chk-holiday
                CTCA0903R · 공식 문서가 **1일 1회** 호출을 권고한다

**모의계좌 주의**: 과거 분봉(FHKST03010230) 문서에는 "실전계좌의 경우"라고만
적혀 있고 모의계좌 지원 여부가 분명하지 않다. 실제로 확인하기 전까지 된다고
가정하지 말 것. `stockbot minutes --probe`가 그것을 확인하는 명령이다.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import time
from typing import Callable

import pandas as pd

from ..execution.kis import KISBroker, KISError

TODAY_CHART_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
DAILY_CHART_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
HOLIDAY_PATH = "/uapi/domestic-stock/v1/quotations/chk-holiday"

TODAY_CHART_TR = "FHKST03010200"
DAILY_CHART_TR = "FHKST03010230"
HOLIDAY_TR = "CTCA0903R"

#: 조건 시장 분류 코드. J=KRX.
MARKET_KRX = "J"

#: 응답 한 건에서 읽는 필드. 이름이 바뀌면 조용히 0이 되는 대신 무엇이 왔는지
#: 보여주며 실패해야 하므로 한곳에 모아 둔다.
F_DATE = "stck_bsop_date"   # 영업일자 YYYYMMDD
F_TIME = "stck_cntg_hour"   # 체결시간 HHMMSS
F_OPEN = "stck_oprc"
F_HIGH = "stck_hgpr"
F_LOW = "stck_lwpr"
F_CLOSE = "stck_prpr"
F_VOLUME = "cntg_vol"

#: KRX 정규장. 장 시작/마감 판단과 분봉 페이징의 기준.
SESSION_OPEN = dt.time(9, 0)
SESSION_CLOSE = dt.time(15, 30)

#: 한 번에 받을 수 있는 분봉 수(공식 문서 기준).
DAILY_CHART_PAGE = 120

#: 호출 간격(초). 공식 예제는 실전 0.05, 그 외 0.5를 쓴다. 모의투자는 초당
#: 건수 제한이 더 빡빡해서(EGW00201) 넉넉한 쪽을 기본으로 둔다.
PACE_REAL = 0.05
PACE_PAPER = 0.5


class QuoteError(KISError):
    """시세 조회 실패."""


@dataclasses.dataclass
class KISQuotes:
    """분봉과 휴장일 조회.

    인증·토큰·오류판정은 KISBroker가 이미 갖고 있으므로 그대로 빌려 쓴다.
    여기서 따로 구현하면 두 벌이 생기고 한쪽만 고치는 일이 생긴다.
    """

    broker: KISBroker
    pace: float | None = None
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.pace is None:
            self.pace = PACE_PAPER if self.broker.credentials.paper else PACE_REAL

    # ------------------------------------------------------------ 분봉
    def today_minutes(self, ticker: str, until: dt.time | None = None) -> pd.DataFrame:
        """당일 분봉. 최대 30건이라 오늘 흐름을 보는 용도다."""
        payload = self.broker.call(
            "GET", TODAY_CHART_PATH, TODAY_CHART_TR,
            params={
                "FID_COND_MRKT_DIV_CODE": MARKET_KRX,
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_HOUR_1": _hhmmss(until or SESSION_CLOSE),
                "FID_PW_DATA_INCU_YN": "N",
                "FID_ETC_CLS_CODE": "",
            },
        )
        return _to_frame(payload.get("output2") or [], ticker)

    def day_minutes(self, ticker: str, day: dt.date) -> pd.DataFrame:
        """하루치 분봉 전체. 1회 120건이라 뒤에서 앞으로 되짚어 모은다.

        API는 FID_INPUT_HOUR_1 **이전** 구간을 돌려준다. 그래서 장 마감에서
        시작해 받은 것 중 가장 이른 시각의 1분 앞으로 옮겨가며 반복한다.

        **받은 것이 정말 그날 것인지 확인한다.** 휴장일을 물으면 KIS는 빈 응답이
        아니라 **직전 거래일의 분봉**을 돌려준다. 그대로 믿으면 두 가지가 한꺼번에
        어긋난다. 그날 것을 못 받았는데 받았다고 여겨 '빈 날' 기록을 남기지 않아
        매번 다시 묻게 되고, 저장하는 값은 엉뚱한 날짜의 것이 된다. 실제로
        069500에서 38일을 받았다고 하고 18일만 저장됐다(나머지 20일이 휴장일).
        """
        frames: list[pd.DataFrame] = []
        anchor = SESSION_CLOSE
        seen: set[pd.Timestamp] = set()

        while True:
            payload = self.broker.call(
                "GET", DAILY_CHART_PATH, DAILY_CHART_TR,
                params={
                    "FID_COND_MRKT_DIV_CODE": MARKET_KRX,
                    "FID_INPUT_ISCD": ticker,
                    "FID_INPUT_HOUR_1": _hhmmss(anchor),
                    "FID_INPUT_DATE_1": day.strftime("%Y%m%d"),
                    "FID_PW_DATA_INCU_YN": "N",
                    "FID_FAKE_TICK_INCU_YN": "",
                },
            )
            rows = payload.get("output2") or []
            page = _to_frame(rows, ticker)
            page = page[page.index.date == day]     # 물어본 날 것만 남긴다
            if page.empty:
                # 그날 분봉이 없다. 휴장일이거나 보관 기간(1년)을 넘긴 날이다.
                # 여기서 빈 표를 돌려줘야 보관소가 '빈 날'로 기록하고 다시 묻지 않는다.
                break

            fresh = page[~page.index.isin(seen)]
            if fresh.empty:
                # 같은 구간이 다시 왔다. 더 과거가 없다는 뜻이므로 멈춘다.
                # 이 방어가 없으면 장 시작 시각에서 무한히 맴돈다.
                break
            frames.append(fresh)
            seen.update(fresh.index)

            earliest = fresh.index.min()
            if earliest.time() <= SESSION_OPEN or len(rows) < DAILY_CHART_PAGE:
                break
            anchor = (earliest - pd.Timedelta(minutes=1)).time()
            self.sleep(self.pace)

        if not frames:
            return _empty_frame()
        out = pd.concat(frames).sort_index()
        return out[~out.index.duplicated(keep="last")]

    def probe_history(self, ticker: str, day: dt.date) -> dict:
        """과거 분봉이 **이 계좌에서** 실제로 되는지 한 번만 물어본다.

        공식 문서가 "실전계좌의 경우"라고만 적고 있어 모의계좌 지원 여부가
        불분명하다. 백테스트 전체가 여기에 걸려 있으므로, 만들기 전에 확인한다.
        """
        try:
            payload = self.broker.call(
                "GET", DAILY_CHART_PATH, DAILY_CHART_TR,
                params={
                    "FID_COND_MRKT_DIV_CODE": MARKET_KRX,
                    "FID_INPUT_ISCD": ticker,
                    "FID_INPUT_HOUR_1": _hhmmss(SESSION_CLOSE),
                    "FID_INPUT_DATE_1": day.strftime("%Y%m%d"),
                    "FID_PW_DATA_INCU_YN": "N",
                    "FID_FAKE_TICK_INCU_YN": "",
                },
            )
        except KISError as exc:
            return {"ok": False, "reason": str(exc), "bars": 0}

        rows = payload.get("output2") or []
        if not rows:
            return {"ok": False, "reason": "응답은 성공인데 분봉이 0건입니다", "bars": 0}
        frame = _to_frame(rows, ticker)
        return {
            "ok": True,
            "bars": len(frame),
            "first": frame.index.min(),
            "last": frame.index.max(),
        }

    # ------------------------------------------------------------ 휴장일
    def holidays(self, since: dt.date) -> dict[dt.date, bool]:
        """기준일부터의 {날짜: 개장여부}.

        공식 문서가 **1일 1회** 호출을 권고한다(원장 서비스와 연결돼 있다).
        호출한 쪽에서 반드시 캐시할 것 — TradingCalendar가 그 역할을 한다.
        """
        payload = self.broker.call(
            "GET", HOLIDAY_PATH, HOLIDAY_TR,
            params={"BASS_DT": since.strftime("%Y%m%d"), "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
        )
        out: dict[dt.date, bool] = {}
        for row in payload.get("output") or []:
            raw = row.get("bass_dt")
            if not raw:
                continue
            out[dt.datetime.strptime(raw, "%Y%m%d").date()] = str(row.get("opnd_yn", "")).upper() == "Y"
        if not out:
            raise QuoteError(
                "휴장일 응답을 읽지 못했습니다. 받은 키: "
                f"{sorted((payload.get('output') or [{}])[0])}"
            )
        return out


def _hhmmss(value: dt.time) -> str:
    return value.strftime("%H%M%S")


def _empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
    frame.index = pd.DatetimeIndex([], name="datetime")
    return frame


def _to_frame(rows: list[dict], ticker: str) -> pd.DataFrame:
    """응답 배열을 OHLCV 표로. 필드가 없으면 0으로 때우지 않고 실패한다."""
    if not rows:
        return _empty_frame()

    required = (F_DATE, F_TIME, F_OPEN, F_HIGH, F_LOW, F_CLOSE)
    missing = [f for f in required if f not in rows[0]]
    if missing:
        raise QuoteError(
            f"{ticker} 분봉 응답에 필요한 필드가 없습니다: {missing}\n"
            f"  받은 필드: {sorted(rows[0])}\n"
            "  KIS가 응답 형식을 바꿨을 수 있습니다."
        )

    index, records = [], []
    for row in rows:
        stamp = f"{row[F_DATE]}{str(row[F_TIME]).zfill(6)}"
        index.append(pd.Timestamp(dt.datetime.strptime(stamp, "%Y%m%d%H%M%S")))
        records.append({
            "open": float(row[F_OPEN]),
            "high": float(row[F_HIGH]),
            "low": float(row[F_LOW]),
            "close": float(row[F_CLOSE]),
            "volume": float(row.get(F_VOLUME) or 0.0),
        })
    frame = pd.DataFrame(records, index=pd.DatetimeIndex(index, name="datetime"))
    return frame.sort_index()
