"""보관된 분봉이 온전한지 보여주는 기능 검증.

분봉이 적을 수 있는 이유는 두 가지인데 결과가 전혀 다르다. 체결이 없던 분은 봉도
없는 것이 정상이고, 페이징이 일찍 멈춘 것은 아침이 통째로 빈 것이라 1년 뒤 일중
연구를 망친다. 건수만 보면 둘이 구분되지 않는다 — 시작 시각이 구분해 준다.

기준이 되는 분 수를 잘못 잡으면 이 기능 자체가 소음이 된다. 15:20~15:30은
장마감 동시호가라 분봉이 없는데, 정규장 391분으로 세면 완벽한 날에도 매일
"11분 없음"이 뜬다.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from stockbot import cli
from stockbot.data.minute_store import MinuteStore

DAY = dt.date(2026, 10, 1)


class Asset:
    def __init__(self, ticker, name=""):
        self.ticker, self.name = ticker, name


class Config:
    def __init__(self, *assets):
        self.assets = list(assets)


def bars(day: dt.date, start: dt.time, count: int, step: int = 1) -> pd.DataFrame:
    base = dt.datetime.combine(day, start)
    idx = pd.DatetimeIndex([base + dt.timedelta(minutes=i * step) for i in range(count)])
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)


def report(tmp_path, capsys, frame, ticker="069500") -> str:
    store = MinuteStore(str(tmp_path))
    if frame is not None:
        store.save(ticker, DAY, frame)
    cli._report_stored_day(Config(Asset(ticker, "KODEX 200")), str(tmp_path), DAY)
    return capsys.readouterr().out


def test_a_full_session_reports_no_gap(tmp_path, capsys):
    """연속매매 09:00~15:19를 한 분도 안 빠뜨린 날. 실제로 069500이 이랬다."""
    out = report(tmp_path, capsys, bars(DAY, dt.time(9, 0), 380))
    assert "380건" in out
    assert "09:00 ~ 15:19" in out
    assert "체결 없는 분 0분" in out
    assert "!" not in out


def test_scattered_gaps_are_not_flagged(tmp_path, capsys):
    """체결이 없던 분은 봉이 없는 것이 정상이다. 09:00에 시작했으면 온전하다."""
    out = report(tmp_path, capsys, bars(DAY, dt.time(9, 0), 190, step=2))
    assert "190건" in out
    assert "09:00" in out
    assert "아침이 빠졌을" not in out, "시작이 09:00인데 경고하면 매일 헛경고를 본다"


def test_a_truncated_morning_is_flagged(tmp_path, capsys):
    """342건이 09:49부터 시작하면 아침 49분이 통째로 빠진 것이다.

    건수만 보면 '체결 없는 분이 좀 있었나 보다'와 구분되지 않는다. 시작 시각이
    구분해 준다.
    """
    out = report(tmp_path, capsys, bars(DAY, dt.time(9, 49), 342))
    assert "342건" in out
    assert "09:49" in out
    assert "아침이 빠졌을 수 있습니다" in out


def test_a_day_never_fetched_is_distinct_from_a_holiday(tmp_path, capsys):
    """'아직 안 받음'과 '휴장이라 없음'은 할 일이 다르다."""
    missing = report(tmp_path, capsys, None)
    assert "아직 받지 않았습니다" in missing

    store = MinuteStore(str(tmp_path))
    store.save("114260", DAY, pd.DataFrame())      # 빈 날로 기록
    cli._report_stored_day(Config(Asset("114260")), str(tmp_path), DAY)
    assert "빈 날로 기록됨" in capsys.readouterr().out


def test_the_session_length_counts_continuous_trading_only():
    """15:20~15:30은 장마감 동시호가라 분봉이 없다. 정규장 391분으로 세면 안 된다.

    391로 두면 한 분도 안 빠진 날에도 매일 "11분 없음"이 뜬다. 늘 뜨는 경고는
    경고가 아니라 소음이고, 진짜 빠진 날을 가린다.
    """
    assert cli.SESSION_MINUTES == 380

    full = bars(DAY, dt.time(9, 0), cli.SESSION_MINUTES)
    assert full.index.max().strftime("%H:%M") == "15:19", "연속매매 마지막 봉"
