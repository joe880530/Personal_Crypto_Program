import pandas as pd
import pytest

from stockbot.backtest import PRESETS, CostBook
from stockbot.execution import (
    Account,
    BrokerError,
    Order,
    OrderType,
    PaperBroker,
    Position,
    RiskGuard,
    Side,
    plan_orders,
)


@pytest.fixture
def prices_now() -> pd.Series:
    return pd.Series({"SPY": 500.0, "QQQ": 400.0, "TLT": 100.0})


@pytest.fixture
def broker(tmp_path, prices_now) -> PaperBroker:
    return PaperBroker(
        state_path=tmp_path / "acct.json",
        initial_cash=10_000_000,
        currency="KRW",
        costs=CostBook(PRESETS["ZERO"]),
        price_source=lambda tickers: {t: float(prices_now[t]) for t in tickers},
    )


# ------------------------------------------------------------------ 주문 생성
def test_plan_orders_reaches_target_weights(prices_now):
    target = pd.Series({"SPY": 0.5, "QQQ": 0.3, "TLT": 0.2})
    orders = plan_orders(target, {}, prices_now, cash=10_000_000, cash_buffer=0.0)
    allocated = {o.ticker: o.notional for o in orders}
    assert allocated["SPY"] / 10_000_000 == pytest.approx(0.5, abs=0.001)
    assert allocated["QQQ"] / 10_000_000 == pytest.approx(0.3, abs=0.001)


def test_plan_orders_sells_before_buying(prices_now):
    positions = {"TLT": Position("TLT", 50_000)}  # 전액 TLT 보유
    target = pd.Series({"SPY": 1.0, "TLT": 0.0})
    orders = plan_orders(target, positions, prices_now, cash=0.0)
    assert orders[0].side is Side.SELL
    assert any(o.side is Side.BUY for o in orders)


def test_plan_orders_never_shorts(prices_now):
    positions = {"SPY": Position("SPY", 10)}
    target = pd.Series({"SPY": 0.0})
    orders = plan_orders(target, positions, prices_now, cash=0.0)
    sell = next(o for o in orders if o.ticker == "SPY")
    assert sell.quantity <= 10


def test_plan_orders_respects_band(prices_now):
    """목표와 현재가 밴드 안이면 주문을 만들지 않는다."""
    positions = {"SPY": Position("SPY", 10_000)}  # 5,000,000 = 50%
    target = pd.Series({"SPY": 0.52})
    assert plan_orders(target, positions, prices_now, cash=5_000_000, band=0.05) == []
    assert plan_orders(target, positions, prices_now, cash=5_000_000, band=0.01)


def test_plan_orders_produces_integer_shares(prices_now):
    target = pd.Series({"SPY": 0.5, "QQQ": 0.5})
    orders = plan_orders(target, {}, prices_now, cash=1_234_567, allow_fractional=False)
    assert all(float(o.quantity).is_integer() for o in orders)


def test_plan_orders_honours_lot_size(prices_now):
    target = pd.Series({"SPY": 1.0})
    orders = plan_orders(target, {}, prices_now, cash=10_000_000, lot_sizes={"SPY": 10})
    assert orders[0].quantity % 10 == 0


def test_plan_orders_does_not_exceed_available_cash(prices_now):
    target = pd.Series({"SPY": 0.5, "QQQ": 0.5})
    orders = plan_orders(target, {}, prices_now, cash=1_000_000, cash_buffer=0.0)
    assert sum(o.notional for o in orders) <= 1_000_000 + 1e-6


def test_plan_orders_skips_unknown_prices(prices_now):
    target = pd.Series({"SPY": 0.5, "MISSING": 0.5})
    orders = plan_orders(target, {}, prices_now, cash=1_000_000)
    assert all(o.ticker != "MISSING" for o in orders)


def test_limit_orders_get_a_price(prices_now):
    target = pd.Series({"SPY": 1.0})
    orders = plan_orders(
        target, {}, prices_now, cash=1_000_000,
        order_type=OrderType.LIMIT, limit_slippage=0.01,
    )
    assert orders[0].limit_price == pytest.approx(505.0)


def test_cash_buffer_leaves_room(prices_now):
    target = pd.Series({"SPY": 1.0})
    orders = plan_orders(target, {}, prices_now, cash=10_000_000, cash_buffer=0.02)
    assert sum(o.notional for o in orders) <= 10_000_000 * 0.98 + 500


def test_order_rejects_zero_quantity():
    with pytest.raises(ValueError, match="양수"):
        Order("SPY", Side.BUY, 0)


def test_limit_order_requires_price():
    with pytest.raises(ValueError, match="limit_price"):
        Order("SPY", Side.BUY, 1, order_type=OrderType.LIMIT)


# ------------------------------------------------------------------ 모의 브로커
def test_paper_broker_buy_updates_cash_and_position(broker, prices_now):
    broker.submit(Order("SPY", Side.BUY, 100, reference_price=500.0))
    account = broker.get_account()
    assert account.cash == pytest.approx(10_000_000 - 50_000)
    assert account.positions["SPY"].quantity == 100


def test_paper_broker_rejects_insufficient_cash(broker):
    with pytest.raises(BrokerError, match="현금 부족"):
        broker.submit(Order("SPY", Side.BUY, 1_000_000, reference_price=500.0))


def test_paper_broker_rejects_oversell(broker):
    broker.submit(Order("SPY", Side.BUY, 10, reference_price=500.0))
    with pytest.raises(BrokerError, match="많이 매도할 수 없습니다"):
        broker.submit(Order("SPY", Side.SELL, 20, reference_price=500.0))


def test_paper_broker_round_trip_is_cash_neutral_without_costs(broker):
    start = broker.get_account().cash
    broker.submit(Order("SPY", Side.BUY, 100, reference_price=500.0))
    broker.submit(Order("SPY", Side.SELL, 100, reference_price=500.0))
    assert broker.get_account().cash == pytest.approx(start)
    assert "SPY" not in broker.get_account().positions


def test_paper_broker_state_survives_restart(tmp_path, prices_now):
    path = tmp_path / "acct.json"
    first = PaperBroker(path, initial_cash=1_000_000, costs=CostBook(PRESETS["ZERO"]))
    first.submit(Order("SPY", Side.BUY, 100, reference_price=500.0))

    second = PaperBroker(path, initial_cash=999, costs=CostBook(PRESETS["ZERO"]))
    assert second.get_account().positions["SPY"].quantity == 100
    assert second.get_account().cash == pytest.approx(950_000)


def test_paper_broker_average_price_includes_fees(tmp_path):
    broker = PaperBroker(tmp_path / "a.json", initial_cash=10_000_000, costs=CostBook(PRESETS["US"]))
    broker.submit(Order("SPY", Side.BUY, 100, reference_price=500.0))
    # 수수료와 슬리피지 때문에 취득단가는 기준가보다 높아야 한다
    assert broker.get_account().positions["SPY"].avg_price > 500.0


def test_submit_all_records_rejections_without_raising(broker):
    orders = [
        Order("SPY", Side.BUY, 10, reference_price=500.0),
        Order("QQQ", Side.SELL, 10, reference_price=400.0),  # 미보유 -> 거부
    ]
    fills = broker.submit_all(orders)
    assert fills[0].status == "filled"
    assert fills[1].status.startswith("rejected")


# ------------------------------------------------------------------ 안전장치
def _account(cash=5_000_000, positions=None):
    return Account(cash=cash, currency="KRW", positions=positions or {})


def test_guard_blocks_oversized_position(prices_now):
    guard = RiskGuard(max_position_weight=0.30)
    orders = [Order("SPY", Side.BUY, 10_000, reference_price=500.0)]
    report = guard.check(orders, _account(), dict(prices_now))
    assert report.approved == []
    assert any(v.rule == "max_position_weight" for v in report.violations)


def test_guard_blocks_order_above_value_limit(prices_now):
    guard = RiskGuard(max_order_value=1_000_000, max_position_weight=1.0)
    orders = [Order("SPY", Side.BUY, 4_000, reference_price=500.0)]
    report = guard.check(orders, _account(cash=10_000_000), dict(prices_now))
    assert any(v.rule == "max_order_value" for v in report.violations)


def test_guard_order_limit_can_follow_the_account_size(prices_now):
    """비율 한도는 계좌를 따라간다. 절대액은 규모가 바뀌면 손으로 고쳐야 한다."""
    guard = RiskGuard(max_order_pct=0.55, max_position_weight=1.0)
    order = [Order("SPY", Side.BUY, 12, reference_price=500.0)]   # 6,000원... 6,000

    # 평가액 10,000: 주문 6,000 > 한도 5,500 -> 거부
    report = guard.check(order, _account(cash=10_000), dict(prices_now))
    assert any(v.rule == "max_order_value" for v in report.violations)

    # 평가액 100,000: 같은 주문이 한도 55,000 아래 -> 승인
    report = guard.check(order, _account(cash=100_000), dict(prices_now))
    assert report.approved == order, report.describe()


def test_guard_applies_the_stricter_of_the_two_order_limits(prices_now):
    """둘 다 주면 느슨한 쪽이 이기면 안 된다."""
    order = [Order("SPY", Side.BUY, 12, reference_price=500.0)]   # 6,000
    account = _account(cash=100_000)   # 비율 한도 55,000 (느슨)

    guard = RiskGuard(max_order_value=1_000, max_order_pct=0.55, max_position_weight=1.0)
    report = guard.check(order, account, dict(prices_now))
    assert any(v.rule == "max_order_value" for v in report.violations), (
        "절대액 1,000이 더 엄격한데 비율 한도가 이겼습니다"
    )

    # 한도를 아예 안 주면 금액 제한이 없다(기존 동작).
    assert RiskGuard(max_position_weight=1.0).check(order, account, dict(prices_now)).ok


def test_notional_cap_does_not_trap_a_leveraged_account(prices_now):
    """미수가 생겼을 때 정리 매도를 막으면 안 된다.

    평가액 = 자산 - 부채라 빚이 끼면 작아진다. 그 상태를 벗어나려면 평가액보다
    큰 금액만큼 팔아야 하는데, 평가액을 한도 기준으로 두면 이 장치가 복구를
    막는다. 실제로 막혔다 — 승인 0건으로 계좌가 잠겼다.
    """
    account = Account(cash=-9_000.0, currency="USD", positions={
        "SPY": Position("SPY", 20, 500.0),      # 평가 10,000
        "QQQ": Position("QQQ", 20, 400.0),      # 평가  8,000
    })
    guard = RiskGuard(max_total_notional_pct=1.0, max_position_weight=1.0)
    # 평가액은 9,000뿐이지만 총자산은 18,000이다.
    orders = [Order("SPY", Side.SELL, 10, reference_price=500.0),
              Order("QQQ", Side.SELL, 10, reference_price=400.0)]   # 합계 9,000

    report = guard.check(orders, account, dict(prices_now))
    assert report.approved == orders, report.describe()


def test_notional_cap_still_stops_runaway_trading(prices_now):
    """복구를 허용한다고 폭주까지 허용하면 안 된다."""
    account = Account(cash=10_000.0, currency="USD", positions={})
    guard = RiskGuard(max_total_notional_pct=1.0, max_position_weight=1.0)
    orders = [Order("SPY", Side.BUY, 100, reference_price=500.0)]   # 50,000
    report = guard.check(orders, account, dict(prices_now))
    assert report.halted
    assert any(v.rule == "max_total_notional" for v in report.violations)


def test_buying_is_refused_while_in_debt_but_selling_is_not(prices_now):
    """미수 상태에서 더 사면 빚이 는다. 매도까지 막으면 벗어날 수가 없다."""
    account = Account(cash=-5_000.0, currency="USD",
                      positions={"SPY": Position("SPY", 30, 500.0)})
    guard = RiskGuard(max_position_weight=1.0, max_total_notional_pct=1.0)

    buy = [Order("QQQ", Side.BUY, 1, reference_price=400.0)]
    report = guard.check(buy, account, dict(prices_now))
    assert report.approved == []
    assert any(v.rule == "margin" for v in report.violations)
    assert not report.halted, "매도는 계속 낼 수 있어야 합니다"

    sell = [Order("SPY", Side.SELL, 10, reference_price=500.0)]
    assert guard.check(sell, account, dict(prices_now)).approved == sell


def test_margin_can_be_allowed_when_explicitly_asked(prices_now):
    account = Account(cash=-5_000.0, currency="USD",
                      positions={"SPY": Position("SPY", 30, 500.0)})
    guard = RiskGuard(max_position_weight=1.0, max_total_notional_pct=1.0, allow_margin=True)
    buy = [Order("QQQ", Side.BUY, 1, reference_price=400.0)]
    assert guard.check(buy, account, dict(prices_now)).approved == buy


def test_guard_rejects_tickers_outside_whitelist(prices_now):
    guard = RiskGuard(allowed_tickers={"SPY"}, max_position_weight=1.0)
    orders = [Order("QQQ", Side.BUY, 1, reference_price=400.0)]
    report = guard.check(orders, _account(), dict(prices_now))
    assert any(v.rule == "whitelist" for v in report.violations)


def test_guard_halts_on_too_many_orders(prices_now):
    guard = RiskGuard(max_orders=2)
    orders = [Order(f"SPY", Side.BUY, 1, reference_price=500.0) for _ in range(5)]
    report = guard.check(orders, _account(), dict(prices_now))
    assert report.halted
    assert report.approved == []


def test_guard_halts_when_total_notional_too_large(prices_now):
    guard = RiskGuard(max_total_notional_pct=0.10, max_position_weight=1.0)
    orders = [Order("SPY", Side.BUY, 2_000, reference_price=500.0)]
    report = guard.check(orders, _account(cash=5_000_000), dict(prices_now))
    assert report.halted


def test_guard_blocks_zero_price(prices_now):
    guard = RiskGuard(max_position_weight=1.0)
    prices = dict(prices_now)
    prices["SPY"] = 0.0  # 데이터 오류 시나리오
    report = guard.check(
        [Order("SPY", Side.BUY, 1, reference_price=500.0)], _account(), prices
    )
    assert any(v.rule == "price" for v in report.violations)


def test_drawdown_stop_blocks_buys_but_allows_sells(prices_now):
    guard = RiskGuard(max_drawdown_stop=0.20, max_position_weight=1.0)
    account = _account(cash=1_000_000, positions={"SPY": Position("SPY", 1_000)})
    orders = [
        Order("SPY", Side.SELL, 100, reference_price=500.0),
        Order("QQQ", Side.BUY, 100, reference_price=400.0),
    ]
    report = guard.check(orders, account, dict(prices_now), peak_equity=10_000_000)
    approved = {o.ticker for o in report.approved}
    assert "SPY" in approved      # 매도는 허용 — 위험을 줄이는 방향이다
    assert "QQQ" not in approved  # 신규 매수는 차단


def test_guard_blocks_oversell(prices_now):
    guard = RiskGuard(max_position_weight=1.0)
    account = _account(positions={"SPY": Position("SPY", 10)})
    report = guard.check(
        [Order("SPY", Side.SELL, 100, reference_price=500.0)], account, dict(prices_now)
    )
    assert any(v.rule == "oversell" for v in report.violations)


def test_guard_passes_clean_orders(prices_now):
    guard = RiskGuard(max_position_weight=0.60)
    report = guard.check(
        [Order("SPY", Side.BUY, 1_000, reference_price=500.0)],
        _account(cash=10_000_000), dict(prices_now),
    )
    assert report.ok
    assert len(report.approved) == 1
