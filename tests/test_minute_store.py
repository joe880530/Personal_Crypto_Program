"""분봉 보관소 검증.

1년에 걸쳐 조금씩 쌓는 저장소다. 중간에 조용히 어긋나면 1년 뒤에야 알게 되고,
그때는 되돌릴 방법이 없다(KIS가 1년치만 보관하므로).
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from stockbot.data.minute_store import MinuteStore


def bars(day: dt.date, n: int = 5, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(dt.datetime.combine(day, dt.time(9, 0)), periods=n, freq="min")
    return pd.DataFrame(
        {"open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 10.0},
        index=idx,
    )


def test_days_accumulate_without_losing_earlier_ones(tmp_path):
    """하루씩 쌓는 구조다. 새로 저장할 때 이전 날이 사라지면 안 된다."""
    store = MinuteStore(str(tmp_path))
    for offset in range(3):
        day = dt.date(2026, 9, 28) + dt.timedelta(days=offset)
        store.save("069500", day, bars(day))

    frame = store.read("069500")
    assert len(frame) == 15
    assert store.span("069500") == (dt.date(2026, 9, 28), dt.date(2026, 9, 30))
    assert frame.index.is_monotonic_increasing


def test_resaving_a_day_replaces_it_rather_than_duplicating(tmp_path):
    """장중에 받았다가 마감 후 다시 받는 경우가 있다. 겹치면 새 값이 이긴다."""
    store = MinuteStore(str(tmp_path))
    day = dt.date(2026, 9, 29)
    store.save("069500", day, bars(day, n=3, price=100.0))
    store.save("069500", day, bars(day, n=5, price=200.0))

    frame = store.read("069500")
    assert len(frame) == 5, "중복 없이 덮어써야 합니다"
    assert frame["close"].unique().tolist() == [200.0]


def test_empty_days_are_remembered_so_we_stop_asking(tmp_path):
    """휴장일은 0건으로 온다. 기억하지 않으면 매번 다시 물어본다.

    1년치를 채우는 동안 휴장일마다 헛호출이 쌓이고, 모의투자는 초당 제한이 빡빡하다.
    """
    store = MinuteStore(str(tmp_path))
    holiday = dt.date(2026, 10, 3)
    assert store.save("069500", holiday, pd.DataFrame()) == 0
    assert holiday in store.stored_days("069500")

    # 새 인스턴스도 파일만 읽고 알아야 한다.
    assert holiday in MinuteStore(str(tmp_path)).stored_days("069500")


def test_stored_days_covers_both_real_and_empty(tmp_path):
    store = MinuteStore(str(tmp_path))
    store.save("069500", dt.date(2026, 9, 29), bars(dt.date(2026, 9, 29)))
    store.save("069500", dt.date(2026, 10, 3), pd.DataFrame())
    assert store.stored_days("069500") == {dt.date(2026, 9, 29), dt.date(2026, 10, 3)}


def test_tickers_do_not_mix(tmp_path):
    store = MinuteStore(str(tmp_path))
    day = dt.date(2026, 9, 29)
    store.save("069500", day, bars(day, price=110.0))
    store.save("114260", day, bars(day, price=61.0))
    assert store.read("069500")["close"].unique().tolist() == [110.0]
    assert store.read("114260")["close"].unique().tolist() == [61.0]


def test_missing_columns_fail_loudly(tmp_path):
    """열이 빠진 채로 쌓이면 1년 뒤에야 알게 된다."""
    store = MinuteStore(str(tmp_path))
    broken = bars(dt.date(2026, 9, 29)).drop(columns=["volume"])
    with pytest.raises(ValueError, match="volume"):
        store.save("069500", dt.date(2026, 9, 29), broken)


def test_reading_an_unknown_ticker_is_empty_not_an_error(tmp_path):
    store = MinuteStore(str(tmp_path))
    assert store.read("999999").empty
    assert store.stored_days("999999") == set()
    assert store.span("999999") is None
