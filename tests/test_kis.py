"""KIS 브로커 검증.

실제 KIS 서버는 API 키가 있어야 하므로 여기서는 가짜 전송 계층으로
**요청을 제대로 만드는지, 응답을 제대로 읽는지, 위험한 실수를 막는지**를 본다.
실통신은 사용자가 모의투자 키로 확인해야 한다.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error

import pytest

from stockbot.execution.kis import (
    HTTP_TIMEOUT,
    PAPER_DOMAIN,
    REAL_DOMAIN,
    TRANSIENT_RETRIES,
    KISBroker,
    KISCredentials,
    KISError,
    KISTransientError,
    _asks_for_retry,
    _is_rate_limited,
    _urllib_transport,
    diagnose_env,
    load_dotenv,
    mask_secret,
)
from stockbot.execution.order import Order, OrderType, Side

CREDS = KISCredentials(
    app_key="APPKEY", app_secret="SECRET", account="12345678", product_code="01", paper=True
)


class FakeTransport:
    """호출을 기록하고 미리 정한 응답을 돌려준다."""

    def __init__(self, responses: dict[str, tuple[int, dict]]):
        self.responses = responses
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, params, body):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "params": params, "body": body}
        )
        for key, response in self.responses.items():
            if key in url:
                return response
        raise AssertionError(f"준비되지 않은 호출: {url}")


TOKEN_OK = (200, {"access_token": "TOKEN123", "expires_in": 86400})

BALANCE_OK = (200, {
    "rt_cd": "0",
    "output1": [
        {"pdno": "069500", "hldg_qty": "100", "pchs_avg_pric": "38000"},
        {"pdno": "114260", "hldg_qty": "0", "pchs_avg_pric": "105000"},  # 청산된 종목
    ],
    # 가수도정산금액(D+2)과 예수금총금액이 다른 상황 — 오늘 매수가 있었다.
    "output2": [{
        "prvs_rcdl_excc_amt": "1500000",
        "dnca_tot_amt": "10000000",
        "thdt_buy_amt": "8500000",
    }],
})


def _broker(responses, tmp_path, **kwargs):
    # 테스트는 실제로 잠들지 않는다. 호출 간격은 따로 시험한다.
    kwargs.setdefault("pace", 0)
    kwargs.setdefault("sleep", lambda _: None)
    return KISBroker(
        credentials=kwargs.pop("credentials", CREDS),
        token_path=str(tmp_path / "token.json"),
        transport=FakeTransport({"tokenP": TOKEN_OK, **responses}),
        **kwargs,
    )


# ----------------------------------------------------------------- 안전 기본값
def test_paper_is_the_default_and_is_not_live():
    creds = KISCredentials.from_env(
        {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KIS_ACCOUNT": "123"}
    )
    assert creds.paper is True, "KIS_PAPER를 안 넣으면 모의투자여야 합니다"


def test_live_account_requires_explicit_opt_out(tmp_path):
    """실계좌는 명시적으로 false를 넣어야만 열린다."""
    for value in ["true", "TRUE", "1", "yes", "아무거나"]:
        creds = KISCredentials.from_env(
            {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KIS_ACCOUNT": "1", "KIS_PAPER": value}
        )
        assert creds.paper is True, f"{value!r}에서 실계좌가 열렸습니다"

    live = KISCredentials.from_env(
        {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KIS_ACCOUNT": "1", "KIS_PAPER": "false"}
    )
    assert live.paper is False
    broker = KISBroker(live, token_path=str(tmp_path / "t.json"), transport=lambda *a: (200, {}), pace=0, sleep=lambda _: None)
    assert broker.is_live is True, "실계좌인데 is_live가 False면 CLI 확인 절차가 건너뛰어집니다"
    assert broker.domain == REAL_DOMAIN


def test_paper_broker_talks_to_the_paper_domain(tmp_path):
    broker = _broker({}, tmp_path)
    assert broker.domain == PAPER_DOMAIN
    assert broker.is_live is False


def test_missing_credentials_explain_what_to_do():
    with pytest.raises(KISError, match="KIS_APP_SECRET"):
        KISCredentials.from_env({"KIS_APP_KEY": "k"})


# ----------------------------------------------------------------- 조회
def test_balance_is_parsed_and_liquidated_rows_are_dropped(tmp_path):
    broker = _broker({"inquire-balance": BALANCE_OK}, tmp_path)
    account = broker.get_account()

    assert account.cash == 1_500_000
    assert account.currency == "KRW"
    assert set(account.positions) == {"069500"}, "보유수량 0인 종목이 남았습니다"
    assert account.positions["069500"].quantity == 100
    assert account.positions["069500"].avg_price == 38_000


def test_missing_field_fails_loudly_instead_of_reporting_zero(tmp_path):
    """현금이 0으로 보이면 전량 매도 주문이 만들어질 수 있다."""
    broken = (200, {"rt_cd": "0", "output1": [], "output2": [{"예수금": "1500000"}]})
    broker = _broker({"inquire-balance": broken}, tmp_path)

    with pytest.raises(KISError) as exc:
        broker.get_account()
    assert "prvs_rcdl_excc_amt" in str(exc.value), "현재 쓰는 현금 필드를 짚어야 합니다"
    assert "받은 필드" in str(exc.value), "무엇이 왔는지 보여줘야 원인을 압니다"


def test_price_lookup_uses_the_quote_endpoint(tmp_path):
    ok = (200, {"rt_cd": "0", "output": {"stck_prpr": "38250"}})
    broker = _broker({"inquire-price": ok}, tmp_path)
    assert broker.get_prices(["069500"]) == {"069500": 38250.0}


def test_rt_cd_failure_is_raised_even_on_http_200(tmp_path):
    """KIS는 HTTP 200으로 거부 응답을 준다. 성공으로 읽으면 안 된다."""
    rejected = (200, {"rt_cd": "1", "msg_cd": "40570000", "msg1": "모의투자 장운영시간이 아닙니다"})
    broker = _broker({"inquire-balance": rejected}, tmp_path)

    with pytest.raises(KISError, match="장운영시간"):
        broker.get_account()


# ----------------------------------------------------------------- 주문
ORDER_OK = (200, {"rt_cd": "0", "output": {"ODNO": "0000117057"}})


def _order(side=Side.BUY, qty=10, price=38000, order_type=OrderType.LIMIT):
    return Order(
        ticker="069500", side=side, quantity=qty,
        order_type=order_type, limit_price=price, reference_price=price,
    )


def test_buy_and_sell_use_the_documented_tr_ids(tmp_path):
    """TR ID가 틀리면 주문이 거부되거나 엉뚱한 주문이 나간다."""
    fake = FakeTransport({"tokenP": TOKEN_OK, "order-cash": ORDER_OK})
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"), transport=fake, pace=0, sleep=lambda _: None)

    broker.submit(_order(side=Side.BUY))
    broker.submit(_order(side=Side.SELL))

    orders = [c for c in fake.calls if "order-cash" in c["url"]]
    assert orders[0]["headers"]["tr_id"] == "VTTC0012U"  # 모의 매수
    assert orders[1]["headers"]["tr_id"] == "VTTC0011U"  # 모의 매도

    live = KISBroker(
        dataclass_replace(CREDS, paper=False),
        token_path=str(tmp_path / "t2.json"),
        transport=FakeTransport({"tokenP": TOKEN_OK, "order-cash": ORDER_OK}), pace=0, sleep=lambda _: None)
    live.submit(_order(side=Side.BUY))
    live_calls = [c for c in live._transport.calls if "order-cash" in c["url"]]
    assert live_calls[0]["headers"]["tr_id"] == "TTTC0012U"  # 실전 매수


def test_order_body_sends_strings_and_required_fields(tmp_path):
    fake = FakeTransport({"tokenP": TOKEN_OK, "order-cash": ORDER_OK})
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"), transport=fake, pace=0, sleep=lambda _: None)
    broker.submit(_order(qty=7, price=38250))

    body = [c for c in fake.calls if "order-cash" in c["url"]][0]["body"]
    assert body["PDNO"] == "069500"
    assert body["ORD_QTY"] == "7", "수량은 문자열이어야 합니다"
    assert body["ORD_UNPR"] == "38250"
    assert body["ORD_DVSN"] == "00", "지정가는 00"
    assert body["CANO"] == "12345678"
    assert body["ACNT_PRDT_CD"] == "01"
    assert body["EXCG_ID_DVSN_CD"] == "KRX"


def test_market_order_sends_zero_price(tmp_path):
    fake = FakeTransport({"tokenP": TOKEN_OK, "order-cash": ORDER_OK})
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"), transport=fake, pace=0, sleep=lambda _: None)
    broker.submit(Order(ticker="069500", side=Side.BUY, quantity=3,
                        order_type=OrderType.MARKET, reference_price=38000))

    body = [c for c in fake.calls if "order-cash" in c["url"]][0]["body"]
    assert body["ORD_DVSN"] == "01", "시장가는 01"
    assert body["ORD_UNPR"] == "0"


def test_fractional_orders_are_refused(tmp_path):
    """국내주식은 소수점 주문이 안 된다. 잘라서 보내면 의도와 달라진다."""
    broker = _broker({"order-cash": ORDER_OK}, tmp_path)
    with pytest.raises(KISError, match="소수점"):
        broker.submit(_order(qty=1.5))


def test_submit_reports_accepted_not_filled(tmp_path):
    """접수 응답에는 체결가가 없다. filled로 표시하면 체결된 줄 안다."""
    broker = _broker({"order-cash": ORDER_OK}, tmp_path)
    fill = broker.submit(_order())
    assert fill.status == "accepted"
    assert fill.order_id == "0000117057"


def test_rejected_order_surfaces_the_broker_message(tmp_path):
    rejected = (200, {"rt_cd": "1", "msg_cd": "40240000", "msg1": "주문가능금액을 초과했습니다"})
    broker = _broker({"order-cash": rejected}, tmp_path)
    with pytest.raises(KISError, match="주문가능금액"):
        broker.submit(_order())


# ----------------------------------------------------------------- 토큰
def test_token_is_requested_once_and_reused(tmp_path):
    """토큰 발급은 횟수 제한이 있다. 호출마다 새로 받으면 막힌다."""
    fake = FakeTransport({"tokenP": TOKEN_OK, "inquire-balance": BALANCE_OK})
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"), transport=fake, pace=0, sleep=lambda _: None)

    broker.get_account()
    broker.get_account()
    broker.get_account()

    assert sum(1 for c in fake.calls if "tokenP" in c["url"]) == 1


def test_cached_token_survives_a_new_process(tmp_path):
    token_file = tmp_path / "t.json"
    first = FakeTransport({"tokenP": TOKEN_OK, "inquire-balance": BALANCE_OK})
    KISBroker(CREDS, token_path=str(token_file), transport=first, pace=0, sleep=lambda _: None).get_account()

    second = FakeTransport({"inquire-balance": BALANCE_OK})  # 토큰 발급을 준비하지 않는다
    KISBroker(CREDS, token_path=str(token_file), transport=second, pace=0, sleep=lambda _: None).get_account()
    assert not any("tokenP" in c["url"] for c in second.calls)


def test_cached_token_is_not_reused_across_environments(tmp_path):
    """모의 토큰을 실전에 쓰면 인증이 깨진다. 섞이면 안 된다."""
    token_file = tmp_path / "t.json"
    KISBroker(
        CREDS, token_path=str(token_file),
        transport=FakeTransport({"tokenP": TOKEN_OK, "inquire-balance": BALANCE_OK}), pace=0, sleep=lambda _: None).get_account()

    live_transport = FakeTransport({"tokenP": TOKEN_OK, "inquire-balance": BALANCE_OK})
    KISBroker(
        dataclass_replace(CREDS, paper=False),
        token_path=str(token_file), transport=live_transport, pace=0, sleep=lambda _: None).get_account()
    assert any("tokenP" in c["url"] for c in live_transport.calls), (
        "모의투자 토큰을 실계좌에 재사용했습니다"
    )


def test_expired_token_is_refreshed(tmp_path):
    clock = {"now": 1_000.0}
    fake = FakeTransport({
        "tokenP": (200, {"access_token": "T", "expires_in": 600}),
        "inquire-balance": BALANCE_OK,
    })
    broker = KISBroker(
        CREDS, token_path=str(tmp_path / "t.json"),
        transport=fake, clock=lambda: clock["now"], pace=0, sleep=lambda _: None)
    broker.get_account()
    clock["now"] += 10_000  # 만료 후
    broker.get_account()
    assert sum(1 for c in fake.calls if "tokenP" in c["url"]) == 2


def test_token_file_is_not_world_readable(tmp_path):
    """토큰은 계좌 접근 권한 그 자체다."""
    token_file = tmp_path / "t.json"
    _broker({"inquire-balance": BALANCE_OK}, tmp_path).get_account()
    broker = KISBroker(
        CREDS, token_path=str(token_file),
        transport=FakeTransport({"tokenP": TOKEN_OK, "inquire-balance": BALANCE_OK}), pace=0, sleep=lambda _: None)
    broker.get_account()
    assert token_file.stat().st_mode & 0o077 == 0, "다른 사용자가 토큰을 읽을 수 있습니다"


def test_secrets_never_appear_in_the_token_cache(tmp_path):
    token_file = tmp_path / "t.json"
    KISBroker(
        CREDS, token_path=str(token_file),
        transport=FakeTransport({"tokenP": TOKEN_OK, "inquire-balance": BALANCE_OK}), pace=0, sleep=lambda _: None).get_account()
    saved = json.loads(token_file.read_text())
    assert "SECRET" not in json.dumps(saved), "앱시크릿이 파일에 저장됐습니다"


# ----------------------------------------------------------------- .env
def test_dotenv_does_not_override_real_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('KIS_APP_KEY=from_file\nKIS_ACCOUNT="99999999"\n# 주석\n')
    monkeypatch.setenv("KIS_APP_KEY", "from_environment")

    values = load_dotenv(str(env_file))
    assert values["KIS_APP_KEY"] == "from_environment"
    assert values["KIS_ACCOUNT"] == "99999999", "따옴표가 벗겨져야 합니다"


def dataclass_replace(obj, **kwargs):
    import dataclasses

    return dataclasses.replace(obj, **kwargs)


# ------------------------------------------------------- 접속 정보 자가진단
GOOD_ENV = {
    "KIS_APP_KEY": "PS" + "a" * 34,
    "KIS_APP_SECRET": "SS" + "b" * 46,
    "KIS_ACCOUNT": "50214588",
    "KIS_PRODUCT_CODE": "01",
    "KIS_PAPER": "true",
}


def levels_for(lines, needle):
    """진단 줄 중 needle이 들어간 것들의 수준만 뽑는다."""
    return [level for level, text in lines if needle in text]


def write_env(tmp_path, text):
    file = tmp_path / ".env"
    file.write_text(text, encoding="utf-8")
    return str(file)


def test_mask_never_shows_a_whole_secret():
    secret = "PS" + "a" * 34
    masked = mask_secret(secret)
    assert secret not in masked
    assert "(36자)" in masked, "길이는 보여야 복사가 잘렸는지 알 수 있습니다"
    # 짧은 값은 앞뒤를 남기면 사실상 전부 드러난다. 통째로 가려야 한다.
    assert mask_secret("abc") == "*** (3자)"


def test_good_env_has_no_problems(tmp_path):
    lines, bad = diagnose_env(GOOD_ENV, write_env(tmp_path, "KIS_PAPER=true\n"), os_environ={})
    assert bad == 0
    assert not [level for level, _ in lines if level == "bad"]


def test_missing_env_file_is_a_blocking_problem(tmp_path):
    """파일도 없고 값도 없으면 막는다.

    (값이 환경변수로 들어오는 경우는 아래 test_environment_variables_alone_are_enough.)
    """
    lines, bad = diagnose_env({}, str(tmp_path / "없음.env"), os_environ={})
    assert bad >= 1
    assert levels_for(lines, ".env 파일이 없습니다") == ["bad"]


def test_placeholders_are_caught(tmp_path):
    env = dict(GOOD_ENV, KIS_ACCOUNT="12345678", KIS_APP_KEY="발급받은_앱키")
    lines, bad = diagnose_env(env, write_env(tmp_path, ""), os_environ={})
    assert bad == 2, "예시 값을 그대로 둔 것은 통과시키면 안 됩니다"
    assert levels_for(lines, "KIS_ACCOUNT: 예시 값") == ["bad"]


def test_inline_comment_in_a_key_is_flagged(tmp_path):
    """줄 끝 주석은 값에 그대로 붙는다. 인증만 실패하고 원인은 안 보인다."""
    env = dict(GOOD_ENV, KIS_APP_KEY="PS" + "a" * 34 + "  # 내 키")
    lines, bad = diagnose_env(env, write_env(tmp_path, ""), os_environ={})
    assert bad == 0, "고칠 수 있는 문제지만 값 자체는 있으므로 중단까지는 아닙니다"
    assert levels_for(lines, "값 안에 공백이나 # 가 있습니다") == ["warn"]


def test_truncated_key_is_flagged(tmp_path):
    lines, _ = diagnose_env(dict(GOOD_ENV, KIS_APP_SECRET="short"), write_env(tmp_path, ""),
                            os_environ={})
    assert levels_for(lines, "짧아 보입니다") == ["warn"]


def test_live_account_is_called_out(tmp_path):
    lines, bad = diagnose_env(dict(GOOD_ENV, KIS_PAPER="false"), write_env(tmp_path, ""),
                              os_environ={})
    assert bad == 0, "실계좌는 사용자가 고른 것이므로 막지는 않습니다"
    assert levels_for(lines, "실계좌") == ["warn"], "다만 반드시 눈에 띄어야 합니다"


def test_os_environment_shadowing_the_file_is_flagged(tmp_path):
    """예전에 set 해 둔 값이 .env를 이긴다. 파일만 고치면 원인을 못 찾는다."""
    path = write_env(tmp_path, "KIS_APP_KEY=" + "f" * 36 + "\n")
    env = dict(GOOD_ENV, KIS_APP_KEY="o" * 36)   # load_dotenv가 환경변수를 우선한 결과
    lines, _ = diagnose_env(env, path, os_environ={"KIS_APP_KEY": "o" * 36})
    assert levels_for(lines, "덮어쓰고 있습니다") == ["warn"]

    # 값이 같으면 덮어쓴 게 없으므로 조용해야 한다.
    lines, _ = diagnose_env(env, path, os_environ={"KIS_APP_KEY": "f" * 36})
    assert levels_for(lines, "덮어쓰고 있습니다") == []


def test_ping_checks_authentication_only(tmp_path):
    """ping은 토큰만 본다. 잔고를 건드리면 장 시간에 따라 결과가 달라진다."""
    transport = FakeTransport({"tokenP": TOKEN_OK, "inquire-balance": BALANCE_OK})
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"), transport=transport, pace=0, sleep=lambda _: None)
    text = broker.ping()

    assert [c for c in transport.calls if "tokenP" in c["url"]]
    assert not [c for c in transport.calls if "inquire-balance" in c["url"]]
    assert PAPER_DOMAIN in text
    assert "TOKEN" not in text, "토큰이 그대로 찍히면 안 됩니다"


def test_environment_variables_alone_are_enough(tmp_path):
    """컨테이너에는 .env 파일이 없다. 값이 환경변수로 들어오면 그걸로 충분하다."""
    missing = str(tmp_path / "없음.env")
    lines, bad = diagnose_env(GOOD_ENV, missing, os_environ=dict(GOOD_ENV))
    assert bad == 0, "파일이 없다고 막으면 NAS/컨테이너에서 쓸 수 없습니다"
    assert levels_for(lines, "환경변수로 값이 들어왔습니다") == ["ok"]

    # 파일도 없고 값도 없으면 그건 진짜 문제다.
    lines, bad = diagnose_env({}, missing, os_environ={})
    assert bad >= 1
    assert levels_for(lines, ".env 파일이 없습니다") == ["bad"]


# --------------------------------------------------- 결제 전 예수금을 쓰지 않는다
def test_cash_is_the_settled_amount_not_the_raw_deposit(tmp_path):
    """예수금총금액을 '남은 현금'으로 읽으면 같은 날 또 산다.

    국내 주식 대금은 D+2 결제라 예수금총금액에는 오늘 산 금액이 아직 빠져 있지
    않다. 실제로 그 때문에 069500을 45주씩 두 번 샀다.
    """
    broker = _broker({"inquire-balance": BALANCE_OK}, tmp_path)
    account = broker.get_account()

    assert account.cash == 1_500_000, "가수도정산금액(D+2)을 써야 합니다"
    assert account.cash != 10_000_000, "예수금총금액을 쓰면 안 됩니다"


def test_cash_detail_shows_why_the_two_numbers_differ(tmp_path):
    """숫자 하나만 보여주면 '현금이 그대로네'의 이유를 알 수 없다."""
    detail = _broker({"inquire-balance": BALANCE_OK}, tmp_path).cash_detail()
    assert detail["settled"] == 1_500_000
    assert detail["raw"] == 10_000_000
    assert detail["bought_today"] == 8_500_000


# ------------------------------------------------------- 호출 제한 (EGW00201)
RATE_LIMITED = (200, {"rt_cd": "1", "msg_cd": "EGW00201",
                      "msg1": "초당 거래건수를 초과하였습니다."})


class FlakyTransport(FakeTransport):
    """처음 N번은 호출 제한으로 거부하고 그 뒤에 성공한다."""

    def __init__(self, responses, fail_times: int):
        super().__init__(responses)
        self.remaining = fail_times
        self.attempts = 0

    def __call__(self, method, url, headers, params, body):
        if "tokenP" in url:
            return super().__call__(method, url, headers, params, body)
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            return RATE_LIMITED
        # 성공 경로는 super()가 기록한다. 여기서도 기록하면 두 번 센다.
        return super().__call__(method, url, headers, params, body)


def test_rate_limited_queries_are_retried(tmp_path):
    """보유 종목 시세를 연달아 물으면 모의투자 초당 제한에 걸린다. 실제로 걸렸다."""
    ok = (200, {"rt_cd": "0", "output": {"stck_prpr": "38250"}})
    transport = FlakyTransport({"tokenP": TOKEN_OK, "inquire-price": ok}, fail_times=2)
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"),
                       transport=transport, pace=0, sleep=lambda _: None)

    assert broker.get_prices(["069500"]) == {"069500": 38250.0}
    assert transport.attempts == 3, "두 번 거부당한 뒤 세 번째에 성공해야 합니다"


def test_orders_are_never_retried(tmp_path):
    """거부된 줄 알았는데 접수됐으면 중복 주문이 된다. 조회와 다르다."""
    transport = FlakyTransport(
        {"tokenP": TOKEN_OK, "order-cash": (200, {"rt_cd": "0", "output": {"ODNO": "1"}})},
        fail_times=1)
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"),
                       transport=transport, pace=0, sleep=lambda _: None)

    with pytest.raises(KISError) as exc:
        broker.submit(Order("069500", Side.BUY, 1, order_type=OrderType.MARKET))
    assert "EGW00201" in str(exc.value)
    assert transport.attempts == 1, "주문은 다시 보내면 안 됩니다"


def test_calls_keep_a_minimum_interval(tmp_path):
    """간격을 안 두면 제한에 걸린다. 재시도보다 애초에 안 걸리는 편이 낫다."""
    slept: list[float] = []
    ticks = iter([0.0] * 40)
    ok = (200, {"rt_cd": "0", "output": {"stck_prpr": "100"}})
    broker = KISBroker(
        CREDS, token_path=str(tmp_path / "t.json"),
        transport=FakeTransport({"tokenP": TOKEN_OK, "inquire-price": ok}),
        pace=0.5, sleep=slept.append, clock=lambda: next(ticks, 0.0),
    )
    broker.get_prices(["069500", "114260", "360750"])
    assert len(slept) >= 3, f"종목마다 간격을 둬야 합니다: {slept}"
    assert all(s <= 0.5 for s in slept)


# ------------------------------------------------- 끊긴 호출 (응답을 못 받음)
class DroppingTransport(FakeTransport):
    """처음 N번은 응답 없이 끊기고 그 뒤에 성공한다.

    분봉 1년치(516회 호출)를 받다 실제로 겪은 상황이다. 258일 중 14일이
    "The read operation timed out"으로 빠졌다.
    """

    def __init__(self, responses, drop_times: int, error=None):
        super().__init__(responses)
        self.remaining = drop_times
        self.attempts = 0
        self.error = error or KISTransientError("KIS 서버와의 통신이 끊겼습니다: timed out")

    def __call__(self, method, url, headers, params, body):
        if "tokenP" in url:
            return super().__call__(method, url, headers, params, body)
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self.error
        return super().__call__(method, url, headers, params, body)


def test_dropped_queries_are_retried(tmp_path):
    """시간 초과로 끊긴 조회는 다시 묻는다. 다시 물어도 잃을 것이 없다."""
    ok = (200, {"rt_cd": "0", "output": {"stck_prpr": "38250"}})
    transport = DroppingTransport({"tokenP": TOKEN_OK, "inquire-price": ok}, drop_times=2)
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"),
                       transport=transport, pace=0, sleep=lambda _: None)

    assert broker.get_prices(["069500"]) == {"069500": 38250.0}
    assert transport.attempts == 3, "두 번 끊긴 뒤 세 번째에 성공해야 합니다"


def test_dropped_queries_give_up_eventually(tmp_path):
    """계속 끊기면 포기하고 원인을 그대로 올린다. 무한정 매달리면 안 된다."""
    ok = (200, {"rt_cd": "0", "output": {"stck_prpr": "1"}})
    transport = DroppingTransport({"tokenP": TOKEN_OK, "inquire-price": ok}, drop_times=99)
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"),
                       transport=transport, pace=0, sleep=lambda _: None)

    with pytest.raises(KISTransientError) as exc:
        broker.get_prices(["069500"])
    assert "끊겼습니다" in str(exc.value)
    assert transport.attempts == TRANSIENT_RETRIES


def test_dropped_orders_are_never_resent(tmp_path):
    """끊긴 주문은 접수됐는지 알 수 없다. 다시 보내면 두 번 살 수 있다.

    실제로 069500을 45주씩 두 번 산 적이 있다(원인은 달랐지만 결과는 같다).
    답이 없다는 것은 '거부됐다'가 아니라 '모른다'는 뜻이다.
    """
    ok = (200, {"rt_cd": "0", "output": {"ODNO": "1"}})
    transport = DroppingTransport({"tokenP": TOKEN_OK, "order-cash": ok}, drop_times=1)
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"),
                       transport=transport, pace=0, sleep=lambda _: None)

    with pytest.raises(KISTransientError):
        broker.submit(Order("069500", Side.BUY, 1, order_type=OrderType.MARKET))
    assert transport.attempts == 1, "끊긴 주문을 다시 보내면 중복 매수가 됩니다"


def test_transient_error_is_a_kis_error():
    """호출 쪽이 KISError만 잡고 있어도 놓치지 않아야 한다."""
    assert issubclass(KISTransientError, KISError)


@pytest.mark.parametrize("raised", [
    TimeoutError("The read operation timed out"),
    ConnectionResetError("Connection reset by peer"),
    OSError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"),
    urllib.error.URLError("연결 실패"),
])
def test_transport_marks_dropped_connections_as_transient(monkeypatch, raised):
    """실제로 올라온 예외들이 전부 '다시 시도할 것'으로 분류돼야 한다.

    시간 초과는 URLError가 아니라 TimeoutError로 그냥 올라온다. 한쪽만 잡으면
    나머지는 재시도 없이 그대로 실패한다.
    """
    def boom(req, timeout=None):
        raise raised

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(KISTransientError):
        _urllib_transport("GET", "https://example.test/x", {}, None, None)


def test_transport_keeps_server_refusals_separate(monkeypatch):
    """서버가 '안 된다'고 답한 것은 재시도 대상이 아니다. 다시 물어도 같은 답이다."""
    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("https://example.test/x", 500, "err", {}, None)

        def read(self):
            return json.dumps({"rt_cd": "1", "msg1": "모의투자 TR 이 아닙니다."}).encode()

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(FakeHTTPError()))
    status, payload = _urllib_transport("GET", "https://example.test/x", {}, None, None)
    assert status == 500
    assert "모의투자" in payload["msg1"]


def test_timeout_is_long_enough_for_minute_bars():
    """20초로는 분봉 조회가 끊겼다. 다시 줄이려면 그때 일을 먼저 보라."""
    assert HTTP_TIMEOUT >= 30


# ------------------------------------- KIS가 "다시 물어봐 달라"고 답하는 경우
#: 분봉 백필에서 실제로 받은 응답.
ASK_RETRY = (500, {"rt_cd": "1", "msg_cd": "OPSQ0001",
                   "msg1": "조회 처리 중 오류 발생하였습니다. 재 조회 수행 부탁드립니다."})
#: 같은 HTTP 500이지만 몇 번을 물어도 답이 같은 경우(모의투자 미지원 TR).
HARD_REFUSAL = (500, {"rt_cd": "1", "msg_cd": "OPSQ0002",
                      "msg1": "모의투자 TR 이 아닙니다."})


class AnsweringTransport(FakeTransport):
    """처음 N번은 정해둔 실패 응답을 주고 그 뒤에 성공한다."""

    def __init__(self, responses, failure, fail_times: int):
        super().__init__(responses)
        self.failure = failure
        self.remaining = fail_times
        self.attempts = 0

    def __call__(self, method, url, headers, params, body):
        if "tokenP" in url:
            return super().__call__(method, url, headers, params, body)
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            return self.failure
        return super().__call__(method, url, headers, params, body)


def test_kis_asking_for_a_retry_is_honoured(tmp_path):
    """서버가 "재 조회 수행 부탁드립니다"라고 하면 다시 물어야 한다.

    답을 받았다고 해서 다 최종 답인 것은 아니다. 분봉 백필에서 이 응답을
    그냥 실패로 넘겨 하루를 빠뜨린 적이 있다.
    """
    ok = (200, {"rt_cd": "0", "output": {"stck_prpr": "38250"}})
    transport = AnsweringTransport({"tokenP": TOKEN_OK, "inquire-price": ok},
                                   ASK_RETRY, fail_times=2)
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"),
                       transport=transport, pace=0, sleep=lambda _: None)

    assert broker.get_prices(["069500"]) == {"069500": 38250.0}
    assert transport.attempts == 3


def test_a_flat_refusal_is_not_retried(tmp_path):
    """같은 HTTP 500이라도 '모의투자 TR 이 아닙니다'는 다시 물어도 소용없다.

    상태 코드로 재시도를 판단하면 이런 응답에 세 배의 호출을 낭비하고,
    세 배 느리게 같은 실패에 도달한다. 구분 기준은 문구다.
    """
    ok = (200, {"rt_cd": "0", "output": {"stck_prpr": "1"}})
    transport = AnsweringTransport({"tokenP": TOKEN_OK, "inquire-price": ok},
                                   HARD_REFUSAL, fail_times=1)
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"),
                       transport=transport, pace=0, sleep=lambda _: None)

    with pytest.raises(KISError) as exc:
        broker.get_prices(["069500"])
    assert "모의투자 TR" in str(exc.value)
    assert transport.attempts == 1, "다시 물어도 같은 답이 오는 응답입니다"


def test_orders_are_not_resent_even_when_kis_asks(tmp_path):
    """서버가 다시 보내라고 해도 **주문은** 다시 보내지 않는다.

    조회는 다시 물어도 잃을 것이 없지만, 주문은 앞의 것이 접수됐을 수 있다.
    """
    ok = (200, {"rt_cd": "0", "output": {"ODNO": "1"}})
    transport = AnsweringTransport({"tokenP": TOKEN_OK, "order-cash": ok},
                                   ASK_RETRY, fail_times=1)
    broker = KISBroker(CREDS, token_path=str(tmp_path / "t.json"),
                       transport=transport, pace=0, sleep=lambda _: None)

    with pytest.raises(KISError):
        broker.submit(Order("069500", Side.BUY, 1, order_type=OrderType.MARKET))
    assert transport.attempts == 1, "끝내 접수됐을지 모르는 주문을 또 보내면 안 됩니다"


def test_successful_replies_are_never_mistaken_for_retries(tmp_path):
    """정상 응답에 '재조회' 같은 글자가 섞여도 재시도로 읽으면 안 된다."""
    assert not _asks_for_retry(200, {"rt_cd": "0", "msg1": "재 조회 완료"})
    assert not _is_rate_limited(200, {"rt_cd": "0", "msg1": "초당 거래건수 안내"})
