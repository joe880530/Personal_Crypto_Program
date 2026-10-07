"""거래비용 모델.

백테스트가 실전과 벌어지는 가장 큰 원인이 비용 누락이다. 특히 리밸런싱이
잦은 전략은 수수료·세금·슬리피지를 빼면 수익이 통째로 사라지기도 한다.
"""

from __future__ import annotations

import dataclasses

BPS = 1e-4


@dataclasses.dataclass(frozen=True)
class CostModel:
    """편도 거래비용.

    Attributes:
        commission_bps: 위탁수수료(bp). 매수·매도 양쪽에 붙는다.
        slippage_bps: 체결 슬리피지(bp). 호가 스프레드와 시장충격의 근사치.
        sell_tax_bps: 매도 시에만 붙는 세금(bp). 국내 주식 증권거래세 등.
        min_commission: 건당 최소 수수료(계좌 통화 기준).
    """

    commission_bps: float = 1.5
    slippage_bps: float = 5.0
    sell_tax_bps: float = 0.0
    min_commission: float = 0.0

    def fill_price(self, price: float, side: str) -> float:
        """슬리피지를 반영한 체결가. 매수는 불리하게 위로, 매도는 아래로."""
        slip = price * self.slippage_bps * BPS
        return price + slip if side == "buy" else max(price - slip, 0.0)

    def fee(self, notional: float, side: str) -> float:
        """체결 금액에 대한 수수료 + 세금."""
        notional = abs(notional)
        if notional == 0:
            return 0.0
        commission = max(notional * self.commission_bps * BPS, self.min_commission)
        tax = notional * self.sell_tax_bps * BPS if side == "sell" else 0.0
        return commission + tax


# 참고용 프리셋. 실제 수치는 본인 증권사 수수료표로 반드시 교체할 것.
PRESETS: dict[str, CostModel] = {
    # 국내 주식: 온라인 수수료 ~0.015% + 증권거래세 0.18%(2025년 코스피/코스닥 기준)
    "KR": CostModel(commission_bps=1.5, slippage_bps=5.0, sell_tax_bps=18.0),
    # 국내 상장 ETF: 거래세 없음
    "KR_ETF": CostModel(commission_bps=1.5, slippage_bps=4.0, sell_tax_bps=0.0),
    # 미국 주식/ETF: 수수료 ~0.07~0.25%, SEC 수수료는 매도에만 소액
    "US": CostModel(commission_bps=7.0, slippage_bps=5.0, sell_tax_bps=0.3),
    # 업비트 KRW 마켓. 사용자 계정에서 확인한 수치(2026-10-07):
    #   일반주문(지정가/시장가) 0.05%,  예약주문 KRW 0.139%
    # **예약주문을 쓰면 안 된다.** 왕복 0.278%로 일반주문의 2.8배다.
    # 호가 단위가 가격대마다 달라 슬리피지는 넉넉히 잡는다. 거래세는 없다.
    "UPBIT_KRW": CostModel(commission_bps=5.0, slippage_bps=5.0, sell_tax_bps=0.0),
    # 같은 조건에서 예약주문을 썼을 때. 비교용으로만 둔다.
    "UPBIT_KRW_RESERVED": CostModel(commission_bps=13.9, slippage_bps=5.0, sell_tax_bps=0.0),
    "ZERO": CostModel(commission_bps=0.0, slippage_bps=0.0, sell_tax_bps=0.0),
}


class CostBook:
    """종목별로 다른 비용 모델을 적용한다(국내/미국 혼합 포트폴리오용)."""

    def __init__(
        self,
        default: CostModel | None = None,
        per_ticker: dict[str, CostModel] | None = None,
    ) -> None:
        self.default = default or CostModel()
        self.per_ticker = per_ticker or {}

    def for_ticker(self, ticker: str) -> CostModel:
        return self.per_ticker.get(ticker, self.default)

    @classmethod
    def from_markets(
        cls, ticker_markets: dict[str, str], default_market: str = "US"
    ) -> "CostBook":
        """{티커: 시장코드} 매핑으로 비용표를 구성한다."""
        unknown = sorted({m for m in ticker_markets.values() if m not in PRESETS})
        if unknown:
            raise ValueError(f"알 수 없는 시장 프리셋: {unknown}. 사용 가능: {sorted(PRESETS)}")
        return cls(
            default=PRESETS[default_market],
            per_ticker={t: PRESETS[m] for t, m in ticker_markets.items()},
        )
