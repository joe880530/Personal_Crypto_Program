"""목표 비중 + 현재 보유 -> 주문 리스트.

백테스트 엔진의 `_rebalance_to`와 같은 일을 하지만, 실계좌용이라 규칙이 더
빡빡하다: 정수 주수, 시장별 최소 거래단위, 수수료 여유분, 매도 우선 처리.
"""

from __future__ import annotations

import math

import pandas as pd

from ..portfolio.base import CASH
from .order import Order, OrderType, Position, Side


def plan_orders(
    target_weights: pd.Series,
    positions: dict[str, Position],
    prices: pd.Series,
    cash: float,
    *,
    band: float = 0.0,
    lot_sizes: dict[str, int] | None = None,
    allow_fractional: bool = False,
    min_trade_value: float = 0.0,
    cash_buffer: float = 0.005,
    order_type: OrderType = OrderType.MARKET,
    limit_slippage: float = 0.0,
) -> list[Order]:
    """목표 비중에 도달하기 위한 주문 목록을 만든다.

    Args:
        target_weights: 목표 비중(`CASH` 항목은 무시).
        positions: 현재 보유 {티커: Position}.
        prices: 현재가 Series.
        cash: 사용 가능 현금.
        band: 이 값(비중 %p) 이하의 괴리는 무시한다. 잦은 소액 매매를 막는다.
        lot_sizes: {티커: 최소 거래단위}. 미지정 시 1주.
        allow_fractional: 소수점 주식 허용 여부(미국 일부 브로커).
        min_trade_value: 이 금액 미만 주문은 만들지 않는다.
        cash_buffer: 수수료·가격변동 대비로 남겨둘 현금 비율.
        order_type: 시장가/지정가.
        limit_slippage: 지정가 주문 시 기준가 대비 허용 폭(예: 0.003 = 0.3%).

    Returns:
        매도가 앞, 매수가 뒤에 오도록 정렬된 주문 목록.
    """
    lot_sizes = lot_sizes or {}
    target = target_weights.drop(index=[CASH], errors="ignore").astype(float)

    valid = prices.reindex(target.index.union(pd.Index(positions.keys())))
    valid = valid[valid.notna() & (valid > 0)]
    if valid.empty:
        return []

    held_value = {t: p.quantity * float(valid.get(t, 0.0)) for t, p in positions.items()}
    equity = cash + sum(held_value.values())
    if equity <= 0:
        return []

    investable = equity * (1.0 - cash_buffer)
    orders: list[Order] = []

    universe = sorted(set(target.index) | set(positions.keys()))
    for ticker in universe:
        price = float(valid.get(ticker, 0.0))
        if price <= 0:
            continue  # 가격을 모르는 종목은 건드리지 않는다

        current_qty = positions[ticker].quantity if ticker in positions else 0.0
        current_w = (current_qty * price) / equity
        target_w = float(target.get(ticker, 0.0))

        if band > 0 and abs(target_w - current_w) <= band:
            continue

        desired_qty = (target_w * investable) / price
        lot = max(int(lot_sizes.get(ticker, 1)), 1)
        if not allow_fractional:
            # 목표 비중을 넘기지 않도록 항상 내림 처리한다.
            desired_qty = math.floor(desired_qty / lot) * lot

        delta = desired_qty - current_qty
        if abs(delta) * price < max(min_trade_value, 1e-9):
            continue
        if not allow_fractional:
            delta = math.trunc(delta / lot) * lot
            if delta == 0:
                continue

        side = Side.BUY if delta > 0 else Side.SELL
        quantity = abs(delta)
        if side is Side.SELL:
            quantity = min(quantity, current_qty)  # 공매도 금지
            if quantity <= 0:
                continue

        limit_price = None
        if order_type is OrderType.LIMIT:
            adj = 1.0 + limit_slippage if side is Side.BUY else 1.0 - limit_slippage
            limit_price = round(price * adj, 4)

        orders.append(
            Order(
                ticker=ticker,
                side=side,
                quantity=quantity,
                order_type=order_type,
                limit_price=limit_price,
                reference_price=price,
                note=f"목표 {target_w:.2%} / 현재 {current_w:.2%}",
            )
        )

    # 매도를 먼저 내보내 현금을 확보한 뒤 매수한다.
    orders.sort(key=lambda o: (o.side is Side.BUY, -o.notional))
    return _fit_to_cash(orders, cash, lot_sizes, allow_fractional)


def _fit_to_cash(
    orders: list[Order],
    cash: float,
    lot_sizes: dict[str, int],
    allow_fractional: bool,
) -> list[Order]:
    """매도 대금을 반영해도 현금이 모자라는 매수 주문을 줄이거나 버린다."""
    available = cash + sum(o.notional for o in orders if o.side is Side.SELL)
    fitted: list[Order] = []
    for order in orders:
        if order.side is Side.SELL:
            fitted.append(order)
            continue
        if order.notional <= available:
            available -= order.notional
            fitted.append(order)
            continue

        price = order.limit_price or order.reference_price or 0.0
        if price <= 0:
            continue
        lot = max(int(lot_sizes.get(order.ticker, 1)), 1)
        qty = available / price
        if not allow_fractional:
            qty = math.floor(qty / lot) * lot
        if qty <= 0:
            continue
        available -= qty * price
        fitted.append(
            Order(
                ticker=order.ticker,
                side=order.side,
                quantity=qty,
                order_type=order.order_type,
                limit_price=order.limit_price,
                reference_price=order.reference_price,
                note=order.note + " (현금 한도로 축소)",
            )
        )
    return fitted
