"""업비트 시세 조회 (공개 API, 인증 불필요).

엔드포인트와 응답 형식은 **실제 호출로 확인했다**(2026-10-07, api.upbit.com).
기억이나 문서 요약이 아니라 받은 응답을 보고 적은 것이다.

    GET /v1/candles/minutes/{unit}?market=&count=&to=
    GET /v1/candles/days?market=&count=&to=
    GET /v1/market/all?isDetails=true

**확인된 것 세 가지.** 셋 다 추측했으면 틀렸을 것들이다.

1. 종가 필드는 `close`가 아니라 `trade_price`다. `opening_price`/`high_price`/
   `low_price`와 이름 규칙이 다르다.

2. **count는 200이 한도인데, 넘겨 요청해도 오류가 아니라 조용히 200으로
   잘린다.** 500을 달라고 하면 200이 오고 HTTP 200이다. 요청한 개수를
   받았다고 가정하고 페이징하면 구간이 통째로 빈다 — KIS에서 "38일 받았다"고
   하고 18일만 저장된 것과 같은 종류의 사고다. 그래서 여기서는 **요청한 수가
   아니라 받은 것의 시각**을 보고 다음 구간을 정한다.

3. `to`는 UTC 기준이고 그 시각을 **포함하지 않는다**
   (`to=2026-10-01T00:00:00Z` -> 가장 최근 봉이 `2026-09-30T23:59:00`).
   그래서 받은 것 중 가장 이른 시각을 다음 `to`로 그대로 넣으면 겹치지 않는다.

응답은 최신순으로 온다(01:52 다음에 01:51).

호출 한도도 응답 헤더로 확인했다: `remaining-req: group=candles; min=600; sec=9`.
초당 10건, 분당 600건이다. 초당 10건으로 꽉 채우면 분 한도에 걸리므로
간격을 0.12초로 둔다(분당 500건).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

import pandas as pd

from .base import PriceData, PriceProvider

BASE = "https://api.upbit.com/v1"

#: 1회 최대 캔들 수. 더 달라고 해도 오류 없이 여기서 잘린다.
MAX_COUNT = 200

#: 호출 간격(초). 초당 10건이 한도지만 그대로 쓰면 분당 600건이 되어
#: 분 한도(600)에 딱 걸린다. 0.12초면 분당 500건으로 여유가 생긴다.
PACE = 0.12

#: 응답을 기다리는 시간(초).
HTTP_TIMEOUT = 45.0

#: 끊긴 호출을 다시 시도하는 횟수와 대기(초).
RETRIES = 3
BACKOFF = 2.0

#: 응답에서 읽는 필드. 이름이 바뀌면 조용히 0이 되는 대신 무엇이 왔는지
#: 보여주며 실패해야 하므로 한곳에 모아 둔다.
F_TIME_UTC = "candle_date_time_utc"
F_OPEN = "opening_price"
F_HIGH = "high_price"
F_LOW = "low_price"
F_CLOSE = "trade_price"      # **close 가 아니다**
F_VOLUME = "candle_acc_trade_volume"

COLUMNS = ["open", "high", "low", "close", "volume"]


class UpbitError(RuntimeError):
    """업비트 호출 실패."""


class UpbitTransientError(UpbitError):
    """답을 못 받고 끊긴 호출. 서버가 거부한 것과는 다르다.

    시간 초과·연결 끊김·SSL 중단처럼 **응답 자체가 없었던** 경우만 들어온다.
    조회는 다시 물어도 잃을 것이 없으므로 재시도한다.
    """


Transport = Callable[[str], tuple[int, object]]


def _urllib_transport(url: str) -> tuple[int, object]:
    """기본 전송 계층. 표준 라이브러리만 쓴다."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:      # 서버가 답은 했다
        try:
            return exc.code, json.loads(exc.read().decode() or "null")
        except Exception:
            return exc.code, None
    except urllib.error.URLError as exc:
        raise UpbitTransientError(f"업비트에 접속하지 못했습니다: {exc.reason}") from exc
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        # 시간 초과는 URLError가 아니라 TimeoutError로 그냥 올라온다.
        raise UpbitTransientError(f"업비트와의 통신이 끊겼습니다: {exc}") from exc


@dataclasses.dataclass
class UpbitQuotes:
    """업비트 공개 시세.

    인증이 필요 없다. 주문은 별도의 인증 경로를 쓰므로 여기에 넣지 않는다 —
    조회는 다시 물어도 잃을 것이 없지만 주문은 다르고, 한 클래스에 섞어두면
    재시도 규칙이 뒤섞인다.
    """

    transport: Transport = _urllib_transport
    pace: float = PACE
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _last_call: float = dataclasses.field(default=0.0, init=False)

    # ------------------------------------------------------------ 호출
    def _get(self, path: str, params: dict) -> object:
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(RETRIES):
            self._wait_turn()
            try:
                status, payload = self.transport(url)
            except UpbitTransientError:
                if attempt == RETRIES - 1:
                    raise
                self.sleep(BACKOFF * (attempt + 1))
                continue
            if status == 429:
                # 한도 초과. 간격을 지켜도 다른 곳에서 같이 쓰면 걸릴 수 있다.
                if attempt == RETRIES - 1:
                    raise UpbitError(f"{path} 호출 한도에 계속 걸립니다 ({RETRIES}회 시도)")
                self.sleep(BACKOFF * (attempt + 1))
                continue
            if status != 200:
                raise UpbitError(f"{path} 실패 (HTTP {status}): {payload}")
            return payload
        raise AssertionError("unreachable")

    def _wait_turn(self) -> None:
        if self.pace <= 0:
            return
        waited = self.clock() - self._last_call
        if waited < self.pace:
            self.sleep(self.pace - waited)
        self._last_call = self.clock()

    # ------------------------------------------------------------ 캔들
    def candles(self, market: str, unit: int | None = None,
                count: int = MAX_COUNT, to: dt.datetime | None = None) -> pd.DataFrame:
        """캔들 한 묶음. `unit`이 None이면 일봉.

        count는 200을 넘겨도 조용히 200으로 잘린다. 그래서 여기서 미리 자르고,
        몇 건이 **실제로 왔는지**는 호출한 쪽이 결과를 보고 판단하게 둔다.
        """
        path = "/candles/days" if unit is None else f"/candles/minutes/{unit}"
        params: dict = {"market": market, "count": min(int(count), MAX_COUNT)}
        if to is not None:
            params["to"] = _to_param(to)
        rows = self._get(path, params)
        if not isinstance(rows, list):
            raise UpbitError(f"{path} 응답이 목록이 아닙니다: {type(rows).__name__} {rows!r:.200}")
        return _to_frame(rows, market)

    def history(self, market: str, start: dt.datetime, end: dt.datetime | None = None,
                unit: int | None = None) -> pd.DataFrame:
        """[start, end] 구간 전체. 최신에서 과거로 되짚어 모은다.

        끝내는 조건을 **받은 개수가 아니라 받은 시각**으로 판단한다. 요청한
        수만큼 왔는지로 판단하면, 조용히 잘리는 응답(500 -> 200) 때문에
        구간이 빈 채로 끝난다.
        """
        end = end or dt.datetime.now(dt.timezone.utc)
        # 응답의 시각에는 타임존 표기가 없다(= UTC). 표기가 붙은 값과 그냥
        # 비교하면 pandas가 거부한다. 양쪽을 '표기 없는 UTC'로 맞춰 둔다.
        floor = _naive_utc(start)
        frames: list[pd.DataFrame] = []
        cursor = end
        seen_oldest: pd.Timestamp | None = None

        while True:
            page = self.candles(market, unit=unit, count=MAX_COUNT, to=cursor)
            if page.empty:
                break
            wanted = page[page.index >= floor]
            if not wanted.empty:
                frames.append(wanted)

            # 다음 구간은 **받은 것 중 가장 이른 시각**으로 정한다. 요청한
            # 개수를 기준으로 삼으면, 조용히 잘린 응답에서 구간이 빈다.
            oldest = page.index.min()
            if oldest <= floor:
                break
            if seen_oldest is not None and oldest >= seen_oldest:
                # 같은 구간이 다시 왔다. 더 과거가 없다는 뜻이므로 멈춘다.
                # 이 방어가 없으면 상장일 근처에서 무한히 맴돈다.
                break
            seen_oldest = oldest
            cursor = oldest.to_pydatetime().replace(tzinfo=dt.timezone.utc)

        if not frames:
            return _empty()
        out = pd.concat(frames).sort_index()
        return out[~out.index.duplicated(keep="last")]

    # ------------------------------------------------------------ 마켓
    def markets(self, details: bool = True) -> list[dict]:
        """거래 가능한 마켓 목록.

        details=True면 유의종목·주의종목 지정 여부(`market_event`)가 같이 온다.
        상장폐지 위험 종목을 거르는 데 쓴다 — 주식에는 없던 장치다.
        """
        rows = self._get("/market/all", {"isDetails": str(bool(details)).lower()})
        if not isinstance(rows, list):
            raise UpbitError(f"/market/all 응답이 목록이 아닙니다: {rows!r:.200}")
        return rows

    def flagged(self) -> dict[str, str]:
        """{마켓: 사유}. 유의(warning)나 주의(caution)로 지정된 것만."""
        out: dict[str, str] = {}
        for row in self.markets(details=True):
            event = row.get("market_event") or {}
            reasons = []
            if event.get("warning"):
                reasons.append("유의종목")
            reasons += [k for k, v in (event.get("caution") or {}).items() if v]
            if reasons:
                out[str(row.get("market"))] = ", ".join(reasons)
        return out


class UpbitProvider(PriceProvider):
    """백테스트용 일봉 제공자."""

    name = "upbit"

    def __init__(self, quotes: UpbitQuotes | None = None) -> None:
        self.quotes = quotes or UpbitQuotes()

    def fetch(self, tickers: list[str], start: str, end: str | None = None) -> PriceData:
        begin = dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc)
        finish = (dt.datetime.fromisoformat(end).replace(tzinfo=dt.timezone.utc)
                  if end else None)

        closes, opens, volumes = {}, {}, {}
        for ticker in tickers:
            frame = self.quotes.history(ticker, begin, finish, unit=None)
            if frame.empty:
                continue
            closes[ticker] = frame["close"]
            opens[ticker] = frame["open"]
            volumes[ticker] = frame["volume"]
        if not closes:
            return PriceData(close=pd.DataFrame())
        return PriceData(
            close=pd.DataFrame(closes).sort_index(),
            open=pd.DataFrame(opens).sort_index(),
            volume=pd.DataFrame(volumes).sort_index(),
        )


# ------------------------------------------------------------------ 변환
def _to_param(when: dt.datetime) -> str:
    """업비트 `to` 파라미터 형식. UTC로 바꿔서 보낸다.

    타임존이 없는 값을 그대로 보내면 KST를 UTC로 읽어 9시간이 어긋난다.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _naive_utc(when: dt.datetime) -> pd.Timestamp:
    """표기 없는 UTC 시각. 응답의 시각과 같은 기준으로 맞춘다."""
    stamp = pd.Timestamp(when)
    return stamp.tz_convert("UTC").tz_localize(None) if stamp.tz else stamp


def _empty() -> pd.DataFrame:
    frame = pd.DataFrame(columns=COLUMNS, dtype=float)
    frame.index = pd.DatetimeIndex([], name="datetime")
    return frame


def _to_frame(rows: list, market: str) -> pd.DataFrame:
    """응답 배열을 OHLCV 표로. 필드가 없으면 0으로 때우지 않고 실패한다."""
    if not rows:
        return _empty()

    required = (F_TIME_UTC, F_OPEN, F_HIGH, F_LOW, F_CLOSE)
    missing = [f for f in required if f not in rows[0]]
    if missing:
        raise UpbitError(
            f"{market} 캔들 응답에 필요한 필드가 없습니다: {missing}\n"
            f"  받은 필드: {sorted(rows[0])}\n"
            "  업비트가 응답 형식을 바꿨을 수 있습니다."
        )

    index, records = [], []
    for row in rows:
        index.append(pd.Timestamp(row[F_TIME_UTC]))
        records.append({
            "open": float(row[F_OPEN]),
            "high": float(row[F_HIGH]),
            "low": float(row[F_LOW]),
            "close": float(row[F_CLOSE]),
            "volume": float(row.get(F_VOLUME) or 0.0),
        })
    frame = pd.DataFrame(records, index=pd.DatetimeIndex(index, name="datetime"))
    return frame.sort_index()
