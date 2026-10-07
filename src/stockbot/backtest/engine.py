"""리밸런싱 백테스트 엔진.

설계 원칙 두 가지:

1. **미래를 보지 않는다.** 전략에는 판단 시점까지 잘린 가격만 넘기고, 체결은
   반드시 *다음* 봉에서 한다. 신호와 체결을 같은 봉에서 처리하면 실전에서
   재현 불가능한 수익이 만들어진다.
2. **비용을 먼저 뺀다.** 수수료·세금·슬리피지를 매 체결마다 차감한다.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from .. import metrics
from ..portfolio.base import CASH, Strategy
from .costs import CostBook
from .schedule import needs_rebalance, rebalance_flags


@dataclasses.dataclass
class BacktestResult:
    """백테스트 결과 묶음."""

    equity: pd.Series
    weights: pd.DataFrame
    trades: pd.DataFrame
    cash: pd.Series
    initial_cash: float
    periods_per_year: int = 252

    @property
    def total_costs(self) -> float:
        """수수료+세금 합계(슬리피지는 체결가에 이미 반영)."""
        return float(self.trades["fee"].sum()) if len(self.trades) else 0.0

    @property
    def turnover(self) -> float:
        """연평균 회전율. 1.0이면 연간 포트폴리오 전체를 한 번 교체한 셈."""
        if self.equity.empty or len(self.trades) == 0:
            return 0.0
        years = max(len(self.equity) / self.periods_per_year, 1e-9)
        traded = float(self.trades["notional"].abs().sum())
        return traded / float(self.equity.mean()) / years / 2.0

    def summary(self, risk_free: float = 0.0) -> dict[str, float]:
        out = metrics.summary(self.equity, risk_free, self.periods_per_year)
        out["total_costs"] = self.total_costs
        out["cost_drag_pct_of_initial"] = (
            self.total_costs / self.initial_cash if self.initial_cash else float("nan")
        )
        out["annual_turnover"] = self.turnover
        out["num_trades"] = float(len(self.trades))
        return out


class Backtester:
    """목표 비중 전략을 과거 가격에 적용해 시뮬레이션한다.

    Args:
        prices: 종가 DataFrame(index=날짜, columns=티커). 배당 재투자 반영된
            수정주가를 쓰는 것을 권장한다.
        strategy: `target_weights`를 구현한 전략.
        initial_cash: 초기 투자금.
        rebalance: 리밸런싱 주기('D'/'W'/'M'/'Q'/'Y'/'never').
        band: 밴드 리밸런싱 임계값(예: 0.05 = 5%p). 0이면 매 예정일 실행.
        costs: 종목별 거래비용표.
        execution: 'next_open'(익일 시가) 또는 'next_close'(익일 종가).
        open_prices: 시가 DataFrame. execution='next_open'일 때 필요.
        allow_fractional: False면 정수 주수로 내림 체결(국내 주식 등).
        cash_rate: 현금에 붙는 연이자율(예: 0.03).
        min_trade_value: 이 금액 미만의 주문은 건너뛴다.
    """

    def __init__(
        self,
        prices: pd.DataFrame,
        strategy: Strategy,
        initial_cash: float = 10_000_000.0,
        rebalance: str = "M",
        band: float = 0.0,
        costs: CostBook | None = None,
        execution: str = "next_open",
        open_prices: pd.DataFrame | None = None,
        allow_fractional: bool = True,
        cash_rate: float = 0.0,
        min_trade_value: float = 0.0,
        periods_per_year: int = 252,
    ) -> None:
        if prices.empty:
            raise ValueError("가격 데이터가 비어 있습니다")
        if not isinstance(prices.index, pd.DatetimeIndex):
            raise TypeError("prices의 index는 DatetimeIndex여야 합니다")
        if execution not in {"next_open", "next_close"}:
            raise ValueError("execution은 'next_open' 또는 'next_close'여야 합니다")
        if initial_cash <= 0:
            raise ValueError("initial_cash는 양수여야 합니다")

        self.prices = prices.sort_index()
        # 평가용 가격: 마지막으로 알려진 종가로 채운다. 결측을 0으로 두면
        # 보유 종목을 그날만 0원으로 쳐서, 가격이 멀쩡한데도 자산곡선에
        # 가짜 폭락과 가짜 급등이 생긴다(거래일 달력이 다른 시장을 섞으면
        # 반드시 발생한다). 앞으로만 채우므로 미래 정보는 쓰지 않는다.
        self.valuation_prices = self.prices.ffill()
        self.strategy = strategy
        self.initial_cash = float(initial_cash)
        self.rebalance = rebalance
        self.band = band
        self.costs = costs or CostBook()
        self.execution = execution
        self.allow_fractional = allow_fractional
        self.cash_rate = cash_rate
        self.min_trade_value = min_trade_value
        self.periods_per_year = periods_per_year

        if execution == "next_open":
            if open_prices is None:
                # 시가가 없으면 종가 체결로 자동 강등한다(더 보수적인 가정은 아니지만
                # 조용히 틀린 결과를 내는 것보다 명시적으로 동작을 바꾸는 편이 낫다).
                self.execution = "next_close"
                self.open_prices = None
            else:
                self.open_prices = open_prices.reindex(
                    index=self.prices.index, columns=self.prices.columns
                )
        else:
            self.open_prices = None

    # ------------------------------------------------------------------ 내부
    def _exec_prices(self, i: int) -> pd.Series:
        """i번째 봉의 체결 기준가. 시가가 비어 있으면 종가로 대체한다."""
        close = self.prices.iloc[i]
        if self.execution == "next_open" and self.open_prices is not None:
            return self.open_prices.iloc[i].fillna(close)
        return close

    def _mark(self, i: int, px: pd.Series | None = None) -> pd.Series:
        """i번째 봉의 평가 기준가.

        인자로 받은 가격(체결가 등)이 있으면 그걸 우선 쓰고, 값이 없는 종목만
        마지막으로 알려진 종가로 메운다.
        """
        last = self.valuation_prices.iloc[i]
        if px is None:
            return last
        usable = px.notna() & (px > 0)
        return px.where(usable, last)

    def _current_weights(self, shares: pd.Series, cash: float, px: pd.Series) -> pd.Series:
        value = shares * px.fillna(0.0)
        equity = float(value.sum()) + cash
        if equity <= 0:
            return pd.Series(0.0, index=shares.index)
        return value / equity

    def _rebalance_to(
        self,
        target: pd.Series,
        shares: pd.Series,
        cash: float,
        px: pd.Series,
        mark_px: pd.Series,
        date: pd.Timestamp,
    ) -> tuple[pd.Series, float, list[dict]]:
        """목표 비중에 맞춰 주문을 만들고 체결한다. (신주수, 현금, 체결기록) 반환.

        `px`는 이번 봉의 실제 체결 가능 가격이고, `mark_px`는 평가용 가격이다.
        가격이 없는 종목은 거래하지 않되, 보유분은 마지막 시세로 평가한다.
        """
        tradable = px.notna() & (px > 0)
        mark = mark_px.fillna(0.0)
        equity = float((shares * mark).sum()) + cash

        target_value = target.reindex(px.index).fillna(0.0) * equity
        target_value[~tradable] = (shares * mark)[~tradable]  # 거래 불가 종목은 유지

        current_value = shares * mark
        deltas = target_value - current_value

        fills: list[dict] = []
        # 매도를 먼저 처리해 현금을 확보한 뒤 매수한다(현금 부족으로 인한 미체결 방지).
        for side in ("sell", "buy"):
            wanted = deltas[deltas < 0] if side == "sell" else deltas[deltas > 0]
            for ticker, delta in wanted.items():
                if not tradable.get(ticker, False) or abs(delta) < self.min_trade_value:
                    continue
                model = self.costs.for_ticker(ticker)
                fill_px = model.fill_price(float(px[ticker]), side)
                if fill_px <= 0:
                    continue

                qty = delta / fill_px
                if not self.allow_fractional:
                    qty = np.trunc(qty)  # 0 방향으로 내림: 매수는 덜 사고 매도는 덜 판다
                if side == "sell":
                    qty = max(qty, -float(shares.get(ticker, 0.0)))  # 공매도 금지
                if abs(qty) < 1e-12:
                    continue

                notional = qty * fill_px
                fee = model.fee(notional, side)
                if side == "buy" and notional + fee > cash + 1e-9:
                    # 현금 한도까지만 매수한다.
                    affordable = max(cash - fee, 0.0)
                    qty = affordable / fill_px
                    if not self.allow_fractional:
                        qty = np.trunc(qty)
                    if qty <= 0:
                        continue
                    notional = qty * fill_px
                    fee = model.fee(notional, side)
                    if notional + fee > cash + 1e-9:
                        continue

                cash -= notional + fee
                shares[ticker] = float(shares.get(ticker, 0.0)) + qty
                fills.append(
                    {
                        "date": date,
                        "ticker": ticker,
                        "side": side,
                        "quantity": float(qty),
                        "price": float(fill_px),
                        "notional": float(notional),
                        "fee": float(fee),
                    }
                )
        return shares, cash, fills

    # ------------------------------------------------------------------ 실행
    def run(self) -> BacktestResult:
        dates = self.prices.index
        tickers = list(self.prices.columns)
        warmup = max(1, self.strategy.warmup())
        flags = rebalance_flags(dates, self.rebalance)
        daily_cash_rate = (1.0 + self.cash_rate) ** (1.0 / self.periods_per_year) - 1.0

        shares = pd.Series(0.0, index=tickers, dtype=float)
        cash = self.initial_cash
        pending: pd.Series | None = None

        equity_rows: list[float] = []
        weight_rows: list[pd.Series] = []
        cash_rows: list[float] = []
        all_fills: list[dict] = []

        for i, date in enumerate(dates):
            cash *= 1.0 + daily_cash_rate  # 현금 이자

            # 1) 전 봉에서 낸 신호를 이번 봉에서 체결한다.
            if pending is not None:
                exec_px = self._exec_prices(i)
                shares, cash, fills = self._rebalance_to(
                    pending, shares, cash, exec_px, self._mark(i, exec_px), date
                )
                all_fills.extend(fills)
                pending = None

            # 2) 종가로 평가한다. 시세가 없는 날은 마지막으로 알려진 종가를 쓴다.
            close = self._mark(i)
            equity = float((shares * close.fillna(0.0)).sum()) + cash
            equity_rows.append(equity)
            cash_rows.append(cash)
            weight_rows.append(self._current_weights(shares, cash, close))

            # 3) 신호를 낸다. 마지막 봉은 체결할 다음 봉이 없으므로 건너뛴다.
            if i + 1 >= warmup and bool(flags.iloc[i]) and i < len(dates) - 1:
                history = self.prices.iloc[: i + 1]
                target = self.strategy.target_weights(history)
                target = target.drop(index=[CASH], errors="ignore").reindex(tickers).fillna(0.0)
                current = weight_rows[-1]
                if needs_rebalance(current, target, self.band):
                    pending = target

        return BacktestResult(
            equity=pd.Series(equity_rows, index=dates, name="equity"),
            weights=pd.DataFrame(weight_rows, index=dates),
            trades=pd.DataFrame(
                all_fills,
                columns=["date", "ticker", "side", "quantity", "price", "notional", "fee"],
            ),
            cash=pd.Series(cash_rows, index=dates, name="cash"),
            initial_cash=self.initial_cash,
            periods_per_year=self.periods_per_year,
        )
