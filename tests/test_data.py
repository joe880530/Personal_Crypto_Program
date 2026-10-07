import numpy as np
import pandas as pd
import pytest

from stockbot.data import AssetSpec, CsvProvider, FrameProvider, MarketDataLoader, PriceCache


@pytest.fixture
def kr_us_loader():
    kr_idx = pd.bdate_range("2024-01-02", periods=20)
    us_idx = pd.bdate_range("2024-01-01", periods=20)
    kr = FrameProvider(pd.DataFrame({"069500": np.linspace(30_000, 32_000, 20)}, index=kr_idx))
    us = FrameProvider(pd.DataFrame({"SPY": np.linspace(400.0, 420.0, 20)}, index=us_idx))
    fx = FrameProvider(pd.DataFrame({"USD/KRW": np.full(20, 1300.0)}, index=us_idx))
    return MarketDataLoader({"KR": kr, "US": us}, base_currency="KRW", fx_provider=fx)


def test_loader_converts_foreign_prices_to_base_currency(kr_us_loader):
    assets = [AssetSpec("069500", "KR", "KRW"), AssetSpec("SPY", "US", "USD")]
    data = kr_us_loader.load(assets, "2024-01-01")

    # 원본 USD 가격(2024-01-03은 US 3번째 거래일) * 1300원
    usd_price = np.linspace(400.0, 420.0, 20)[2]
    assert data.close.loc["2024-01-03", "SPY"] == pytest.approx(usd_price * 1300.0, rel=1e-9)
    # 원화 자산은 환산 없이 그대로여야 한다
    assert data.close.loc["2024-01-03", "069500"] == pytest.approx(np.linspace(30_000, 32_000, 20)[1])


def test_loader_aligns_different_trading_calendars(kr_us_loader):
    assets = [AssetSpec("069500", "KR", "KRW"), AssetSpec("SPY", "US", "USD")]
    data = kr_us_loader.load(assets, "2024-01-01")
    assert data.close.notna().all().all()
    assert data.close.index.is_monotonic_increasing


def test_loader_never_backfills():
    """앞쪽으로만 채운다. 뒤에서 끌어오면 미래 가격을 쓰는 셈이다."""
    idx = pd.bdate_range("2024-01-01", periods=10)
    late = pd.Series(np.nan, index=idx)
    late.iloc[5:] = 100.0  # 6번째 봉부터 상장
    early = pd.Series(np.linspace(50, 60, 10), index=idx)
    loader = MarketDataLoader(
        {"US": FrameProvider(pd.DataFrame({"LATE": late, "EARLY": early}))},
        base_currency="USD",
    )
    data = loader.load([AssetSpec("LATE", "US", "USD"), AssetSpec("EARLY", "US", "USD")], "2024-01-01")
    # 두 종목 모두 값이 생긴 시점부터 시작해야 한다
    assert data.close.index[0] == idx[5]
    assert data.close.notna().all().all()


def test_loader_requires_fx_when_currencies_differ():
    loader = MarketDataLoader(
        {"US": FrameProvider(pd.DataFrame({"SPY": [1.0, 2.0]}, index=pd.bdate_range("2024-01-01", periods=2)))},
        base_currency="KRW",
    )
    with pytest.raises(ValueError, match="fx_provider"):
        loader.load([AssetSpec("SPY", "US", "USD")], "2024-01-01")


def test_loader_rejects_market_without_provider():
    loader = MarketDataLoader({"US": FrameProvider(pd.DataFrame())}, base_currency="USD")
    with pytest.raises(KeyError, match="데이터 제공자가 없는 시장"):
        loader.load([AssetSpec("005930", "KR", "USD")], "2024-01-01")


def test_csv_provider_reads_directory(tmp_path):
    idx = pd.bdate_range("2024-01-01", periods=10)
    pd.DataFrame({"close": np.arange(10.0), "open": np.arange(10.0)}, index=idx).to_csv(
        tmp_path / "AAA.csv"
    )
    data = CsvProvider(tmp_path).fetch(["AAA", "MISSING"], "2024-01-01")
    assert list(data.close.columns) == ["AAA"]
    assert len(data.close) == 10


def test_csv_provider_requires_close_column(tmp_path):
    idx = pd.bdate_range("2024-01-01", periods=3)
    pd.DataFrame({"price": [1.0, 2.0, 3.0]}, index=idx).to_csv(tmp_path / "BBB.csv")
    with pytest.raises(ValueError, match="close"):
        CsvProvider(tmp_path).fetch(["BBB"], "2024-01-01")


def test_cache_round_trip_and_merge(tmp_path):
    cache = PriceCache(tmp_path)
    idx = pd.bdate_range("2024-01-01", periods=5)
    first = pd.DataFrame({"close": np.arange(5.0)}, index=idx)
    cache.write("test", "AAA", first)

    later = pd.bdate_range("2024-01-08", periods=5)
    merged = cache.merge("test", "AAA", pd.DataFrame({"close": np.arange(5.0, 10.0)}, index=later))
    assert len(merged) == 10
    assert cache.covers("test", "AAA", "2024-01-01", "2024-01-12")
    assert not cache.covers("test", "AAA", "2023-01-01", "2024-01-12")


def test_cache_sanitizes_ticker_with_slash(tmp_path):
    """'USD/KRW' 같은 티커가 경로로 해석되면 안 된다."""
    cache = PriceCache(tmp_path)
    idx = pd.bdate_range("2024-01-01", periods=3)
    cache.write("fx", "USD/KRW", pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx))
    assert cache.read("fx", "USD/KRW") is not None


def test_price_data_drops_sparse_tickers():
    from stockbot.data.base import PriceData

    idx = pd.bdate_range("2024-01-01", periods=10)
    sparse = pd.Series(np.nan, index=idx)
    sparse.iloc[-1] = 1.0
    data = PriceData(close=pd.DataFrame({"GOOD": np.arange(10.0), "SPARSE": sparse}, index=idx))
    assert list(data.dropna_tickers(0.9).close.columns) == ["GOOD"]


# ------------------------------------------------------------------ 누락 종목 처리
@pytest.fixture
def partial_loader():
    """미국 2종목은 있고 국내 1종목이 빠진 상황."""
    idx = pd.bdate_range("2024-01-01", periods=30)
    us = FrameProvider(
        pd.DataFrame(
            {"SPY": np.linspace(400, 420, 30), "QQQ": np.linspace(300, 330, 30)}, index=idx
        )
    )
    kr = FrameProvider(pd.DataFrame({"069500": np.linspace(30_000, 32_000, 30)}, index=idx))
    fx = FrameProvider(pd.DataFrame({"USD/KRW": np.full(30, 1300.0)}, index=idx))
    return us, kr, fx


ASSETS_WITH_MISSING = [
    AssetSpec("SPY", "US", "USD", "미국 S&P500"),
    AssetSpec("QQQ", "US", "USD", "나스닥100"),
    AssetSpec("069500", "KR_ETF", "KRW", "KODEX 200"),
    AssetSpec("114260", "KR_ETF", "KRW", "KODEX 국고채3년"),  # 데이터 없음
]


def test_missing_ticker_raises_by_default(partial_loader):
    """종목이 빠지면 조용히 넘어가지 말고 중단해야 한다.

    안전자산 하나가 빠지면 하락장 방어가 통째로 사라지는데도 숫자는 멀쩡해
    보인다. 설정한 것과 다른 포트폴리오를 백테스트하는 게 더 위험하다.
    """
    us, kr, fx = partial_loader
    loader = MarketDataLoader({"US": us, "KR_ETF": kr}, base_currency="KRW", fx_provider=fx)

    with pytest.raises(ValueError) as exc:
        loader.load(ASSETS_WITH_MISSING, "2024-01-01")

    message = str(exc.value)
    assert "KODEX 국고채3년" in message       # 어느 종목인지
    assert "finance-datareader" in message    # 국내 종목이니 해당 안내
    assert "on_missing" in message            # 넘어가는 방법


def test_missing_ticker_can_be_downgraded_to_warning(partial_loader):
    us, kr, fx = partial_loader
    loader = MarketDataLoader(
        {"US": us, "KR_ETF": kr}, base_currency="KRW", fx_provider=fx, on_missing="warn"
    )

    with pytest.warns(UserWarning, match="KODEX 국고채3년"):
        data = loader.load(ASSETS_WITH_MISSING, "2024-01-01")

    assert set(data.close.columns) == {"SPY", "QQQ", "069500"}


def test_complete_data_raises_nothing(kr_us_loader):
    assets = [AssetSpec("069500", "KR", "KRW"), AssetSpec("SPY", "US", "USD")]
    assert not kr_us_loader.load(assets, "2024-01-01").close.empty


def test_invalid_on_missing_rejected():
    with pytest.raises(ValueError, match="on_missing"):
        MarketDataLoader({}, on_missing="ignore")
