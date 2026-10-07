"""주문 자료구조."""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum


class Side(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclasses.dataclass(frozen=True)
class Order:
    """제출 전 주문.

    Attributes:
        ticker: 종목 코드.
        side: 매수/매도.
        quantity: 주수. 항상 양수이며 방향은 `side`가 결정한다.
        order_type: 시장가/지정가.
        limit_price: 지정가 주문의 가격.
        reference_price: 주문 생성 시점의 기준가(금액 환산·검증용).
        note: 사람이 읽을 사유(예: "리밸런싱: 목표 40% vs 현재 46%").
    """

    ticker: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    reference_price: float | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"주문 수량은 양수여야 합니다: {self.ticker} {self.quantity}")
        if self.order_type is OrderType.LIMIT and not self.limit_price:
            raise ValueError(f"지정가 주문에는 limit_price가 필요합니다: {self.ticker}")

    @property
    def notional(self) -> float:
        """주문 예상 금액. 지정가가 있으면 그 가격을, 없으면 기준가를 쓴다."""
        price = self.limit_price or self.reference_price or 0.0
        return self.quantity * price

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side is Side.BUY else -self.quantity

    def describe(self) -> str:
        price = self.limit_price or self.reference_price
        px = f"@{price:,.2f}" if price else "@시장가"
        label = "매수" if self.side is Side.BUY else "매도"
        return f"{self.ticker} {label} {self.quantity:g}주 {px}"


@dataclasses.dataclass(frozen=True)
class Fill:
    """체결 결과."""

    order_id: str
    ticker: str
    side: Side
    quantity: float
    price: float
    fee: float
    timestamp: dt.datetime
    status: str = "filled"

    @property
    def notional(self) -> float:
        return self.quantity * self.price


@dataclasses.dataclass(frozen=True)
class Position:
    """보유 종목."""

    ticker: str
    quantity: float
    avg_price: float = 0.0

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_price) * self.quantity


@dataclasses.dataclass(frozen=True)
class Account:
    """계좌 요약."""

    cash: float
    currency: str = "KRW"
    positions: dict[str, Position] = dataclasses.field(default_factory=dict)

    def equity(self, prices: dict[str, float]) -> float:
        held = sum(p.market_value(prices.get(t, 0.0)) for t, p in self.positions.items())
        return self.cash + held
