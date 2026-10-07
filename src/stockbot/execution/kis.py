"""한국투자증권(KIS) OpenAPI 브로커.

모의투자 도메인을 기본으로 한다. 실계좌는 `paper=False`를 명시해야만 열리고,
그때 `is_live=True`가 되어 CLI가 제출 전 확인을 강제한다.

엔드포인트와 TR ID는 한국투자증권이 공개한 공식 예제
(github.com/koreainvestment/open-trading-api, examples_user/)에서 확인했다.
인터넷에 널리 퍼진 구버전 TR ID(TTTC0802U 등)와 다르므로 기억으로 고치지 말 것.

    주문   POST /uapi/domestic-stock/v1/trading/order-cash
           실전 매수 TTTC0012U / 매도 TTTC0011U
           모의 매수 VTTC0012U / 매도 VTTC0011U
    잔고   GET  /uapi/domestic-stock/v1/trading/inquire-balance
           실전 TTTC8434R / 모의 VTTC8434R
    현재가 GET  /uapi/domestic-stock/v1/quotations/inquire-price   FHKST01010100
    토큰   POST /oauth2/tokenP

**검증 범위**: 이 모듈의 로직(요청 구성, 응답 해석, 토큰 재사용, 오류 처리)은
가짜 전송 계층으로 테스트했다. 실제 KIS 서버와의 통신은 API 키가 있어야
하므로 확인하지 못했다. 처음 붙일 때는 반드시 모의투자로 시작할 것.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import http.client
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .broker import Broker, BrokerError
from .order import Account, Fill, Order, OrderType, Position, Side

REAL_DOMAIN = "https://openapi.koreainvestment.com:9443"
PAPER_DOMAIN = "https://openapivts.koreainvestment.com:29443"

ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
TOKEN_PATH = "/oauth2/tokenP"

PRICE_TR = "FHKST01010100"

#: 호출 간격(초). 모의투자는 초당 건수 제한이 빡빡해서(EGW00201) 넉넉히 둔다.
#: 공식 예제도 실전 0.05 / 그 외 0.5를 쓴다.
PACE_REAL = 0.05
PACE_PAPER = 0.5

#: 호출 제한에 걸렸을 때의 재시도 횟수와 대기(초).
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF = 1.0

#: 응답을 기다리는 시간(초). 분봉 조회가 20초를 넘기는 일이 실제로 있었다.
HTTP_TIMEOUT = 45.0

#: 끊긴 호출(응답 없음)을 다시 시도하는 횟수와 대기(초).
#: 대기를 회차마다 늘린다 — 서버가 밀려 있을 때 바로 다시 두드려야 소용없다.
TRANSIENT_RETRIES = 3
TRANSIENT_BACKOFF = 2.0

#: 호출 제한 응답을 알아보는 표시. 코드가 바뀔 수 있어 문구도 같이 본다.
RATE_LIMIT_CODE = "EGW00201"
RATE_LIMIT_TEXT = "초당 거래건수"

#: KIS가 "다시 물어봐 달라"고 답할 때의 문구. 서버가 응답은 했지만 내용이
#: 재시도 요청인 경우다(예: HTTP 500 "조회 처리 중 오류 발생하였습니다.
#: 재 조회 수행 부탁드립니다."). 공백 위치가 일정하지 않아 공백을 뗀 뒤 본다.
#:
#: 상태 코드로 판단하면 안 된다. 같은 HTTP 500으로 "모의투자 TR 이 아닙니다"도
#: 오는데 그건 몇 번을 물어도 같은 답이 온다. 구분 기준은 문구뿐이다.
RETRY_HINTS = ("재조회", "다시조회", "재시도")

#: 주문구분. "00" 지정가, "01" 시장가.
ORD_DVSN_LIMIT = "00"
ORD_DVSN_MARKET = "01"

#: 응답에서 읽는 필드. KIS가 필드명을 바꾸면 조용히 0이 되는 대신
#: 무엇이 왔는지 보여주며 실패해야 하므로 한 곳에 모아 둔다.
#: 주문에 쓸 수 있는 현금. **예수금총금액(dnca_tot_amt)이 아니다.**
#: 국내 주식 대금은 D+2에 결제되므로 예수금총금액에는 오늘 산 금액이 아직
#: 빠져 있지 않다. 그걸 '남은 현금'으로 읽으면 같은 날 또 사게 된다
#: (실제로 069500을 45주씩 두 번 샀다). 가수도정산금액은 D+2 기준이라
#: 오늘 매수·매도가 반영돼 있다.
FIELD_CASH = "prvs_rcdl_excc_amt"    # 가수도정산금액 (D+2 예수금)
FIELD_CASH_RAW = "dnca_tot_amt"      # 예수금총금액 (미결제 포함, 참고용)
FIELD_BUY_TODAY = "thdt_buy_amt"     # 금일매수금액
FIELD_HOLDINGS_TICKER = "pdno"       # 상품번호(종목코드)
FIELD_HOLDINGS_QTY = "hldg_qty"      # 보유수량
FIELD_HOLDINGS_AVG = "pchs_avg_pric"  # 매입평균가격
FIELD_PRICE = "stck_prpr"            # 주식 현재가

#: 전송 계층. (method, url, headers, params, body) -> (status, json)
#: 테스트에서 가짜로 갈아끼우기 위해 분리한다.
Transport = Callable[[str, str, dict, dict | None, dict | None], tuple[int, dict]]


class KISError(BrokerError):
    """KIS API 호출 실패."""


class KISTransientError(KISError):
    """답을 못 받고 끊긴 호출. 서버가 거부한 것과는 다르다.

    시간 초과, 연결 끊김, SSL 중단처럼 **응답 자체가 없었던** 경우만 여기에
    들어온다. 서버가 "안 된다"고 답한 것(HTTP 오류, rt_cd != 0)은 그대로
    KISError다 — 다시 물어도 같은 답이 오므로 재시도할 이유가 없다.

    분봉 1년치를 받으면 500번 넘게 호출하는데, 그중 10여 번이 이렇게 끊긴다
    (실제로 258일 중 14일이 "The read operation timed out"으로 빠졌다).
    KIS 서버 문제라 이쪽에서 할 수 있는 건 잠시 쉬었다 다시 묻는 것뿐이다.
    """


@dataclasses.dataclass(frozen=True)
class KISCredentials:
    """접속 정보. **절대 커밋하지 말 것** — .env로 관리한다."""

    app_key: str
    app_secret: str
    account: str          # 종합계좌번호 앞 8자리 (CANO)
    product_code: str = "01"   # 계좌상품코드 (ACNT_PRDT_CD)
    paper: bool = True    # 기본은 모의투자

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "KISCredentials":
        """환경변수에서 읽는다.

            KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT
            KIS_PRODUCT_CODE (기본 01), KIS_PAPER (기본 true)

        실계좌를 열려면 KIS_PAPER=false를 **명시적으로** 넣어야 한다.
        변수를 빠뜨렸을 때 실계좌로 흘러가면 안 되기 때문이다.
        """
        env = env if env is not None else dict(os.environ)
        missing = [k for k in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT") if not env.get(k)]
        if missing:
            raise KISError(
                "KIS 접속 정보가 없습니다: " + ", ".join(missing) + "\n"
                "  프로젝트 루트에 .env를 만들고 아래를 채우세요(이 파일은 .gitignore에 있습니다):\n"
                "    KIS_APP_KEY=...\n"
                "    KIS_APP_SECRET=...\n"
                "    KIS_ACCOUNT=12345678      # 계좌번호 앞 8자리\n"
                "    KIS_PRODUCT_CODE=01       # 뒤 2자리\n"
                "    KIS_PAPER=true            # 모의투자. 실계좌는 false"
            )
        paper = str(env.get("KIS_PAPER", "true")).strip().lower() not in {"false", "0", "no"}
        return cls(
            app_key=env["KIS_APP_KEY"],
            app_secret=env["KIS_APP_SECRET"],
            account=env["KIS_ACCOUNT"],
            product_code=env.get("KIS_PRODUCT_CODE", "01"),
            paper=paper,
        )


def _urllib_transport(
    method: str, url: str, headers: dict, params: dict | None, body: dict | None
) -> tuple[int, dict]:
    """기본 전송 계층. 표준 라이브러리만 쓴다(의존성 추가 없이)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:  # 본문에 원인이 들어 있다
        # 서버가 답은 했다. 상태 코드와 본문을 그대로 올려보내 호출 쪽이 판단하게 둔다.
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except Exception:
            return exc.code, {}
    except urllib.error.URLError as exc:
        raise KISTransientError(f"KIS 서버에 접속하지 못했습니다: {exc.reason}") from exc
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        # 시간 초과는 URLError가 아니라 TimeoutError로 그냥 올라온다. 연결 끊김
        # (ConnectionResetError)과 SSL 중단(ssl.SSLError)도 OSError 갈래다.
        # 공통점은 '응답을 못 받았다'는 것 하나뿐이라 한 묶음으로 다룬다.
        raise KISTransientError(f"KIS 서버와의 통신이 끊겼습니다: {exc}") from exc


def _message_of(status: int, payload: dict) -> str:
    """실패 응답의 코드+문구. 성공이면 빈 문자열."""
    if status == 200 and str(payload.get("rt_cd", "0")) == "0":
        return ""
    return f"{payload.get('msg_cd', '')} {payload.get('msg1', '')}"


def _is_rate_limited(status: int, payload: dict) -> bool:
    """호출 제한에 걸린 응답인가. 코드가 바뀔 수 있어 문구도 같이 본다."""
    blob = _message_of(status, payload)
    return bool(blob) and (RATE_LIMIT_CODE in blob or RATE_LIMIT_TEXT in blob)


def _asks_for_retry(status: int, payload: dict) -> bool:
    """KIS가 스스로 "다시 조회해 달라"고 답한 경우인가.

    분봉 1년치를 받다 "조회 처리 중 오류 발생하였습니다. 재 조회 수행
    부탁드립니다."를 받고 그냥 실패로 넘긴 날이 있었다. 서버가 다시 물어보라고
    하는데 안 물어본 것이다. 답을 받았다고 해서 다 최종 답인 것은 아니다.
    """
    blob = _message_of(status, payload).replace(" ", "")
    return bool(blob) and any(hint in blob for hint in RETRY_HINTS)


class KISBroker(Broker):
    """한국투자증권 국내주식 브로커."""

    def __init__(
        self,
        credentials: KISCredentials,
        token_path: str = ".state/kis_token.json",
        transport: Transport | None = None,
        exchange: str = "KRX",
        clock: Callable[[], float] = time.time,
        pace: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.credentials = credentials
        self.domain = PAPER_DOMAIN if credentials.paper else REAL_DOMAIN
        self.name = "kis-모의투자" if credentials.paper else "kis-실계좌"
        self.is_live = not credentials.paper
        self.exchange = exchange
        self._token_path = pathlib.Path(token_path)
        self._transport = transport or _urllib_transport
        self._clock = clock
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._pace = (PACE_PAPER if credentials.paper else PACE_REAL) if pace is None else pace
        self._sleep = sleep
        self._last_call: float = 0.0

    # ------------------------------------------------------------ 인증
    def _access_token(self) -> str:
        """접근토큰. 24시간 유효하고 발급 횟수 제한이 있어 파일에 캐시한다."""
        now = self._clock()
        if self._token and now < self._token_expires:
            return self._token

        cached = self._read_cached_token()
        if cached:
            self._token, self._token_expires = cached
            return self._token

        status, body = self._transport(
            "POST",
            f"{self.domain}{TOKEN_PATH}",
            {"Content-Type": "application/json"},
            None,
            {
                "grant_type": "client_credentials",
                "appkey": self.credentials.app_key,
                "appsecret": self.credentials.app_secret,
            },
        )
        token = body.get("access_token")
        if status != 200 or not token:
            raise KISError(
                f"접근토큰 발급 실패 (HTTP {status}). "
                f"{body.get('error_description') or body.get('msg1') or body}\n"
                "  앱키/앱시크릿이 맞는지, 모의투자 키를 실전 도메인에 쓰고 있지 않은지 확인하세요."
            )
        # expires_in은 초 단위. 만료 직전 갱신되도록 5분 여유를 둔다.
        ttl = float(body.get("expires_in", 86400))
        self._token = token
        self._token_expires = now + max(ttl - 300, 60)
        self._write_cached_token(token, self._token_expires)
        return token

    def _read_cached_token(self) -> tuple[str, float] | None:
        try:
            data = json.loads(self._token_path.read_text())
        except (OSError, ValueError):
            return None
        # 다른 계좌/환경의 토큰을 재사용하면 안 된다.
        if data.get("domain") != self.domain or data.get("app_key") != self.credentials.app_key:
            return None
        if float(data.get("expires_at", 0)) <= self._clock():
            return None
        return data["token"], float(data["expires_at"])

    def _write_cached_token(self, token: str, expires_at: float) -> None:
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(json.dumps({
                "token": token,
                "expires_at": expires_at,
                "domain": self.domain,
                "app_key": self.credentials.app_key,
            }))
            self._token_path.chmod(0o600)  # 토큰은 계좌 접근 권한 그 자체다
        except OSError:
            pass  # 캐시 실패는 치명적이지 않다. 매번 발급받으면 된다.

    def ping(self) -> str:
        """토큰 발급만 확인한다.

        잔고 조회는 장 시간이나 계좌 상태에 걸릴 수 있어서, 인증이 되는지와
        계좌가 되는지를 한 번에 물으면 원인을 가려내기 어렵다. 여기서는
        인증만 본다. 캐시된 토큰이 있으면 그것을 쓰므로 발급 횟수를 축내지 않는다.
        """
        token = self._access_token()
        return f"{self.name} · {self.domain} · 토큰 {mask_secret(token)}"

    def _headers(self, tr_id: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._access_token()}",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def call(
        self, method: str, path: str, tr_id: str,
        params: dict | None = None, body: dict | None = None,
    ) -> dict:
        """인증된 KIS 호출 한 번. 시세 조회 쪽에서도 쓰도록 공개한다.

        토큰 발급·캐시·오류 판정이 전부 여기 묶여 있어서, 시세 모듈이 따로
        구현하면 두 벌이 생기고 한쪽만 고치는 일이 생긴다.

        호출 간격을 여기서 지킨다. 보유 종목 시세를 연달아 물으면 모의투자의
        초당 제한에 걸린다(실제로 걸렸다). 제한에 걸리거나 답을 못 받고 끊긴
        **조회**는 다시 시도하되, **주문은 다시 보내지 않는다** — 거부된 줄
        알았는데 접수됐으면 중복 주문이 된다. 조회는 다시 물어도 잃을 것이 없지만
        주문은 다르다. 특히 끊긴 주문은 접수 여부를 이쪽에서 알 방법이 없으므로
        다시 보내는 쪽이 훨씬 위험하다.
        """
        retryable = method.upper() == "GET"
        attempts = max(RATE_LIMIT_RETRIES, TRANSIENT_RETRIES) if retryable else 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            self._wait_turn()
            try:
                status, payload = self._transport(
                    method, f"{self.domain}{path}", self._headers(tr_id), params, body
                )
            except KISTransientError:
                if last or attempt >= TRANSIENT_RETRIES - 1:
                    raise
                self._sleep(TRANSIENT_BACKOFF * (attempt + 1))
                continue
            if _is_rate_limited(status, payload):
                if not last and attempt < RATE_LIMIT_RETRIES - 1:
                    self._sleep(RATE_LIMIT_BACKOFF * (attempt + 1))
                    continue
                # 더 시도할 여지가 없다. rt_cd 검사로 흘려보내면 "거부됨"이라고만
                # 나와서, 한도에 걸린 것인지 주문이 틀린 것인지 구분되지 않는다.
                raise KISError(
                    f"{path} 호출 제한에 계속 걸립니다 ({attempt + 1}회 시도)"
                    f" [{payload.get('msg_cd')}]: {payload.get('msg1')}"
                )
            if _asks_for_retry(status, payload) and not last:
                self._sleep(TRANSIENT_BACKOFF * (attempt + 1))
                continue
            # KIS는 HTTP 200이어도 rt_cd가 "0"이 아니면 실패다.
            if status != 200:
                raise KISError(f"{path} 실패 (HTTP {status}): {payload.get('msg1') or payload}")
            if str(payload.get("rt_cd", "0")) != "0":
                raise KISError(
                    f"{path} 거부됨 [{payload.get('msg_cd')}]: {payload.get('msg1')}"
                )
            return payload
        raise AssertionError("unreachable")  # 위 분기가 모두 반환하거나 올린다

    def _wait_turn(self) -> None:
        """직전 호출과 최소 간격을 둔다."""
        if self._pace <= 0:
            return
        waited = self._clock() - self._last_call
        if waited < self._pace:
            self._sleep(self._pace - waited)
        self._last_call = self._clock()

    # ------------------------------------------------------------ 조회
    def _tr(self, real: str, demo: str) -> str:
        return demo if self.credentials.paper else real

    def _balance(self) -> dict:
        return self.call(
            "GET", BALANCE_PATH, self._tr("TTTC8434R", "VTTC8434R"),
            params={
                "CANO": self.credentials.account,
                "ACNT_PRDT_CD": self.credentials.product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",        # 종목별
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )

    @staticmethod
    def _need(row: dict, field: str, where: str) -> str:
        """필드가 없으면 실제로 뭐가 왔는지 보여주며 실패한다.

        조용히 0으로 넘어가면 '현금 0원'이나 '보유 없음'으로 오인해
        엉뚱한 주문을 만들어낸다.
        """
        if field not in row:
            raise KISError(
                f"{where} 응답에 '{field}'가 없습니다. KIS 응답 형식이 바뀌었을 수 있습니다.\n"
                f"  받은 필드: {sorted(row)[:20]}"
            )
        return row[field]

    def get_positions(self) -> dict[str, Position]:
        payload = self._balance()
        out: dict[str, Position] = {}
        for row in payload.get("output1") or []:
            qty = float(self._need(row, FIELD_HOLDINGS_QTY, "잔고조회") or 0)
            if qty <= 0:
                continue  # 청산된 종목도 목록에 남는다
            ticker = self._need(row, FIELD_HOLDINGS_TICKER, "잔고조회")
            out[ticker] = Position(
                ticker=ticker,
                quantity=qty,
                avg_price=float(row.get(FIELD_HOLDINGS_AVG) or 0),
            )
        return out

    def cash_detail(self) -> dict[str, float]:
        """현금을 여러 기준으로 돌려준다. 어느 숫자를 쓰는지 눈으로 보려는 것.

        settled(D+2)와 raw(예수금총금액)가 크게 다르면 오늘 매매가 있었다는 뜻이다.
        """
        summary = (self._balance().get("output2") or [{}])[0]
        return {
            "settled": float(self._need(summary, FIELD_CASH, "잔고조회") or 0),
            "raw": float(summary.get(FIELD_CASH_RAW) or 0),
            "bought_today": float(summary.get(FIELD_BUY_TODAY) or 0),
        }

    def get_account(self) -> Account:
        payload = self._balance()
        summary = (payload.get("output2") or [{}])[0]
        cash = float(self._need(summary, FIELD_CASH, "잔고조회") or 0)

        positions: dict[str, Position] = {}
        for row in payload.get("output1") or []:
            qty = float(row.get(FIELD_HOLDINGS_QTY) or 0)
            if qty <= 0:
                continue
            ticker = row[FIELD_HOLDINGS_TICKER]
            positions[ticker] = Position(
                ticker=ticker, quantity=qty,
                avg_price=float(row.get(FIELD_HOLDINGS_AVG) or 0),
            )
        return Account(cash=cash, currency="KRW", positions=positions)

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for ticker in tickers:
            payload = self.call(
                "GET", PRICE_PATH, PRICE_TR,
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
            )
            output = payload.get("output") or {}
            prices[ticker] = float(self._need(output, FIELD_PRICE, f"현재가({ticker})") or 0)
        return prices

    # ------------------------------------------------------------ 주문
    def submit(self, order: Order) -> Fill:
        is_market = order.order_type is OrderType.MARKET
        if order.quantity != int(order.quantity):
            raise KISError(
                f"국내주식은 소수점 주문을 받지 않습니다: {order.ticker} {order.quantity}주.\n"
                "  설정에서 execution.allow_fractional을 false로 두세요."
            )

        tr_id = (
            self._tr("TTTC0012U", "VTTC0012U") if order.side is Side.BUY
            else self._tr("TTTC0011U", "VTTC0011U")
        )
        payload = self.call(
            "POST", ORDER_PATH, tr_id,
            body={
                "CANO": self.credentials.account,
                "ACNT_PRDT_CD": self.credentials.product_code,
                "PDNO": order.ticker,
                "ORD_DVSN": ORD_DVSN_MARKET if is_market else ORD_DVSN_LIMIT,
                "ORD_QTY": str(int(order.quantity)),
                # 시장가는 단가를 0으로 보낸다.
                "ORD_UNPR": "0" if is_market else str(int(order.limit_price or 0)),
                "EXCG_ID_DVSN_CD": self.exchange,
                "SLL_TYPE": "" if order.side is Side.BUY else "01",
                "CNDT_PRIC": "",
            },
        )
        output = payload.get("output") or {}
        # 접수 응답에는 체결가가 없다. 체결은 비동기로 일어난다.
        return Fill(
            order_id=str(output.get("ODNO", "")),
            ticker=order.ticker,
            side=order.side,
            quantity=order.quantity,
            price=float(order.limit_price or order.reference_price or 0),
            fee=0.0,
            timestamp=dt.datetime.now(dt.timezone.utc),
            status="accepted",  # filled 아님 — 접수됐을 뿐이다
        )


def mask_secret(value: str, keep: int = 4) -> str:
    """앞뒤 일부만 남기고 가린다. 화면 캡처를 주고받아도 안전하도록."""
    if not value:
        return "(비어 있음)"
    if len(value) <= keep * 2 + 4:
        return f"{'*' * len(value)} ({len(value)}자)"
    return f"{value[:keep]}{'*' * 8}{value[-keep:]} ({len(value)}자)"


#: 이보다 짧으면 복사가 잘렸다고 보고 경고한다. 실제 키는 훨씬 길지만,
#: 길이를 단정하면 KIS가 형식을 바꿨을 때 멀쩡한 키를 틀렸다고 하게 된다.
MIN_SECRET_LEN = {"KIS_APP_KEY": 16, "KIS_APP_SECRET": 16}

#: 설정 예시에 들어 있는 자리표시자. 그대로 두고 "채웠다"고 착각하기 쉽다.
PLACEHOLDERS = {"12345678", "발급받은_앱키", "발급받은_앱시크릿", "..."}


def diagnose_env(
    env: dict[str, str], path: str = ".env", os_environ: dict[str, str] | None = None
) -> tuple[list[tuple[str, str]], int]:
    """접속 정보를 훑어 사람이 읽을 진단 줄과 치명적 문제 수를 돌려준다.

    네트워크를 쓰지 않는다. 키를 잘못 넣었을 때 "인증 실패"라는 서버 응답만
    보고 원인을 짐작하게 두지 않으려는 것이다. 값 자체는 절대 그대로 찍지
    않는다 — 진단 화면을 캡처해 공유하는 일이 흔하기 때문이다.

    Returns:
        ([(수준, 문장)], 치명적 문제 수). 수준은 ok / warn / bad.
    """
    os_environ = dict(os.environ) if os_environ is None else os_environ
    out: list[tuple[str, str]] = []
    bad = 0

    file = pathlib.Path(path)
    # .env는 값을 담는 여러 방법 중 하나일 뿐이다. 컨테이너는 환경변수로 넘기는
    # 것이 정상이므로, 파일이 없다는 이유만으로 막으면 NAS에서 아예 못 쓴다.
    # 정말 문제인 경우는 '파일도 없고 값도 없는' 때다.
    has_credentials = all(env.get(k) for k in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT"))
    if file.exists():
        out.append(("ok", f".env 파일: {file.resolve()}"))
    elif has_credentials:
        out.append(("ok", ".env 파일은 없지만 환경변수로 값이 들어왔습니다 (컨테이너 등)"))
    else:
        out.append(("bad", f".env 파일이 없습니다: {file.resolve()}"))
        out.append(("bad", "  copy .env.example .env  를 먼저 실행하세요(프로젝트 폴더 안에서)."))
        bad += 1

    for key in ("KIS_APP_KEY", "KIS_APP_SECRET"):
        value = env.get(key, "")
        if not value:
            out.append(("bad", f"{key}: 비어 있습니다"))
            bad += 1
            continue
        if value in PLACEHOLDERS:
            out.append(("bad", f"{key}: 예시 값이 그대로 남아 있습니다"))
            bad += 1
            continue
        out.append(("ok", f"{key}: {mask_secret(value)}"))
        # 앞뒤 공백과 따옴표는 load_dotenv가 떼어내므로 문제가 되지 않는다.
        # 값 "안에" 섞인 것만 남는다 — 줄 끝에 주석을 달았거나 복사가 어긋난 경우다.
        if any(c.isspace() for c in value) or "#" in value:
            out.append(("warn", f"  {key} 값 안에 공백이나 # 가 있습니다."
                                " 줄 끝에 주석을 달지 말고 키만 적으세요."))
        if len(value) < MIN_SECRET_LEN[key]:
            out.append(("warn", f"  {key} 가 실제 키보다 짧아 보입니다."
                                " 복사가 잘리지 않았는지 확인하세요."))

    account = env.get("KIS_ACCOUNT", "")
    if not account:
        out.append(("bad", "KIS_ACCOUNT: 비어 있습니다"))
        bad += 1
    elif account in PLACEHOLDERS:
        out.append(("bad", "KIS_ACCOUNT: 예시 값 12345678 이 그대로 남아 있습니다"))
        bad += 1
    else:
        out.append(("ok", f"KIS_ACCOUNT: {account}"))
        if not (account.isdigit() and len(account) == 8):
            out.append(("warn", "  보통 숫자 8자리입니다. 뒤 2자리는 KIS_PRODUCT_CODE에 따로 넣으세요."))

    product = env.get("KIS_PRODUCT_CODE", "01")
    out.append(("ok", f"KIS_PRODUCT_CODE: {product}"))
    if not (product.isdigit() and len(product) == 2):
        out.append(("warn", "  보통 숫자 2자리입니다(종합계좌는 01)."))

    raw_paper = env.get("KIS_PAPER", "true")
    paper = str(raw_paper).strip().lower() not in {"false", "0", "no"}
    if paper:
        out.append(("ok", f"KIS_PAPER: {raw_paper} -> 모의투자 (안전)"))
    else:
        out.append(("warn", f"KIS_PAPER: {raw_paper} -> **실계좌**. 실제 돈이 움직입니다."))

    # .env보다 OS 환경변수가 우선한다. 예전에 set 해 둔 값이 조용히 이기는 경우가 있다.
    if file.exists():
        from_file = _parse_env_file(file)
        for key, value in from_file.items():
            if key in os_environ and os_environ[key] != value:
                out.append((
                    "warn",
                    f"{key}: OS 환경변수 값이 .env 파일 값을 덮어쓰고 있습니다."
                    " 창을 새로 열거나 set/unset 으로 정리하세요.",
                ))
    return out, bad


def _parse_env_file(file: pathlib.Path) -> dict[str, str]:
    """.env 한 파일만 파싱한다(환경변수와 합치지 않는다)."""
    values: dict[str, str] = {}
    try:
        text = file.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_dotenv(path: str = ".env") -> dict[str, str]:
    """.env를 읽어 dict로 준다. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    values: dict[str, str] = dict(os.environ)
    file = pathlib.Path(path)
    if not file.exists():
        return values
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        values.setdefault(key, value)
        if key not in os.environ:
            values[key] = value
    return values
