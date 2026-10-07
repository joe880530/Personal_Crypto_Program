"""실계좌 안전장치.

자동매매에서 계좌를 망가뜨리는 건 대개 전략이 아니라 사고다: 데이터 오류로
가격이 0이 되거나, 설정 실수로 한 종목에 전액이 들어가거나, 무한 루프로 같은
주문이 반복 제출되거나. 주문은 반드시 이 검사를 통과한 뒤에만 나간다.
"""

from __future__ import annotations

import dataclasses

from .order import Account, Order, Side


@dataclasses.dataclass(frozen=True)
class Violation:
    """위반 내역."""

    rule: str
    message: str
    ticker: str | None = None
    #: True면 해당 주문만 버리고 진행, False면 전체 실행을 중단한다.
    order_level: bool = True


@dataclasses.dataclass
class GuardReport:
    """검사 결과."""

    approved: list[Order]
    violations: list[Violation]

    @property
    def halted(self) -> bool:
        """계좌 단위 위반이 있어 전체를 중단해야 하는가."""
        return any(not v.order_level for v in self.violations)

    @property
    def ok(self) -> bool:
        return not self.violations

    def describe(self) -> str:
        if self.ok:
            return f"안전장치 통과 ({len(self.approved)}건 승인)"
        lines = [f"안전장치 위반 {len(self.violations)}건:"]
        for v in self.violations:
            scope = "주문 취소" if v.order_level else "전체 중단"
            lines.append(f"  [{v.rule}/{scope}] {v.message}")
        return "\n".join(lines)


@dataclasses.dataclass
class RiskGuard:
    """주문 제출 전 최종 검사.

    Attributes:
        max_order_value: 주문 1건의 최대 금액(절대액).
        max_order_pct: 주문 1건의 최대 금액을 평가액 대비 비율로. 절대액과 함께
            주면 둘 중 **더 엄격한 쪽**이 적용된다.
        max_position_weight: 한 종목이 차지할 수 있는 최대 비중.
        max_total_notional_pct: 1회 리밸런싱의 총 거래대금 한도. 기준은 평가액이
            아니라 총자산(보유평가 + 현금)이다. 미수가 생겼을 때 이 장치가
            정리 매도를 막지 않도록.
        max_orders: 1회 실행의 최대 주문 건수. 폭주 방지.
        allowed_tickers: 화이트리스트. None이면 제한 없음.
        min_price / max_price: 비정상 가격 탐지 범위.
        max_drawdown_stop: 최고점 대비 이 낙폭을 넘으면 신규 매수를 중단한다.
        allow_margin: 현금이 마이너스(미수)일 때도 매수를 허용할지. 기본은 금지다.
            빚이 있는 상태에서 노출을 더 늘릴 이유가 없고, 실제로 중복 매수로
            2배 레버리지가 된 적이 있다. 매도는 막지 않는다 — 그래야 벗어난다.
    """

    max_order_value: float | None = None
    max_order_pct: float | None = None
    max_position_weight: float = 0.40
    max_total_notional_pct: float = 1.0
    max_orders: int = 50
    allowed_tickers: set[str] | None = None
    min_price: float = 0.0
    max_price: float | None = None
    max_drawdown_stop: float | None = None
    allow_margin: bool = False

    def check(
        self,
        orders: list[Order],
        account: Account,
        prices: dict[str, float],
        peak_equity: float | None = None,
    ) -> GuardReport:
        """주문 목록을 검사해 승인된 주문과 위반 내역을 돌려준다."""
        violations: list[Violation] = []
        equity = account.equity(prices)

        if equity <= 0:
            violations.append(
                Violation("equity", f"계좌 평가액이 0 이하입니다({equity:,.0f})", order_level=False)
            )
            return GuardReport([], violations)

        if len(orders) > self.max_orders:
            violations.append(
                Violation(
                    "max_orders",
                    f"주문 {len(orders)}건이 한도 {self.max_orders}건을 초과했습니다",
                    order_level=False,
                )
            )
            return GuardReport([], violations)

        # 한도의 기준은 평가액이 아니라 **총자산**이다. 미수(외상매수)가 생기면
        # 평가액 = 자산 - 부채라 작아지는데, 그 상태를 벗어나려면 보유분을 평가액보다
        # 큰 금액만큼 팔아야 한다. 평가액을 기준으로 두면 이 장치가 복구를 막는다
        # (실제로 막혔다). 빚이 없는 계좌에서는 두 값이 같아 동작이 달라지지 않는다.
        gross = sum(
            pos.market_value(prices.get(t, pos.avg_price))
            for t, pos in account.positions.items()
        ) + max(account.cash, 0.0)
        cap = gross * self.max_total_notional_pct
        total_notional = sum(o.notional for o in orders)
        if total_notional > cap:
            violations.append(
                Violation(
                    "max_total_notional",
                    f"총 거래대금 {total_notional:,.0f}이 한도 {cap:,.0f}를 넘습니다",
                    order_level=False,
                )
            )
            return GuardReport([], violations)

        buying_blocked = False
        if self.max_drawdown_stop is not None and peak_equity and peak_equity > 0:
            dd = equity / peak_equity - 1.0
            if dd <= -abs(self.max_drawdown_stop):
                buying_blocked = True
                violations.append(
                    Violation(
                        "max_drawdown_stop",
                        f"낙폭 {dd:.2%}가 한도 {-abs(self.max_drawdown_stop):.2%}에 도달해"
                        " 신규 매수를 중단합니다(매도는 허용)",
                    )
                )

        approved: list[Order] = []
        for order in orders:
            problem = self._check_one(order, account, prices, equity, buying_blocked)
            if problem is None:
                approved.append(order)
            else:
                violations.append(problem)

        return GuardReport(approved, violations)

    def _order_limit(self, equity: float) -> float | None:
        """주문 1건의 한도. 절대액과 비율 중 더 엄격한 쪽.

        절대액만 두면 계좌 규모가 바뀔 때마다 손으로 고쳐야 하고, 잊으면
        한도가 사실상 풀린 채로 돈다. 비율은 계좌를 따라간다.
        """
        limits = [v for v in (self.max_order_value,
                              None if self.max_order_pct is None
                              else equity * self.max_order_pct) if v is not None]
        return min(limits) if limits else None

    def _check_one(
        self,
        order: Order,
        account: Account,
        prices: dict[str, float],
        equity: float,
        buying_blocked: bool,
    ) -> Violation | None:
        if self.allowed_tickers is not None and order.ticker not in self.allowed_tickers:
            return Violation("whitelist", f"{order.ticker}는 허용 목록에 없습니다", order.ticker)

        price = prices.get(order.ticker, order.reference_price or 0.0)
        if price <= self.min_price:
            return Violation("price", f"{order.ticker} 가격이 비정상입니다({price})", order.ticker)
        if self.max_price is not None and price > self.max_price:
            return Violation("price", f"{order.ticker} 가격이 상한을 넘습니다({price})", order.ticker)

        notional = order.quantity * price
        limit = self._order_limit(equity)
        if limit is not None and notional > limit:
            return Violation(
                "max_order_value",
                f"{order.ticker} 주문금액 {notional:,.0f}이 한도 {limit:,.0f} 초과",
                order.ticker,
            )

        if order.side is Side.BUY:
            # 미수 상태에서 더 사면 빚이 늘어난다. 매도는 막지 않는다 —
            # 막으면 벗어날 수가 없다.
            if not self.allow_margin and account.cash < 0:
                return Violation(
                    "margin",
                    f"현금이 {account.cash:,.0f}원(미수)이라 매수하지 않습니다."
                    " 먼저 보유분을 줄여 결제를 맞추세요",
                    order.ticker,
                )
            if buying_blocked:
                return Violation("max_drawdown_stop", f"{order.ticker} 신규 매수 차단", order.ticker)
            held = account.positions.get(order.ticker)
            held_qty = held.quantity if held else 0.0
            after_weight = (held_qty + order.quantity) * price / equity
            if after_weight > self.max_position_weight + 1e-9:
                return Violation(
                    "max_position_weight",
                    f"{order.ticker} 체결 후 비중 {after_weight:.2%}가"
                    f" 한도 {self.max_position_weight:.2%}를 넘습니다",
                    order.ticker,
                )
        else:
            held = account.positions.get(order.ticker)
            held_qty = held.quantity if held else 0.0
            if order.quantity > held_qty + 1e-9:
                return Violation(
                    "oversell",
                    f"{order.ticker} 보유 {held_qty:g}주 초과 매도({order.quantity:g}주)",
                    order.ticker,
                )
        return None
