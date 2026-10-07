"""모의 브로커 — 실제 돈 없이 전체 파이프라인을 검증한다.

실계좌를 붙이기 전에 최소 수개월은 이걸로 돌려봐야 한다. 신호 생성부터 주문
수량 계산, 상태 관리까지 같은 코드 경로를 쓰기 때문에, 여기서 통과하지 못하는
버그는 실계좌에서도 그대로 재현된다.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import uuid

from ..backtest.costs import CostBook
from .broker import Broker, BrokerError
from .order import Account, Fill, Order, Position, Side


class PaperBroker(Broker):
    """JSON 파일에 상태를 저장하는 모의 계좌.

    Args:
        state_path: 계좌 상태 파일 경로. 없으면 initial_cash로 새로 만든다.
        initial_cash: 최초 예수금.
        currency: 계좌 통화.
        costs: 체결 시 적용할 비용 모델.
        price_source: 티커 목록을 받아 {티커: 현재가}를 돌려주는 함수.
    """

    name = "paper"
    is_live = False

    def __init__(
        self,
        state_path: str | pathlib.Path = ".state/paper_account.json",
        initial_cash: float = 10_000_000.0,
        currency: str = "KRW",
        costs: CostBook | None = None,
        price_source=None,
    ) -> None:
        self.state_path = pathlib.Path(state_path)
        self.currency = currency
        self.costs = costs or CostBook()
        self.price_source = price_source
        self._state = self._load(initial_cash)

    # ------------------------------------------------------------------ 상태
    def _load(self, initial_cash: float) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {
            "cash": float(initial_cash),
            "currency": self.currency,
            "positions": {},
            "history": [],
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # --------------------------------------------------------------- 조회 API
    def get_positions(self) -> dict[str, Position]:
        return {
            t: Position(ticker=t, quantity=p["quantity"], avg_price=p["avg_price"])
            for t, p in self._state["positions"].items()
            if p["quantity"] != 0
        }

    def get_account(self) -> Account:
        return Account(
            cash=float(self._state["cash"]),
            currency=self._state.get("currency", self.currency),
            positions=self.get_positions(),
        )

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        if self.price_source is None:
            raise BrokerError("price_source가 설정되지 않아 현재가를 조회할 수 없습니다")
        return self.price_source(tickers)

    # --------------------------------------------------------------- 주문 API
    def submit(self, order: Order) -> Fill:
        price = order.limit_price or order.reference_price
        if not price:
            prices = self.get_prices([order.ticker])
            price = prices.get(order.ticker)
        if not price or price <= 0:
            raise BrokerError(f"{order.ticker} 체결가를 결정할 수 없습니다")

        model = self.costs.for_ticker(order.ticker)
        fill_price = model.fill_price(float(price), order.side.value)
        qty = float(order.quantity)
        positions = self._state["positions"]
        held = positions.get(order.ticker, {"quantity": 0.0, "avg_price": 0.0})

        if order.side is Side.SELL:
            if qty > held["quantity"] + 1e-9:
                raise BrokerError(
                    f"{order.ticker} 보유 {held['quantity']:g}주보다 많이 매도할 수 없습니다({qty:g}주)"
                )
            notional = qty * fill_price
            fee = model.fee(notional, "sell")
            self._state["cash"] += notional - fee
            held["quantity"] -= qty
            if held["quantity"] <= 1e-9:
                positions.pop(order.ticker, None)
            else:
                positions[order.ticker] = held
        else:
            notional = qty * fill_price
            fee = model.fee(notional, "buy")
            if notional + fee > self._state["cash"] + 1e-6:
                raise BrokerError(
                    f"현금 부족: 필요 {notional + fee:,.0f} / 보유 {self._state['cash']:,.0f}"
                )
            self._state["cash"] -= notional + fee
            total_qty = held["quantity"] + qty
            # 평균단가에 수수료까지 포함해 실제 취득원가를 반영한다.
            held["avg_price"] = (
                held["quantity"] * held["avg_price"] + notional + fee
            ) / total_qty
            held["quantity"] = total_qty
            positions[order.ticker] = held

        fill = Fill(
            order_id=uuid.uuid4().hex[:12],
            ticker=order.ticker,
            side=order.side,
            quantity=qty,
            price=fill_price,
            fee=fee,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        self._state["history"].append(
            {
                "order_id": fill.order_id,
                "ticker": fill.ticker,
                "side": fill.side.value,
                "quantity": fill.quantity,
                "price": fill.price,
                "fee": fill.fee,
                "timestamp": fill.timestamp.isoformat(),
                "note": order.note,
            }
        )
        self.save()
        return fill
