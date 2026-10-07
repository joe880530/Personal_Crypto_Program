"""시장 코드 -> 데이터 제공자 라우팅.

설정의 `market`은 거래비용 프리셋과 데이터 제공자를 함께 고르는 키다.
한쪽에만 등록된 시장이 생기면, 국내 ETF를 미국 소스에 물어보고
"상장폐지된 것 같다"는 답을 받는다. 실제로 KR_ETF에서 그랬다.
"""

from __future__ import annotations

import pytest

from stockbot.backtest.costs import PRESETS
from stockbot.config import from_dict, load_config
from stockbot.data.krx import KrxProvider
from stockbot.data.yahoo import YahooProvider
from stockbot.pipeline import MARKET_PROVIDERS, build_loader, provider_kind


@pytest.mark.parametrize(
    ("market", "expected"),
    [("KR", "krx"), ("KR_ETF", "krx"), ("US", "yahoo"), ("ZERO", "yahoo")],
)
def test_market_routes_to_expected_provider(market, expected):
    assert provider_kind(market) == expected


def test_unknown_market_fails_loudly():
    """모르는 시장을 조용히 한쪽으로 흘려보내면 안 된다."""
    with pytest.raises(KeyError, match="데이터 제공자를 모르는 시장"):
        provider_kind("JP")


def test_every_cost_preset_has_a_provider():
    """비용 프리셋으로 쓸 수 있는 시장은 데이터 제공자도 있어야 한다.

    둘 중 하나에만 등록되면 설정은 통과하는데 데이터만 조용히 어긋난다.
    """
    missing = sorted(set(PRESETS) - set(MARKET_PROVIDERS))
    assert not missing, f"데이터 제공자가 없는 비용 프리셋: {missing}"


def test_korean_assets_get_the_korean_provider():
    """국내 ETF 설정이 KRX 제공자로 연결돼야 한다."""
    config = from_dict({
        "name": "t",
        "base_currency": "KRW",
        "assets": [
            {"ticker": "069500", "market": "KR_ETF", "currency": "KRW"},
            {"ticker": "005930", "market": "KR", "currency": "KRW"},
            {"ticker": "SPY", "market": "US", "currency": "USD"},
        ],
    })
    loader = build_loader(config)

    assert isinstance(loader.providers["KR_ETF"], KrxProvider)
    assert isinstance(loader.providers["KR"], KrxProvider)
    assert isinstance(loader.providers["US"], YahooProvider)


def test_example_config_routes_every_asset():
    """리포지터리의 예시 설정이 실제로 조회 가능한 조합이어야 한다.

    예시 설정이 깨져 있으면 처음 쓰는 사람이 가장 먼저 밟는다.
    """
    config = load_config("config/portfolio.example.yaml")
    loader = build_loader(config)

    for asset in config.assets:
        provider = loader.providers[asset.market]
        expected = KrxProvider if asset.market.startswith("KR") else YahooProvider
        assert isinstance(provider, expected), (
            f"{asset.ticker}({asset.market})가 {type(provider).__name__}로 연결됐습니다"
        )


def test_fx_needed_only_for_foreign_currency():
    krw_only = from_dict({
        "name": "t",
        "base_currency": "KRW",
        "assets": [{"ticker": "069500", "market": "KR_ETF", "currency": "KRW"}],
    })
    assert build_loader(krw_only).fx_provider is None

    mixed = from_dict({
        "name": "t",
        "base_currency": "KRW",
        "assets": [
            {"ticker": "069500", "market": "KR_ETF", "currency": "KRW"},
            {"ticker": "SPY", "market": "US", "currency": "USD"},
        ],
    })
    assert build_loader(mixed).fx_provider is not None
