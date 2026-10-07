"""브로커 추상 인터페이스.

실계좌 연동은 이 인터페이스의 구현체를 추가하는 것으로 끝나야 한다.
전략·리밸런싱 코드가 특정 증권사 API에 직접 의존하면, 증권사를 바꿀 때나
모의투자로 되돌릴 때 전부 뜯어고쳐야 한다.

구현 예정:
    - `PaperBroker` (완료): 로컬 상태 파일 기반 모의 체결.
    - `KISBroker`: 한국투자증권 Open API. 모의투자 도메인을 먼저 붙인다.
    - `AlpacaBroker`: 미국 주식. paper-api.alpaca.markets로 먼저 붙인다.
"""

from __future__ import annotations

import abc

from .order import Account, Fill, Order, Position


class BrokerError(RuntimeError):
    """브로커 연동 실패."""


class Broker(abc.ABC):
    """주문 집행 대상."""

    name: str = "broker"
    #: 실제 돈이 오가는 계좌인지. 안전장치가 이 값을 보고 확인 절차를 강제한다.
    is_live: bool = False

    @abc.abstractmethod
    def get_account(self) -> Account:
        """현금과 보유 종목을 포함한 계좌 상태."""

    @abc.abstractmethod
    def get_positions(self) -> dict[str, Position]:
        """보유 종목 {티커: Position}."""

    @abc.abstractmethod
    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """현재가 조회."""

    @abc.abstractmethod
    def submit(self, order: Order) -> Fill:
        """주문 제출. 체결/접수 결과를 반환한다."""

    def submit_all(self, orders: list[Order]) -> list[Fill]:
        """여러 주문을 순서대로 제출한다.

        매도가 앞에 오도록 정렬된 목록을 그대로 받는 것을 전제로 한다.
        한 건이 실패해도 나머지를 계속 시도하되, 실패는 호출자가 알 수 있도록
        상태에 남긴다.
        """
        fills: list[Fill] = []
        for order in orders:
            try:
                fills.append(self.submit(order))
            except BrokerError as exc:  # pragma: no cover - 구현체별 예외 경로
                fills.append(self._rejected(order, str(exc)))
        return fills

    @staticmethod
    def _rejected(order: Order, reason: str) -> Fill:
        import datetime as dt

        return Fill(
            order_id="",
            ticker=order.ticker,
            side=order.side,
            quantity=0.0,
            price=0.0,
            fee=0.0,
            timestamp=dt.datetime.now(dt.timezone.utc),
            status=f"rejected: {reason}",
        )
