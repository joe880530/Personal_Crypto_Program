"""설정 -> 데이터 -> 전략 -> 결과를 잇는 조립 함수들.

CLI와 테스트, 나중에 붙일 스케줄러가 모두 같은 경로를 쓰도록 여기에 모은다.
"""

from __future__ import annotations

import pathlib

import pandas as pd

from . import metrics
from .backtest.engine import Backtester, BacktestResult
from .config import AppConfig
from .data.base import PriceData, PriceProvider
from .data.cache import PriceCache
from .data.csv_source import CsvProvider
from .data.krx import FxProvider, KrxProvider
from .data.loader import MarketDataLoader
from .data.yahoo import YahooProvider
from .execution.broker import Broker
from .execution.equity_log import EquityLog
from .execution.guards import GuardReport
from .execution.order import Order
from .execution.paper import PaperBroker
from .execution.planner import plan_orders
from .portfolio.base import CASH


#: 시장 코드 -> 데이터 제공자. 설정의 `market`은 거래비용 프리셋과 데이터
#: 제공자를 **함께** 고르는 키다. 여기에 없는 시장을 조용히 한쪽으로 흘려보내면,
#: 국내 ETF를 미국 소스에 물어보고 "상장폐지된 것 같다"는 엉뚱한 답을 받는다.
MARKET_PROVIDERS: dict[str, str] = {
    "KR": "krx",
    "KR_ETF": "krx",
    "US": "yahoo",
    "ZERO": "yahoo",
}


def provider_kind(market: str) -> str:
    """시장 코드에 맞는 데이터 제공자 종류를 돌려준다."""
    try:
        return MARKET_PROVIDERS[market]
    except KeyError:
        raise KeyError(
            f"데이터 제공자를 모르는 시장입니다: {market!r}. "
            f"사용 가능: {sorted(MARKET_PROVIDERS)}"
        ) from None


def build_loader(config: AppConfig, cache_dir: str = "data/cache") -> MarketDataLoader:
    """설정에 등장하는 시장에 맞는 데이터 제공자를 구성한다.

    설정의 `data.source`를 ``csv``로 두면 네트워크 없이 로컬 CSV만 읽는다.
    재현 가능한 백테스트와 테스트에 쓴다.
    """
    data_cfg = config.raw.get("data") or {}
    markets = {a.market for a in config.assets}
    providers: dict[str, PriceProvider] = {}

    if str(data_cfg.get("source", "auto")).lower() == "csv":
        root = data_cfg.get("path", "data/csv")
        for market in markets:
            providers[market] = CsvProvider(root)
        fx_provider = CsvProvider(root)
    else:
        cache = PriceCache(cache_dir)
        builders = {"krx": KrxProvider, "yahoo": YahooProvider}
        for market in markets:
            providers[market] = builders[provider_kind(market)](cache=cache)
        fx_provider = FxProvider(cache=cache)

    needs_fx = any(a.currency != config.base_currency for a in config.assets)
    return MarketDataLoader(
        providers=providers,
        base_currency=config.base_currency,
        fx_provider=fx_provider if needs_fx else None,
        ffill_limit=data_cfg.get("ffill_limit", 5),
        on_missing=data_cfg.get("on_missing", "raise"),
    )


def load_prices(
    config: AppConfig, loader: MarketDataLoader | None = None, cache_dir: str = "data/cache"
) -> PriceData:
    """설정 구간의 가격표를 만든다.

    데이터를 하나도 못 받으면 어디를 찾아봤는지까지 알려준다. "비어 있습니다"만
    보고 원인을 짐작하게 두면, 실제로는 실행 위치가 틀렸을 뿐인데도 설정을
    엉뚱하게 고치게 된다.
    """
    loader = loader or build_loader(config, cache_dir)
    data = loader.load(config.assets, config.backtest.start, config.backtest.end)
    if data.close.empty:
        raise ValueError(_no_data_hint(config, cache_dir))
    return data


def _no_data_hint(config: AppConfig, cache_dir: str) -> str:
    """데이터를 못 받았을 때의 진단 메시지."""
    import os

    data_cfg = config.raw.get("data") or {}
    source = str(data_cfg.get("source", "auto")).lower()
    lines = [
        f"가격 데이터를 하나도 받지 못했습니다 (종목 {len(config.assets)}개,"
        f" 기간 {config.backtest.start} ~ {config.backtest.end or '오늘'})."
    ]
    if source == "csv":
        root = pathlib.Path(data_cfg.get("path", "data/csv"))
        lines += [
            f"  데이터 소스: csv",
            f"  찾아본 경로: {root.resolve()}",
            f"  현재 작업 디렉터리: {os.getcwd()}",
        ]
        if not root.exists():
            lines.append("  -> 이 폴더가 없습니다. 설정의 data.path는 현재 작업 디렉터리 기준입니다.")
            lines.append("     프로젝트 루트에서 실행했는지 확인하고, 없으면 샘플 데이터를 먼저 만드세요:")
            lines.append("       python scripts/make_sample_data.py --out data/csv")
        else:
            found = sorted(f.name for f in root.glob("*.csv"))[:10]
            lines.append(f"  폴더 안 CSV: {found or '(없음)'}")
            lines.append(f"  설정의 티커: {config.tickers}")
            lines.append("  -> 파일명이 '{티커}.csv' 형식인지 확인하세요.")
    else:
        lines += [
            f"  데이터 소스: 온라인 ({sorted({a.market for a in config.assets})})",
            f"  캐시 경로: {pathlib.Path(cache_dir).resolve()}",
            "  -> 네트워크 연결, 티커 표기(국내는 6자리 종목코드), 조회 기간을 확인하세요.",
            "     회사망/방화벽이 데이터 제공처를 막는 경우도 있습니다.",
        ]
    return "\n".join(lines)


def run_backtest(config: AppConfig, prices: PriceData) -> BacktestResult:
    """설정대로 백테스트를 돌린다."""
    if prices.close.empty:
        raise ValueError("가격 데이터가 비어 있어 백테스트를 돌릴 수 없습니다")
    bt = config.backtest
    return Backtester(
        prices=prices.close,
        strategy=config.strategy,
        initial_cash=bt.initial_cash,
        rebalance=bt.rebalance,
        band=bt.band,
        costs=config.cost_book(),
        execution=bt.execution,
        open_prices=prices.open,
        allow_fractional=bt.allow_fractional,
        cash_rate=bt.cash_rate,
        min_trade_value=bt.min_trade_value,
    ).run()


def fixed_strategy_over(
    config: AppConfig,
    prices: PriceData,
    strategy,
    start,
    end,
) -> dict[str, float]:
    """전략 하나를 고정한 채 [start, end] 구간의 성과만 잘라서 낸다.

    워크포워드가 구간마다 전략을 바꿔 얻은 성과를, '그냥 하나 정해서 들고
    있었다면' 어땠는지와 같은 기간으로 비교하기 위한 것이다. 이 비교가 없으면
    선택이라는 수고가 값어치를 했는지 알 수 없다.

    백테스트는 전체 기간으로 돌린 뒤 자산곡선만 잘라낸다. 구간 앞부분을 버리고
    시작하면 지표(lookback)에 필요한 워밍업이 사라져 불공정해지기 때문이다.
    고정 전략은 학습이 없으므로 이렇게 해도 미래 정보가 새지 않는다.
    """
    original = config.strategy
    config.strategy = strategy
    try:
        equity = run_backtest(config, prices).equity
    finally:
        config.strategy = original

    window = equity.loc[(equity.index >= start) & (equity.index <= end)]
    if len(window) < 2:
        raise ValueError("비교할 구간이 너무 짧습니다")
    # 시작 자산을 워크포워드와 같게 맞춰야 수익률을 나란히 읽을 수 있다.
    scaled = window / float(window.iloc[0]) * config.backtest.initial_cash
    return metrics.summary(scaled, config.backtest.risk_free)


def current_target(config: AppConfig, prices: PriceData) -> pd.Series:
    """지금 시점의 목표 비중. 가장 최근 가격까지만 보고 계산한다."""
    if prices.close.empty:
        raise ValueError("가격 데이터가 비어 있습니다")
    warmup = config.strategy.warmup()
    if len(prices.close) < warmup:
        raise ValueError(
            f"데이터가 {len(prices.close)}봉뿐입니다. 이 전략은 최소 {warmup}봉이 필요하니"
            " backtest.start를 더 앞당기세요."
        )
    return config.strategy.target_weights(prices.close)


def build_broker(config: AppConfig, prices: PriceData | None = None) -> Broker:
    """설정의 broker 종류에 맞는 구현체를 만든다.

    - ``paper``: 로컬 상태 파일 기반 모의 체결. 네트워크가 필요 없다.
    - ``kis``: 한국투자증권 OpenAPI. .env의 KIS_* 값을 읽으며,
      KIS_PAPER=false를 **명시할 때만** 실계좌가 열린다.
    """
    kind = config.execution.broker.lower()
    if kind == "kis":
        from .execution.kis import KISBroker, KISCredentials, load_dotenv

        return KISBroker(
            credentials=KISCredentials.from_env(load_dotenv()),
            token_path=str(pathlib.Path(config.execution.state_path).parent / "kis_token.json"),
        )
    if kind != "paper":
        raise NotImplementedError(
            f"브로커 {kind!r}는 아직 구현되지 않았습니다. 사용 가능: 'paper', 'kis'.\n"
            "실계좌 연동은 execution/broker.py의 Broker 인터페이스를 구현해서 붙이세요."
        )

    price_source = None
    if prices is not None and not prices.close.empty:
        last = prices.close.ffill().iloc[-1]

        def price_source(tickers: list[str]) -> dict[str, float]:
            return {t: float(last[t]) for t in tickers if t in last and pd.notna(last[t])}

    return PaperBroker(
        state_path=config.execution.state_path,
        initial_cash=config.backtest.initial_cash,
        currency=config.base_currency,
        costs=config.cost_book(),
        price_source=price_source,
    )


def plan_rebalance(
    config: AppConfig, broker: Broker, prices: PriceData
) -> tuple[list[Order], GuardReport, pd.Series]:
    """목표 비중을 계산하고 주문을 만든 뒤 안전장치를 통과시킨다.

    Returns:
        (승인 전 주문, 안전장치 리포트, 목표 비중).
    """
    target = current_target(config, prices)
    last = prices.close.ffill().iloc[-1]
    price_map = {t: float(last[t]) for t in last.index if pd.notna(last[t])}

    account = broker.get_account()
    ex = config.execution
    orders = plan_orders(
        target_weights=target,
        positions=account.positions,
        prices=pd.Series(price_map),
        cash=account.cash,
        band=ex.band,
        lot_sizes=config.lot_sizes,
        allow_fractional=ex.allow_fractional,
        min_trade_value=ex.min_trade_value,
        cash_buffer=ex.cash_buffer,
        order_type=ex.as_order_type(),
        limit_slippage=ex.limit_slippage,
    )
    # 고점을 넘겨주지 않으면 max_drawdown_stop 이 **영원히 발동하지 않는다**.
    # 설정과 README에는 있는데 실제로는 없는 장치였다. 고점은 쌓아 둔 평가액
    # 기록에서 온다 — 기록이 없으면 None이고, 그때는 '낙폭 0'이 아니라
    # '아직 모른다'는 뜻으로 장치가 쉰다.
    report = config.guard.check(orders, account, price_map, peak_equity=equity_peak(config))
    return orders, report, target.drop(index=[CASH], errors="ignore")


def equity_log_path(config: AppConfig) -> pathlib.Path:
    """평가액 기록 파일. 계좌 상태 파일과 같은 폴더에 둔다."""
    return pathlib.Path(config.execution.state_path).parent / "equity.csv"


def equity_peak(config: AppConfig) -> float | None:
    """지금까지 기록된 최고 평가액. 기록이 없으면 None."""
    return EquityLog(equity_log_path(config)).peak()
