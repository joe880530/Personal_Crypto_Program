"""일봉 OHLC 한 종목.

기존 파이프라인(PriceData)은 포트폴리오 배분용이다 — 여러 종목, 환율 변환,
종가 중심. 돌파 전략은 단일 종목의 고가·저가가 필요하고 환율과 무관해서
모양이 다르다. 잘 돌아가는 쪽을 건드리지 않으려고 경로를 따로 둔다.

캐시는 같은 PriceCache를 쓴다. 저장 형식이 같으므로 두 벌이 되지 않는다.
"""

from __future__ import annotations

import pandas as pd

from .cache import PriceCache

#: 캐시 구획 이름. 종가만 담는 기존 캐시와 섞이면 안 된다.
PROVIDER = "krx_ohlc"

COLUMNS = ["open", "high", "low", "close", "volume"]


def load_daily_ohlc(
    ticker: str,
    start: str,
    end: str | None = None,
    cache_dir: str = "data/cache",
    downloader=None,
) -> pd.DataFrame:
    """일봉 OHLC를 받아 온다. 캐시에 있으면 모자란 구간만 더 받는다.

    Args:
        downloader: 테스트에서 갈아끼우기 위한 주입점. (ticker, start, end) -> DataFrame.
    """
    cache = PriceCache(cache_dir)
    cached = cache.read(PROVIDER, ticker)

    need_from = start
    if cached is not None and not cached.empty:
        last = cached.index.max()
        # 마지막 날짜 다음부터만 받는다. 겹치는 구간은 merge가 새 값으로 덮는다.
        need_from = (last - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        if end is not None and last >= pd.Timestamp(end):
            return _tidy(cached, start, end)

    fresh = (downloader or _download)(ticker, need_from, end)
    if fresh is None or fresh.empty:
        if cached is None or cached.empty:
            raise ValueError(
                f"{ticker} 일봉을 받지 못했습니다 ({need_from} ~ {end or '오늘'}).\n"
                "  종목코드(국내는 6자리)와 네트워크를 확인하세요."
            )
        return _tidy(cached, start, end)

    merged = cache.merge(PROVIDER, ticker, fresh)
    return _tidy(merged, start, end)


def _download(ticker: str, start: str, end: str | None) -> pd.DataFrame | None:
    try:
        import FinanceDataReader as fdr
    except ImportError as exc:  # pragma: no cover - 환경 의존
        raise ImportError(
            "국내 시장 데이터에는 finance-datareader가 필요합니다: "
            "pip install finance-datareader"
        ) from exc

    df = fdr.DataReader(ticker, start, end)
    if df is None or df.empty:
        return None
    df = df.rename(columns=str.lower)
    missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
    if missing:
        raise ValueError(
            f"{ticker} 응답에 {missing}가 없습니다. 받은 열: {sorted(df.columns)}"
        )
    keep = [c for c in COLUMNS if c in df.columns]
    df = df[keep].dropna(how="all")
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def _tidy(df: pd.DataFrame, start: str, end: str | None) -> pd.DataFrame:
    out = df.loc[start:end] if end else df.loc[start:]
    out = out.dropna(subset=[c for c in ("open", "high", "low", "close") if c in out.columns])
    if out.empty:
        raise ValueError(f"요청 구간에 일봉이 없습니다 ({start} ~ {end or '오늘'})")
    # 고가 < 저가 같은 값이 섞이면 돌파 판정이 조용히 엉뚱해진다.
    bad = out[out["high"] < out["low"]]
    if not bad.empty:
        raise ValueError(f"고가가 저가보다 낮은 날이 있습니다: {list(bad.index[:3])}")
    return out.sort_index()
