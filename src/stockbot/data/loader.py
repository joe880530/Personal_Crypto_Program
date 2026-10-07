"""여러 시장의 데이터를 하나의 가격표로 합친다.

국내와 미국을 같이 담으면 두 가지 문제가 생긴다:

1. **거래일이 다르다.** 한국 공휴일에 미국은 열려 있다. 날짜를 합집합으로 두고
   앞쪽 값으로만 채운다(ffill). 뒤에서 끌어오는 bfill은 미래 정보를 쓰는 셈이라
   절대 쓰지 않는다.
2. **통화가 다르다.** 비중 계산은 같은 통화로 환산한 뒤에 해야 의미가 있다.
"""

from __future__ import annotations

import dataclasses
import warnings

import pandas as pd

from .base import PriceData, PriceProvider


@dataclasses.dataclass(frozen=True)
class AssetSpec:
    """포트폴리오 구성 종목 하나의 명세.

    Attributes:
        ticker: 종목 코드(국내는 6자리, 미국은 심볼).
        market: 시장 코드. 데이터 제공자와 비용 모델을 고르는 키.
        currency: 거래 통화.
        name: 표시용 이름.
        lot_size: 최소 거래단위.
    """

    ticker: str
    market: str = "US"
    currency: str = "USD"
    name: str = ""
    lot_size: int = 1

    @property
    def label(self) -> str:
        return self.name or self.ticker


class MarketDataLoader:
    """시장별 제공자를 묶어 하나의 정렬된 가격표를 만든다.

    Args:
        providers: {시장코드: PriceProvider}.
        base_currency: 모든 가격을 환산할 기준 통화.
        fx_provider: 환율 제공자. 통화가 섞여 있을 때만 필요하다.
        ffill_limit: 결측 종가를 앞 값으로 채울 최대 일수. None이면 제한 없음.
        on_missing: 요청한 종목의 데이터를 못 받았을 때의 동작.
            ``"raise"``(기본)는 중단하고, ``"warn"``은 경고 후 남은 종목으로 진행한다.
            기본이 중단인 이유는 **조용히 빠지는 쪽이 더 위험하기 때문**이다.
            종목이 하나 빠지면 그건 설정한 포트폴리오가 아니라 다른 포트폴리오다.
    """

    def __init__(
        self,
        providers: dict[str, PriceProvider],
        base_currency: str = "KRW",
        fx_provider: PriceProvider | None = None,
        ffill_limit: int | None = 5,
        on_missing: str = "raise",
    ) -> None:
        if on_missing not in {"raise", "warn"}:
            raise ValueError("on_missing은 'raise' 또는 'warn'이어야 합니다")
        self.providers = providers
        self.base_currency = base_currency
        self.fx_provider = fx_provider
        self.ffill_limit = ffill_limit
        self.on_missing = on_missing

    def load(
        self, assets: list[AssetSpec], start: str, end: str | None = None
    ) -> PriceData:
        """명세대로 데이터를 받아 기준 통화로 환산하고 날짜를 정렬한다."""
        if not assets:
            return PriceData(close=pd.DataFrame())

        by_market: dict[str, list[AssetSpec]] = {}
        for asset in assets:
            by_market.setdefault(asset.market, []).append(asset)

        missing = [m for m in by_market if m not in self.providers]
        if missing:
            raise KeyError(f"데이터 제공자가 없는 시장: {sorted(missing)}")

        closes, opens = [], []
        for market, group in by_market.items():
            data = self.providers[market].fetch([a.ticker for a in group], start, end)
            if data.close.empty:
                continue
            closes.append(data.close)
            if data.open is not None and not data.open.empty:
                opens.append(data.open)

        if not closes:
            return PriceData(close=pd.DataFrame())

        close = pd.concat(closes, axis=1, sort=True).sort_index()
        self._report_missing(assets, set(close.columns), by_market)
        open_ = pd.concat(opens, axis=1, sort=True).sort_index() if opens else None

        rates = self._fx_rates(assets, close.index, start, end)
        close = self._convert(close, assets, rates)
        if open_ is not None:
            open_ = self._convert(open_, assets, rates)

        close = close.ffill(limit=self.ffill_limit)
        if open_ is not None:
            open_ = open_.reindex(index=close.index, columns=close.columns).ffill(
                limit=self.ffill_limit
            )

        # 전 종목에 값이 생긴 시점부터 시작해야 비중 계산이 의미를 갖는다.
        first_valid = close.apply(lambda s: s.first_valid_index()).max()
        if pd.notna(first_valid):
            close = close.loc[first_valid:]
            if open_ is not None:
                open_ = open_.loc[first_valid:]

        return PriceData(close=close, open=open_)

    def _report_missing(
        self,
        assets: list[AssetSpec],
        received: set[str],
        by_market: dict[str, list[AssetSpec]],
    ) -> None:
        """요청했는데 못 받은 종목을 알린다.

        종목이 조용히 빠지면, 설정한 것과 다른 포트폴리오를 백테스트하고도
        그 사실을 모른 채 결과를 믿게 된다. 안전자산이 빠지면 하락장 방어가
        통째로 사라지는데도 숫자는 멀쩡해 보인다.
        """
        if not received:
            # 하나도 못 받은 경우는 개별 종목 문제가 아니라 소스 설정이나 실행
            # 위치 문제일 때가 많다. 호출하는 쪽이 경로까지 포함한 더 나은
            # 진단을 내놓으므로 여기서는 비켜준다.
            return

        missing = [a for a in assets if a.ticker not in received]
        if not missing:
            return

        per_market: dict[str, list[str]] = {}
        for asset in missing:
            per_market.setdefault(asset.market, []).append(asset.label)

        detail = "; ".join(f"{market}: {', '.join(names)}" for market, names in sorted(per_market.items()))
        summary = (
            f"요청한 {len(assets)}종목 중 {len(missing)}종목의 데이터를 받지 못했습니다 -> {detail}"
        )

        if self.on_missing == "raise":
            hints = [summary, "", "확인할 것:"]
            if "KR" in per_market or "KR_ETF" in per_market:
                hints += [
                    "  - 국내 데이터에는 finance-datareader가 필요합니다: pip install finance-datareader",
                    "  - 국내 티커는 6자리 종목코드입니다(예: 069500). 따옴표로 감싸야 앞의 0이 유지됩니다.",
                ]
            if any(m not in {"KR", "KR_ETF"} for m in per_market):
                hints.append("  - 해외 데이터에는 yfinance가 필요합니다: pip install yfinance")
            hints += [
                "  - 네트워크/방화벽이 데이터 제공처를 막고 있지 않은지 확인하세요.",
                "",
                "빠진 종목 없이 진행하려면 설정에 data.on_missing: warn 을 넣으세요.",
                "다만 그 결과는 설정한 포트폴리오가 아닙니다.",
            ]
            raise ValueError("\n".join(hints))

        warnings.warn(summary + " (data.on_missing: warn 설정으로 계속 진행합니다)", stacklevel=3)

    # ------------------------------------------------------------------ 환율
    def _fx_rates(
        self, assets: list[AssetSpec], index: pd.Index, start: str, end: str | None
    ) -> dict[str, pd.Series]:
        """{통화: 기준통화 환산 계수} 시계열."""
        currencies = {a.currency for a in assets if a.currency != self.base_currency}
        if not currencies:
            return {}
        if self.fx_provider is None:
            raise ValueError(
                f"통화가 섞여 있는데({sorted(currencies)} -> {self.base_currency})"
                " fx_provider가 없습니다"
            )

        pairs = [f"{c}/{self.base_currency}" for c in sorted(currencies)]
        fx = self.fx_provider.fetch(pairs, start, end).close
        rates: dict[str, pd.Series] = {}
        for currency in sorted(currencies):
            pair = f"{currency}/{self.base_currency}"
            if pair not in fx.columns:
                raise KeyError(f"환율을 가져오지 못했습니다: {pair}")
            series = fx[pair].reindex(index.union(fx.index)).ffill().reindex(index)
            rates[currency] = series
        return rates

    def _convert(
        self, frame: pd.DataFrame, assets: list[AssetSpec], rates: dict[str, pd.Series]
    ) -> pd.DataFrame:
        if not rates:
            return frame
        currency_of = {a.ticker: a.currency for a in assets}
        out = frame.copy()
        for ticker in out.columns:
            currency = currency_of.get(ticker, self.base_currency)
            if currency in rates:
                out[ticker] = out[ticker] * rates[currency].reindex(out.index)
        return out
