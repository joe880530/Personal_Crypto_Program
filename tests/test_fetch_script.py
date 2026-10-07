"""CSV 받아오기 스크립트 검증 (네트워크 없이).

이 스크립트는 샌드박스에서 업비트가 차단돼 있어 만든 것이라, 정작 여기서
실제 호출로 확인할 수 없다. 그래서 **업비트가 실제로 하는 두 가지 함정**을
가짜로 재현해서 스크립트가 거기 걸리지 않는지 본다.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "fetch_upbit_csv.py"


def load():
    spec = importlib.util.spec_from_file_location("fetch_upbit_csv", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(monkeypatch):
    module = load()
    monkeypatch.setattr(module, "PACE", 0)
    return module


def fake_server(total_days: int, hard_cap: int):
    """to 이전(미포함)을 최신순으로 주고, count를 넘기면 조용히 자른다."""
    base = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    bars = [base + dt.timedelta(days=i) for i in range(total_days)]
    calls = {"n": 0}

    def get(url: str):
        calls["n"] += 1
        q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        to = dt.datetime.strptime(q["to"].replace("%3A", ":"), "%Y-%m-%dT%H:%M:%SZ")
        to = to.replace(tzinfo=dt.timezone.utc)
        older = [b for b in bars if b < to]
        page = older[-min(int(q["count"]), hard_cap):]
        return [{
            "market": "KRW-BTC",
            "candle_date_time_utc": b.strftime("%Y-%m-%dT%H:%M:%S"),
            "opening_price": 100.0, "high_price": 110.0, "low_price": 90.0,
            "trade_price": 105.0 + i, "candle_acc_trade_volume": 1.5,
        } for i, b in enumerate(reversed(page))]

    return get, calls


def test_the_whole_range_arrives_even_when_pages_are_truncated(mod, monkeypatch):
    """200을 달라고 해도 50만 오는 상황. 요청한 개수를 믿으면 구간이 빈다."""
    get, calls = fake_server(total_days=600, hard_cap=50)
    monkeypatch.setattr(mod, "get", get)

    rows = mod.fetch("KRW-BTC", days=600, unit=None)
    assert len(rows) == 600, f"구간이 빕니다: {len(rows)}건"
    stamps = [r["candle_date_time_utc"] for r in rows]
    assert stamps == sorted(stamps), "시간순이어야 합니다"
    assert len(set(stamps)) == len(stamps), "중복이 있습니다"


def test_it_stops_when_there_is_no_more_history(mod, monkeypatch):
    """상장일 이전으로 가면 같은 구간이 계속 온다. 그대로 두면 무한루프다."""
    get, calls = fake_server(total_days=30, hard_cap=200)
    monkeypatch.setattr(mod, "get", get)

    rows = mod.fetch("KRW-BTC", days=760, unit=None)   # 있는 것보다 많이 달라고 한다
    assert len(rows) == 30
    assert calls["n"] <= 3, f"무한히 맴돌았습니다: {calls['n']}회"


def test_a_renamed_field_stops_the_run(mod, monkeypatch):
    """형식이 바뀌면 빈 CSV를 만들지 말고 멈춰야 한다."""
    def get(url):
        return [{"candle_date_time_utc": "2026-01-01T00:00:00", "price": 1}]

    monkeypatch.setattr(mod, "get", get)
    with pytest.raises(SystemExit) as exc:
        mod.fetch("KRW-BTC", days=10, unit=None)
    assert "trade_price" in str(exc.value)
