import numpy as np
import pandas as pd
import pytest

from stockbot.backtest import PRESETS, Backtester, CostBook, CostModel, rebalance_flags
from stockbot.backtest.schedule import needs_rebalance
from stockbot.portfolio import EqualWeight, FixedWeight, RiskParity


def _zero_cost() -> CostBook:
    return CostBook(PRESETS["ZERO"])


# ------------------------------------------------------------------ 미래참조 방지
def test_backtest_does_not_peek_into_the_future(prices):
    """분기점 이후의 가격을 바꿔도, 그 이전의 자산곡선은 한 푼도 달라지면 안 된다.

    이 테스트가 깨지면 백테스트 결과 전체를 믿을 수 없다. 미래 가격이 과거
    의사결정에 새어 들어갔다는 뜻이기 때문이다.
    """
    split = 500
    tampered = prices.copy()
    tampered.iloc[split:] *= 3.0  # 이후 구간을 완전히 다른 값으로 교체

    common = dict(strategy=RiskParity(lookback=120), rebalance="M", costs=_zero_cost())
    original = Backtester(prices, **common).run()
    changed = Backtester(tampered, **common).run()

    # 체결은 신호 다음 봉에 일어나므로 split 직전까지 비교한다.
    pd.testing.assert_series_equal(
        original.equity.iloc[: split - 1], changed.equity.iloc[: split - 1]
    )


def test_strategy_never_sees_a_bar_it_cannot_trade_on(prices):
    """전략에 넘어가는 history의 마지막 날짜는 항상 체결 가능한 날이어야 한다.

    마지막 봉에서 신호를 내면 체결할 다음 봉이 없어 조용히 무시된다. 엔진은
    아예 호출하지 않아야 한다.
    """
    seen: list[pd.Timestamp] = []

    class SpyStrategy(EqualWeight):
        def target_weights(self, history):
            seen.append(history.index[-1])
            return super().target_weights(history)

    Backtester(prices, SpyStrategy(), rebalance="D", costs=_zero_cost()).run()

    assert seen, "전략이 한 번도 호출되지 않았습니다"
    assert prices.index[-1] not in seen
    assert max(seen) == prices.index[-2]


def test_first_bar_has_no_trades(prices):
    """첫 봉은 아직 신호가 없으므로 거래가 있을 수 없다."""
    result = Backtester(prices, EqualWeight(), rebalance="D", costs=_zero_cost()).run()
    assert (result.trades["date"] > prices.index[0]).all()


# ------------------------------------------------------------------ 기본 동작
def test_buy_and_hold_tracks_underlying_asset(prices):
    """단일 종목 100% 매수 후 보유는 그 종목 수익률을 따라가야 한다."""
    single = prices[["GROW"]]
    result = Backtester(
        single, FixedWeight({"GROW": 1.0}), rebalance="never",
        costs=_zero_cost(), initial_cash=1_000_000, allow_fractional=True,
    ).run()

    # 첫 봉 신호 -> 둘째 봉 체결이므로 둘째 봉부터의 수익률과 비교한다.
    asset_return = single["GROW"].iloc[-1] / single["GROW"].iloc[1] - 1
    portfolio_return = result.equity.iloc[-1] / result.equity.iloc[1] - 1
    assert portfolio_return == pytest.approx(asset_return, rel=1e-6)


def test_costs_reduce_returns(prices):
    """같은 전략이라면 비용을 붙인 쪽의 최종 자산이 반드시 더 적어야 한다."""
    common = dict(strategy=EqualWeight(), rebalance="W")
    free = Backtester(prices, costs=_zero_cost(), **common).run()
    charged = Backtester(prices, costs=CostBook(PRESETS["KR"]), **common).run()

    assert charged.equity.iloc[-1] < free.equity.iloc[-1]
    assert charged.total_costs > 0
    assert free.total_costs == pytest.approx(0.0)


def test_higher_rebalance_frequency_costs_more(prices):
    """리밸런싱이 잦을수록 비용과 회전율이 커진다."""
    common = dict(strategy=RiskParity(lookback=120), costs=CostBook(PRESETS["US"]))
    monthly = Backtester(prices, rebalance="M", **common).run()
    daily = Backtester(prices, rebalance="D", **common).run()

    assert daily.total_costs > monthly.total_costs
    assert daily.turnover > monthly.turnover


def test_band_rebalancing_reduces_trading(prices):
    """밴드를 넓히면 거래 횟수가 줄어야 한다."""
    common = dict(strategy=EqualWeight(), rebalance="D", costs=CostBook(PRESETS["US"]))
    tight = Backtester(prices, band=0.0, **common).run()
    wide = Backtester(prices, band=0.10, **common).run()

    assert len(wide.trades) < len(tight.trades)
    assert wide.total_costs < tight.total_costs


def test_cash_never_goes_negative(prices):
    result = Backtester(
        prices, EqualWeight(), rebalance="W", costs=CostBook(PRESETS["KR"])
    ).run()
    assert (result.cash >= -1e-6).all()


def test_no_short_positions(prices):
    """공매도는 지원하지 않는다. 어떤 비중도 음수가 되면 안 된다."""
    result = Backtester(prices, RiskParity(lookback=120), rebalance="M").run()
    assert (result.weights >= -1e-9).all().all()


def test_integer_shares_when_fractional_disabled(prices):
    result = Backtester(
        prices, EqualWeight(), rebalance="M", allow_fractional=False,
        costs=_zero_cost(), initial_cash=10_000_000,
    ).run()
    quantities = result.trades["quantity"]
    assert np.allclose(quantities, np.round(quantities))


def test_cash_rate_accrues_interest():
    """전량 현금이면 이자율만큼 자산이 늘어야 한다."""
    idx = pd.bdate_range("2020-01-01", periods=252)
    px = pd.DataFrame({"A": np.full(252, 100.0)}, index=idx)
    result = Backtester(
        px, FixedWeight({"A": 0.0}), rebalance="never",
        costs=_zero_cost(), cash_rate=0.05, initial_cash=1_000_000,
    ).run()
    assert result.equity.iloc[-1] / result.equity.iloc[0] == pytest.approx(1.05, rel=1e-3)


def test_execution_falls_back_to_close_without_open_prices(prices):
    bt = Backtester(prices, EqualWeight(), execution="next_open", open_prices=None)
    assert bt.execution == "next_close"


def test_open_price_execution_uses_open(prices):
    """시가 체결을 지정하면 종가가 아닌 시가로 체결돼야 한다."""
    opens = prices * 0.95
    result = Backtester(
        prices, EqualWeight(), rebalance="never", costs=_zero_cost(),
        execution="next_open", open_prices=opens, allow_fractional=True,
    ).run()
    first = result.trades.iloc[0]
    expected_open = opens.loc[first["date"], first["ticker"]]
    assert first["price"] == pytest.approx(expected_open)


def test_min_trade_value_skips_small_orders(prices):
    common = dict(strategy=EqualWeight(), rebalance="D", costs=_zero_cost())
    normal = Backtester(prices, min_trade_value=0, **common).run()
    filtered = Backtester(prices, min_trade_value=100_000, **common).run()
    assert len(filtered.trades) < len(normal.trades)


def test_rejects_empty_prices():
    with pytest.raises(ValueError, match="비어"):
        Backtester(pd.DataFrame(), EqualWeight())


def test_rejects_non_datetime_index():
    with pytest.raises(TypeError, match="DatetimeIndex"):
        Backtester(pd.DataFrame({"A": [1.0, 2.0]}), EqualWeight())


# ------------------------------------------------------------------ 스케줄
def test_monthly_flags_land_on_last_trading_day_of_month():
    idx = pd.bdate_range("2024-01-01", "2024-03-31")
    flagged = idx[rebalance_flags(idx, "M")]
    assert list(flagged.strftime("%Y-%m-%d")) == ["2024-01-31", "2024-02-29", "2024-03-29"]


def test_never_rebalances_only_once():
    idx = pd.bdate_range("2024-01-01", periods=100)
    assert rebalance_flags(idx, "never").sum() == 1


def test_unknown_frequency_raises():
    with pytest.raises(ValueError, match="리밸런싱 주기"):
        rebalance_flags(pd.bdate_range("2024-01-01", periods=5), "biweekly")


def test_band_blocks_small_deviations():
    current = pd.Series({"A": 0.51, "B": 0.49})
    target = pd.Series({"A": 0.50, "B": 0.50})
    assert not needs_rebalance(current, target, band=0.05)
    assert needs_rebalance(current, target, band=0.005)
    assert needs_rebalance(current, target, band=0.0)


# ------------------------------------------------------------------ 비용 모델
def test_slippage_hurts_both_sides():
    model = CostModel(commission_bps=0, slippage_bps=10, sell_tax_bps=0)
    assert model.fill_price(100.0, "buy") > 100.0
    assert model.fill_price(100.0, "sell") < 100.0


def test_sell_tax_applies_only_to_sells():
    model = CostModel(commission_bps=0, slippage_bps=0, sell_tax_bps=18)
    assert model.fee(1_000_000, "buy") == pytest.approx(0.0)
    assert model.fee(1_000_000, "sell") == pytest.approx(1_800.0)


def test_min_commission_applies_to_tiny_orders():
    model = CostModel(commission_bps=1, min_commission=1000)
    assert model.fee(10_000, "buy") == pytest.approx(1000.0)


def test_cost_book_from_markets_maps_presets():
    book = CostBook.from_markets({"SPY": "US", "069500": "KR_ETF"})
    assert book.for_ticker("069500").sell_tax_bps == 0.0
    assert book.for_ticker("SPY").commission_bps == PRESETS["US"].commission_bps


def test_cost_book_rejects_unknown_market():
    with pytest.raises(ValueError, match="알 수 없는 시장"):
        CostBook.from_markets({"SPY": "MARS"})


# ------------------------------------------------------------------ 결측 가격 처리
def test_missing_price_does_not_zero_out_a_holding():
    """시세가 없는 날에도 보유 종목을 0원으로 평가하면 안 된다.

    가격을 모르는 것과 가치가 0인 것은 다르다. 0으로 채우면 가격이 전혀
    변하지 않았는데도 자산곡선에 가짜 폭락과 가짜 급등이 생긴다.
    거래일 달력이 다른 시장(국내+미국)을 섞으면 반드시 발생한다.
    """
    idx = pd.bdate_range("2024-01-01", periods=40)
    px = pd.DataFrame({"A": np.full(40, 100.0), "B": np.full(40, 100.0)}, index=idx)
    px.loc[idx[20:26], "A"] = np.nan  # A만 6거래일 결측

    result = Backtester(
        px, FixedWeight({"A": 0.5, "B": 0.5}), rebalance="never",
        costs=_zero_cost(), allow_fractional=True,
    ).run()

    # 가격이 한 번도 변하지 않았으므로 평가액도 변하면 안 된다.
    assert result.equity.round(2).nunique() == 1
    returns = result.equity.pct_change().dropna()
    assert returns.abs().max() == pytest.approx(0.0, abs=1e-12)


def test_stale_asset_is_not_traded_while_unpriced():
    """시세가 없는 종목은 평가는 하되 거래는 하지 않아야 한다."""
    idx = pd.bdate_range("2024-01-01", periods=40)
    px = pd.DataFrame(
        {"A": np.linspace(100, 120, 40), "B": np.linspace(100, 90, 40)}, index=idx
    )
    gap = idx[20:26]
    px.loc[gap, "A"] = np.nan

    result = Backtester(
        px, EqualWeight(), rebalance="D", costs=_zero_cost(), allow_fractional=True
    ).run()

    traded_during_gap = result.trades[
        (result.trades["ticker"] == "A") & (result.trades["date"].isin(gap))
    ]
    assert traded_during_gap.empty


def test_valuation_uses_last_known_price_not_future():
    """평가용 채움은 앞으로만 한다. 뒤에서 끌어오면 미래 가격을 쓰는 셈이다."""
    idx = pd.bdate_range("2024-01-01", periods=10)
    px = pd.DataFrame({"A": [np.nan, np.nan, 100.0, 110.0, np.nan, np.nan, 130.0, 140.0, 150.0, 160.0]}, index=idx)

    bt = Backtester(px, FixedWeight({"A": 1.0}), costs=_zero_cost())

    # 상장 전 구간은 여전히 결측이어야 한다(뒤의 100으로 채우면 안 됨)
    assert bt.valuation_prices["A"].iloc[:2].isna().all()
    # 공백 구간은 직전 값 110으로 채워져야 한다(뒤의 130이 아니라)
    assert bt.valuation_prices["A"].iloc[4] == pytest.approx(110.0)
    assert bt.valuation_prices["A"].iloc[5] == pytest.approx(110.0)
