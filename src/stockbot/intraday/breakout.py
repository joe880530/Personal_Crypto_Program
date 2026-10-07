"""변동성 돌파 — 일봉 OHLC 근사 백테스트.

    진입가 = 당일 시가 + K x (전일 고가 - 전일 저가)
    당일 고가 >= 진입가 이면 그 가격에 진입했다고 본다
    청산 = 당일 종가

**근사의 한계를 먼저 적는다.** 일봉만으로는 고가가 언제 나왔는지 알 수 없어서,
돌파 이후에 나온 고가인지 이전인지 구분하지 못한다. 또 진입가에 정확히
체결됐다고 가정한다(실제로는 호가를 넘겨 받으므로 더 불리하다). 따라서 이
백테스트는 **실제보다 좋게 나온다**. 여기서 떨어지는 전략은 실전에서 더 나쁘다.
통과한 전략은 분봉으로 다시 재서 얼마나 부풀려졌는지 확인해야 한다.

그래도 일봉으로 먼저 재는 이유: KIS는 분봉을 1년만 보관한다. 1년이면 워크포워드
구간이 2~3개뿐이라 과최적화를 판별할 수 없다. 일봉은 10년 넘게 있다.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from ..backtest.costs import CostModel

#: 매매가 없던 날 현금에 붙는 연이자. 거래일 기준으로 나눠 쓴다.
TRADING_DAYS = 252


@dataclasses.dataclass(frozen=True)
class BreakoutParams:
    """변동성 돌파의 손잡이.

    Attributes:
        k: 전일 변동폭의 몇 배를 돌파 기준으로 삼을지. 작을수록 자주 진입한다.
        range_lookback: 변동폭을 몇 거래일로 볼지. 1이면 전일 하루.
    """

    k: float = 0.5
    range_lookback: int = 1

    def warmup(self) -> int:
        """첫 신호가 나오기 전에 소진되는 날 수.

        변동폭은 range_lookback일로 재고 그것을 하루 미뤄 쓰므로, 버려지는 날은
        range_lookback일이다. 여기에 1을 더하면 멀쩡한 데이터를 거부한다.
        """
        return self.range_lookback


@dataclasses.dataclass
class BreakoutResult:
    """백테스트 결과.

    Attributes:
        equity: 일별 자산곡선.
        trades: 진입한 날만 담은 표 (진입가·청산가·비용차감수익률).
        days: 검사한 거래일 수.
    """

    equity: pd.Series
    trades: pd.DataFrame
    days: int

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def trades_per_year(self) -> float:
        """연환산 진입 횟수. 비용이 여기에 비례하므로 성과만큼 중요한 수치다."""
        return self.n_trades / self.days * TRADING_DAYS if self.days else 0.0

    @property
    def hit_rate(self) -> float:
        if self.trades.empty:
            return 0.0
        return float((self.trades["net_return"] > 0).mean())


def entry_levels(ohlc: pd.DataFrame, params: BreakoutParams) -> pd.Series:
    """각 날의 돌파 기준가. 당일 시가와 **전일까지의** 변동폭만 쓴다."""
    span = (ohlc["high"] - ohlc["low"]).rolling(params.range_lookback).mean()
    # shift(1): 오늘 판단에 오늘 변동폭을 쓰면 미래를 보는 것이다.
    return ohlc["open"] + params.k * span.shift(1)


def run_breakout(
    ohlc: pd.DataFrame,
    params: BreakoutParams,
    costs: CostModel,
    initial_cash: float = 10_000_000.0,
    cash_rate: float = 0.0,
) -> BreakoutResult:
    """하루 안에 진입·청산하는 돌파 전략을 일봉으로 근사 검증한다."""
    if len(ohlc) <= params.warmup():
        raise ValueError(
            f"일봉이 {len(ohlc)}개뿐입니다. 이 설정은 최소 {params.warmup() + 1}개가 필요합니다."
        )

    levels = entry_levels(ohlc, params)
    daily_cash = (1 + cash_rate) ** (1 / TRADING_DAYS) - 1 if cash_rate else 0.0

    rows, returns, index = [], [], []
    for day, bar in ohlc.iterrows():
        level = levels.get(day, np.nan)
        if not np.isfinite(level):
            continue                      # 워밍업 구간
        index.append(day)

        if bar["high"] < level:
            returns.append(daily_cash)    # 돌파 없음 -> 현금
            continue

        # 진입가에 정확히 체결됐다고 본다(낙관적). 비용은 양쪽 모두 붙인다.
        buy = costs.fill_price(float(level), "buy")
        sell = costs.fill_price(float(bar["close"]), "sell")
        gross = sell / buy - 1.0
        fees = (costs.fee(buy, "buy") + costs.fee(sell, "sell")) / buy
        net = gross - fees

        returns.append(net)
        rows.append({
            "date": day, "entry": buy, "exit": sell,
            "gross_return": gross, "cost": fees, "net_return": net,
        })

    if not index:
        raise ValueError("검사할 수 있는 날이 없습니다(워밍업 구간만 있습니다)")

    equity = initial_cash * (1 + pd.Series(returns, index=pd.DatetimeIndex(index))).cumprod()
    trades = pd.DataFrame(rows).set_index("date") if rows else _empty_trades()
    return BreakoutResult(equity=equity, trades=trades, days=len(index))


def buy_and_hold(ohlc: pd.DataFrame, initial_cash: float = 10_000_000.0) -> pd.Series:
    """비교 기준. 첫날 시가에 사서 들고만 있었다면.

    이게 없으면 '수고한 값어치가 있었나'를 답할 수 없다.
    """
    first_open = float(ohlc["open"].iloc[0])
    return initial_cash * ohlc["close"] / first_open


def _empty_trades() -> pd.DataFrame:
    frame = pd.DataFrame(
        columns=["entry", "exit", "gross_return", "cost", "net_return"], dtype=float
    )
    frame.index = pd.DatetimeIndex([], name="date")
    return frame
