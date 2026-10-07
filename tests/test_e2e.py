"""설정 파일 -> 데이터 -> 백테스트 -> 주문까지 전체 경로를 오프라인으로 검증한다."""

from __future__ import annotations

import argparse
import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest
import yaml

from stockbot import cli
from stockbot.cli import main
from stockbot.config import load_config
from stockbot.pipeline import build_broker, load_prices, plan_rebalance, run_backtest

from .conftest import make_prices


@pytest.fixture
def project(tmp_path):
    """CSV 데이터와 설정 파일을 갖춘 임시 프로젝트."""
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    prices = make_prices(n=700, seed=11)
    for ticker in prices.columns:
        pd.DataFrame(
            {"close": prices[ticker], "open": prices[ticker].shift(1).bfill()}
        ).to_csv(csv_dir / f"{ticker}.csv")

    config = {
        "name": "e2e",
        "base_currency": "KRW",
        "default_market": "KR_ETF",
        "assets": [
            {"ticker": t, "market": "KR_ETF", "currency": "KRW"} for t in prices.columns
        ],
        "strategy": {
            "type": "momentum_risk_parity",
            "params": {
                "momentum": {"lookbacks": [21, 63, 126], "top_n": 2, "safe_asset": "STEADY"},
                "sizer": {"lookback": 90, "max_weight": 0.6},
            },
        },
        "backtest": {
            "start": "2018-01-01",
            "initial_cash": 10_000_000,
            "rebalance": "M",
            "band": 0.02,
            "execution": "next_open",
            "allow_fractional": False,
            "min_trade_value": 50_000,
        },
        "data": {"source": "csv", "path": str(csv_dir)},
        "execution": {
            "broker": "paper",
            "state_path": str(tmp_path / "acct.json"),
            "min_trade_value": 100_000,
            "band": 0.02,
        },
        "risk": {"max_position_weight": 0.7, "max_orders": 20},
    }
    path = tmp_path / "portfolio.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path, tmp_path


def test_full_backtest_pipeline(project):
    config_path, _ = project
    config = load_config(config_path)
    prices = load_prices(config)
    assert not prices.close.empty

    result = run_backtest(config, prices)
    summary = result.summary()

    assert len(result.equity) == len(prices.close)
    assert result.equity.iloc[0] == pytest.approx(config.backtest.initial_cash)
    assert (result.equity > 0).all()
    assert len(result.trades) > 0
    assert summary["total_costs"] > 0
    # 정수 주수 설정이므로 소수점 체결이 있으면 안 된다
    assert np.allclose(result.trades["quantity"], np.round(result.trades["quantity"]))


def test_plan_rebalance_from_empty_account(project):
    config_path, _ = project
    config = load_config(config_path)
    prices = load_prices(config)
    broker = build_broker(config, prices)

    orders, report, target = plan_rebalance(config, broker, prices)
    assert orders, "빈 계좌라면 매수 주문이 나와야 합니다"
    assert all(o.side.value == "buy" for o in orders)
    assert report.ok, report.describe()
    assert target.sum() <= 1.0 + 1e-9


def test_orders_move_account_towards_target(project):
    """주문을 체결하면 실제 보유 비중이 목표에 가까워져야 한다."""
    config_path, _ = project
    config = load_config(config_path)
    prices = load_prices(config)
    broker = build_broker(config, prices)

    orders, report, target = plan_rebalance(config, broker, prices)
    broker.submit_all(report.approved)

    account = broker.get_account()
    last = prices.close.ffill().iloc[-1]
    price_map = {t: float(last[t]) for t in last.index}
    equity = account.equity(price_map)

    for ticker, want in target[target > 0.01].items():
        got = account.positions[ticker].quantity * price_map[ticker] / equity
        assert got == pytest.approx(want, abs=0.02), f"{ticker}: 목표 {want:.2%} vs 실제 {got:.2%}"


def test_second_rebalance_is_a_no_op(project):
    """방금 맞춰놓은 직후에 다시 돌리면 주문이 없어야 한다(무한 매매 방지)."""
    config_path, _ = project
    config = load_config(config_path)
    prices = load_prices(config)
    broker = build_broker(config, prices)

    _, report, _ = plan_rebalance(config, broker, prices)
    broker.submit_all(report.approved)

    orders, _, _ = plan_rebalance(config, broker, prices)
    assert orders == []


def test_cli_backtest_writes_result_files(project, capsys):
    config_path, tmp_path = project
    out = tmp_path / "out"
    code = main(["-c", str(config_path), "backtest", "--out", str(out)])
    assert code == 0

    captured = capsys.readouterr().out
    assert "연평균 수익률" in captured
    assert "최대 낙폭" in captured

    result_dir = out / "e2e"
    assert (result_dir / "equity.csv").exists()
    assert (result_dir / "trades.csv").exists()
    summary = json.loads((result_dir / "summary.json").read_text(encoding="utf-8"))
    assert "cagr" in summary


def test_cli_signal_prints_target_weights(project, capsys):
    config_path, _ = project
    assert main(["-c", str(config_path), "signal"]) == 0
    assert "목표 비중" in capsys.readouterr().out


def test_cli_trade_is_dry_run_by_default(project, capsys):
    config_path, tmp_path = project
    assert main(["-c", str(config_path), "trade"]) == 0
    assert "--execute" in capsys.readouterr().out
    assert not (tmp_path / "acct.json").exists(), "모의 출력인데 계좌 상태가 바뀌었습니다"


def test_cli_trade_executes_with_flag(project, capsys):
    config_path, tmp_path = project
    assert main(["-c", str(config_path), "trade", "--execute"]) == 0
    assert (tmp_path / "acct.json").exists()
    state = json.loads((tmp_path / "acct.json").read_text(encoding="utf-8"))
    assert state["positions"]


def test_second_execute_on_the_same_day_is_blocked(project, capsys):
    """같은 날 두 번 내면 목표 비중의 두 배를 산다. 실제로 그렇게 됐다.

    결제 전 예수금을 '남은 현금'으로 읽는 버그와 겹쳐 069500을 45주씩 두 번
    샀다. 현금 계산은 고쳤지만, 무인 운영에서는 방어선이 하나 더 필요하다.
    """
    config_path, tmp_path = project
    assert main(["-c", str(config_path), "trade", "--execute"]) == 0
    capsys.readouterr()

    assert main(["-c", str(config_path), "trade", "--execute"]) == 3
    err = capsys.readouterr().err
    assert "이미 주문을 제출했습니다" in err
    assert "--again" in err, "의도적으로 한 번 더 낼 방법을 알려줘야 합니다"


def test_repeat_is_possible_when_asked_for_explicitly(project, capsys):
    config_path, _ = project
    assert main(["-c", str(config_path), "trade", "--execute"]) == 0
    assert main(["-c", str(config_path), "trade", "--execute", "--again"]) == 0


def test_dry_run_does_not_consume_the_daily_slot(project, capsys):
    """계획만 본 것이 그날의 제출로 기록되면 정작 낼 때 막힌다."""
    config_path, _ = project
    assert main(["-c", str(config_path), "trade"]) == 0
    assert main(["-c", str(config_path), "trade", "--execute"]) == 0


def another_day_this_month(today: dt.date) -> dt.date:
    """오늘과 **반드시 다른**, 같은 달의 날짜.

    today.replace(day=1)로 잡았다가 하필 1일에 테스트를 돌려 두 날짜가 같아졌고,
    하루 단위로 되돌리는 변이를 못 잡았다. 날짜를 쓰는 테스트는 돌리는 날에
    따라 결과가 달라지면 안 된다.
    """
    return today.replace(day=2 if today.day == 1 else 1)


def test_a_second_submission_later_in_the_same_month_is_blocked(project, capsys):
    """스케줄러를 15·16·17일로 걸면 하루 단위 방어선은 통과해 버린다.

    15일이 주말이면 그달을 거르니까 세 날짜로 거는데, 15일에 정상 제출한 뒤
    16일에 또 내면 목표 비중의 두 배를 산다. 주기가 한 달이면 한 달에 한 번이다.
    """
    config_path, tmp_path = project
    assert main(["-c", str(config_path), "trade", "--execute"]) == 0
    capsys.readouterr()

    # 같은 달의 **다른 날**에 이미 냈던 것으로 꾸민다.
    log = tmp_path / "last_execute.json"
    earlier = another_day_this_month(dt.date.today()).isoformat()
    log.write_text(json.dumps({"date": earlier, "window": dt.date.today().strftime("%Y-%m")}),
                   encoding="utf-8")

    assert main(["-c", str(config_path), "trade", "--execute"]) == 3
    err = capsys.readouterr().err
    assert earlier in err, "언제 냈는지 보여줘야 합니다"
    assert "--again" in err


def test_an_old_record_without_a_window_still_blocks(project, capsys):
    """판올림 직후 한 번은 방어선이 비어 있게 되는 일이 없어야 한다."""
    config_path, tmp_path = project
    assert main(["-c", str(config_path), "trade", "--execute"]) == 0
    capsys.readouterr()

    # 하루 단위만 기록하던 때의 파일 모양 (window 없음)
    log = tmp_path / "last_execute.json"
    log.write_text(json.dumps({"date": another_day_this_month(dt.date.today()).isoformat()}),
                   encoding="utf-8")

    assert main(["-c", str(config_path), "trade", "--execute"]) == 3


def test_the_window_follows_the_configured_cadence():
    """'M'을 코드에 박으면 주간 전략으로 바꿨을 때 조용히 한 달에 한 번만 낸다."""
    class Cfg:
        class backtest:
            rebalance = "M"

    day = dt.date(2026, 10, 16)
    assert cli._execute_window(Cfg(), day) == "2026-10"

    Cfg.backtest.rebalance = "W"
    assert cli._execute_window(Cfg(), day) == "2026-W42"
    assert cli._execute_window(Cfg(), dt.date(2026, 10, 19)) != "2026-W42", "다음 주는 새 주기"

    Cfg.backtest.rebalance = "D"
    assert cli._execute_window(Cfg(), day) == "2026-10-16"

    Cfg.backtest.rebalance = "Q"
    assert cli._execute_window(Cfg(), day) == "2026-Q4"
    assert cli._execute_window(Cfg(), dt.date(2026, 9, 30)) == "2026-Q3"

    Cfg.backtest.rebalance = "Y"
    assert cli._execute_window(Cfg(), day) == "2026"


def test_an_unknown_cadence_falls_back_to_one_day():
    """모르는 주기에 막지 않는 쪽으로 물러나면 방어선이 통째로 사라진다."""
    class Cfg:
        class backtest:
            rebalance = "무슨주기"

    assert cli._execute_window(Cfg(), dt.date(2026, 10, 16)) == "2026-10-16"


def test_snapshot_records_todays_equity(project, capsys):
    config_path, tmp_path = project
    assert main(["-c", str(config_path), "trade", "--execute"]) == 0
    capsys.readouterr()

    assert main(["-c", str(config_path), "snapshot"]) == 0
    assert "평가액" in capsys.readouterr().out

    from stockbot.execution.equity_log import EquityLog
    rows = EquityLog(tmp_path / "equity.csv").rows()
    assert len(rows) == 1
    assert rows[0]["date"] == dt.date.today()
    assert rows[0]["equity"] > 0


def test_report_needs_more_than_one_day(project, capsys):
    """하루치로 수익률을 내면 0%가 나온다. 없는 것을 있는 것처럼 보이면 안 된다."""
    config_path, _ = project
    assert main(["-c", str(config_path), "report"]) == 0
    assert "아직 볼 것이 없습니다" in capsys.readouterr().out


def test_report_compares_against_just_holding(project, capsys):
    """수익률만 보면 '올랐으니 잘했다'가 된다. 그냥 들고만 있어도 올랐을 수 있다."""
    from stockbot.execution.equity_log import EquityLog

    config_path, tmp_path = project
    book = EquityLog(tmp_path / "equity.csv")
    book.append(dt.date(2026, 1, 2), 10_000_000, 0)
    book.append(dt.date(2026, 2, 2), 9_000_000, 0)
    book.append(dt.date(2026, 3, 2), 11_000_000, 0)

    assert main(["-c", str(config_path), "report"]) == 0
    out = capsys.readouterr().out
    assert "+10.00%" in out, "수익률 10%가 나와야 합니다"
    assert "-10.00%" in out, "최대낙폭 -10%가 나와야 합니다"
    assert "운입니다" in out, "짧은 기간임을 분명히 말해야 합니다"


def test_an_unpriced_holding_is_not_recorded_as_a_loss(project, capsys, monkeypatch):
    """시세를 0으로 때우면 가짜 낙폭이 되고, 낙폭 중단 장치가 엉뚱하게 발동한다."""
    import pandas as pd

    config_path, tmp_path = project
    assert main(["-c", str(config_path), "trade", "--execute"]) == 0
    capsys.readouterr()

    real = load_prices

    def missing_prices(config, **kw):
        prices = real(config, **kw)
        # 한 종목 시세가 통째로 없다. 마지막 한 줄만 비우면 ffill 이 메워서
        # (그게 옳은 동작이라) 이 경로를 타지 않는다.
        prices.close[prices.close.columns[0]] = pd.NA
        return prices

    monkeypatch.setattr("stockbot.cli.load_prices", missing_prices)
    assert main(["-c", str(config_path), "snapshot"]) == 1
    assert "남기지 않았습니다" in capsys.readouterr().err
    assert not (tmp_path / "equity.csv").exists()


def test_the_guard_actually_receives_the_recorded_peak(project):
    """guards.py 가 맞아도 pipeline 이 고점을 안 넘기면 소용없다.

    실제로 그 상태였다. 설정과 README에 max_drawdown_stop 이 있는데 고점을
    넘겨주는 곳이 없어, 조건문이 늘 건너뛰어졌다. 있다고 믿게 만드는 장치는
    없느니만 못하다. 두 모듈을 따로 보면 둘 다 멀쩡해 보이므로 **연결**을 본다.
    """
    from stockbot import pipeline
    from stockbot.execution.equity_log import EquityLog
    from stockbot.execution.guards import GuardReport

    config_path, _ = project
    config = load_config(config_path)
    EquityLog(pipeline.equity_log_path(config)).append(dt.date(2026, 10, 1), 12_345_678, 0)

    seen = {}

    class Spy:
        def check(self, orders, account, prices, peak_equity=None):
            seen["peak"] = peak_equity
            return GuardReport(approved=list(orders), violations=[])

    config.guard = Spy()
    prices = load_prices(config)
    plan_rebalance(config, build_broker(config, prices), prices)

    assert seen["peak"] == 12_345_678, "고점이 안전장치까지 전달되지 않았습니다"


def test_a_dry_run_is_never_gated_by_market_hours(project, capsys, monkeypatch):
    """모의 출력은 장 시간과 무관해야 한다.

    '일일 점검'은 계획이 제대로 서는지 보는 리허설이라 밤이든 휴일이든 돌 수
    있어야 한다. 장 확인이 모의 출력보다 앞에 오면 이 리허설이 통째로 막힌다.
    """
    def explode(*a, **kw):
        raise AssertionError("모의 출력인데 장 시간을 확인했습니다")

    monkeypatch.setattr("stockbot.cli._closed_reason", explode)
    config_path, _ = project
    assert main(["-c", str(config_path), "trade"]) == 0


def test_a_closed_market_stops_the_submission(project, capsys, monkeypatch):
    """닫힌 장에 넣은 주문은 거부될지 다음 장으로 넘어갈지 알 수 없다."""
    monkeypatch.setattr("stockbot.cli._closed_reason",
                        lambda config, broker: "오늘은 휴장일입니다. 주문하지 않았습니다.")
    config_path, tmp_path = project

    assert main(["-c", str(config_path), "trade", "--execute"]) == 4
    assert "휴장일" in capsys.readouterr().err
    assert not (tmp_path / "acct.json").exists(), "막혔는데 계좌 상태가 바뀌었습니다"


def test_a_blocked_day_is_not_counted_as_submitted(project, capsys, monkeypatch):
    """장 때문에 막힌 날이 '오늘 제출함'으로 기록되면, 정작 열렸을 때 막힌다."""
    blocked = {"on": True}
    monkeypatch.setattr(
        "stockbot.cli._closed_reason",
        lambda config, broker: "오늘은 휴장일입니다." if blocked["on"] else None)
    config_path, _ = project

    assert main(["-c", str(config_path), "trade", "--execute"]) == 4
    blocked["on"] = False
    assert main(["-c", str(config_path), "trade", "--execute"]) == 0


def test_anytime_overrides_the_market_check(project, capsys, monkeypatch):
    """사람이 판단해서 지금 내겠다고 하면 낼 수 있어야 한다."""
    monkeypatch.setattr("stockbot.cli._closed_reason",
                        lambda config, broker: "오늘은 휴장일입니다.")
    config_path, _ = project
    assert main(["-c", str(config_path), "trade", "--execute", "--anytime"]) == 0


def test_cli_compare_lists_strategies(project, capsys):
    config_path, _ = project
    assert main(["-c", str(config_path), "compare"]) == 0
    out = capsys.readouterr().out
    assert "균등비중" in out
    assert "리스크패리티" in out


def test_cli_reports_missing_config(tmp_path, capsys):
    assert main(["-c", str(tmp_path / "nope.yaml"), "signal"]) == 1
    assert "오류" in capsys.readouterr().err


def test_unknown_broker_fails_loudly(project):
    """모르는 브로커 이름이 조용히 모의로 떨어지면 안 된다."""
    config_path, _ = project
    config = load_config(config_path)
    config.execution.broker = "알수없는증권"
    with pytest.raises(NotImplementedError, match="구현되지 않았습니다"):
        build_broker(config, None)


def test_kis_broker_without_credentials_explains_setup(project, monkeypatch, tmp_path):
    """키가 없으면 무엇을 어디에 넣어야 하는지 알려주고 멈춰야 한다."""
    from stockbot.execution.kis import KISError

    for key in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # 저장소의 .env를 읽지 않도록

    config_path, _ = project
    config = load_config(config_path)
    config.execution.broker = "kis"
    with pytest.raises(KISError, match="KIS_APP_KEY"):
        build_broker(config, None)


def test_missing_data_directory_explains_where_it_looked(project, tmp_path, monkeypatch):
    """데이터를 못 찾으면 어느 경로를 봤는지 알려줘야 한다.

    "비어 있습니다"만 보여주면, 실제로는 실행 위치가 틀렸을 뿐인데도
    설정을 엉뚱하게 고치게 된다.
    """
    config_path, _ = project
    config = load_config(config_path)
    config.raw["data"]["path"] = str(tmp_path / "does_not_exist")

    with pytest.raises(ValueError) as exc:
        load_prices(config)

    message = str(exc.value)
    assert "does_not_exist" in message          # 찾아본 경로
    assert "현재 작업 디렉터리" in message          # 실행 위치
    assert "make_sample_data" in message        # 다음에 할 일


def test_wrong_csv_filenames_lists_what_was_found(project, tmp_path):
    """폴더는 있는데 파일명이 다르면, 실제 파일 목록과 기대 티커를 함께 보여준다."""
    config_path, _ = project
    config = load_config(config_path)

    empty_dir = tmp_path / "wrong_names"
    empty_dir.mkdir()
    (empty_dir / "삼성전자.csv").write_text("date,close\n2024-01-01,100\n", encoding="utf-8")
    config.raw["data"]["path"] = str(empty_dir)

    with pytest.raises(ValueError) as exc:
        load_prices(config)

    message = str(exc.value)
    assert "삼성전자.csv" in message
    assert "GROW" in message  # 설정이 기대하는 티커


@pytest.mark.parametrize(
    "argv_builder",
    [
        pytest.param(lambda c: ["signal", "-c", c], id="서브커맨드_뒤"),
        pytest.param(lambda c: ["-c", c, "signal"], id="서브커맨드_앞"),
        pytest.param(lambda c: ["signal", "--config", c], id="긴_옵션명_뒤"),
        pytest.param(lambda c: ["--config", c, "signal"], id="긴_옵션명_앞"),
    ],
)
def test_config_option_works_on_either_side_of_subcommand(project, capsys, argv_builder):
    """`-c`는 서브커맨드 앞뒤 어디에 와도 동작해야 한다.

    한쪽만 되면 반드시 기억이 틀린 쪽으로 손이 간다. `parents=`로 공유한
    Action에 기본값을 지정하면 서브커맨드가 부모 값을 덮어써서 조용히
    기본 경로를 보게 되므로, 그 회귀를 여기서 잡는다.
    """
    config_path, _ = project
    assert main(argv_builder(str(config_path))) == 0
    assert "목표 비중" in capsys.readouterr().out


def test_cache_option_also_works_on_either_side(project):
    from stockbot.cli import build_parser

    config_path, _ = project
    for argv in (
        ["backtest", "-c", str(config_path), "--cache", "X"],
        ["--cache", "X", "-c", str(config_path), "backtest"],
    ):
        assert build_parser().parse_args(argv).cache == "X"


def test_defaults_apply_when_no_options_given():
    """옵션을 하나도 주지 않으면 기본 경로를 쓴다."""
    from stockbot.cli import DEFAULT_CACHE, DEFAULT_CONFIG, build_parser

    args = build_parser().parse_args(["signal"])
    assert getattr(args, "config", DEFAULT_CONFIG) == DEFAULT_CONFIG
    assert getattr(args, "cache", DEFAULT_CACHE) == DEFAULT_CACHE


def test_cli_walkforward_reports_efficiency(project, capsys):
    """워크포워드 CLI가 효율과 구간별 선택을 출력해야 한다."""
    config_path, _ = project
    assert main(["walkforward", "-c", str(config_path), "--train", "400", "--test", "150"]) == 0

    out = capsys.readouterr().out
    assert "워크포워드 검증" in out
    assert "워크포워드 효율" in out
    assert "검증 구간만 이어 붙인 성과" in out


def test_cli_walkforward_compare_shows_overfitting_gap(project, capsys):
    config_path, _ = project
    assert main([
        "walkforward", "-c", str(config_path),
        "--train", "400", "--test", "150", "--compare-full",
    ]) == 0

    out = capsys.readouterr().out
    assert "전체 기간 백테스트" in out
    assert "과최적화" in out


def test_cli_walkforward_rejects_impossible_split(project, capsys):
    """데이터보다 긴 구간을 요구하면 무엇을 고쳐야 하는지 알려준다."""
    config_path, _ = project
    assert main(["walkforward", "-c", str(config_path), "--train", "99999", "--test", "10"]) == 1
    assert "구간을 만들 수 없습니다" in capsys.readouterr().err


def test_version_flag_lists_every_command(capsys):
    """`--version`이 실제 서브커맨드 목록과 어긋나면 안 된다.

    구버전이 설치된 상태에서 새 명령이 `invalid choice`로 거부될 때, 사용자가
    원인을 확인하는 유일한 수단이 이 출력이다. 목록이 거짓이면 쓸모가 없다.
    """
    from stockbot.cli import COMMANDS, build_parser

    registered = set()
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            registered = set(action.choices)
    assert registered == set(COMMANDS), (
        f"등록된 명령과 COMMANDS가 다릅니다: "
        f"등록={sorted(registered)} COMMANDS={sorted(COMMANDS)}"
    )

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0

    out = capsys.readouterr().out
    for name in COMMANDS:
        assert name in out, f"--version 출력에 {name}이 없습니다"
    assert "코드 위치" in out


def test_unknown_command_points_at_the_likely_cause(capsys):
    """`invalid choice`만 보여주면 원인을 찾을 수 없다.

    실제로 두 번 겪은 상황: 프로젝트 폴더가 아닌 곳에서 실행해 git pull이
    조용히 실패했고, 옛 코드가 돌아 새 명령이 거부됐다. 코드 위치와 작업
    디렉터리가 같이 찍히면 그 자리에서 판별된다.
    """
    with pytest.raises(SystemExit) as exc:
        main(["존재하지않는명령"])
    assert exc.value.code == 2

    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "코드 위치" in err, "어떤 코드가 도는지 없으면 구버전인지 알 수 없습니다"
    assert "현재 작업 디렉터리" in err, "폴더가 틀린 경우를 짚어야 합니다"
    assert "git pull" in err


# --- 선택이 값어치를 했는지 재는 비교 ---------------------------------------


def test_fixed_strategy_over_uses_the_requested_window_only(project):
    """같은 구간으로 잘라야 워크포워드와 나란히 읽을 수 있다."""
    from stockbot.pipeline import fixed_strategy_over
    from stockbot.portfolio.fixed import EqualWeight

    config_path, _ = project
    config = load_config(config_path)
    prices = load_prices(config)
    index = prices.close.index
    start, end = index[len(index) // 2], index[-1]

    out = fixed_strategy_over(config, prices, EqualWeight(), start, end)
    full = run_backtest(config, prices).summary(config.backtest.risk_free)

    assert out["cagr"] != full["cagr"], "구간을 잘랐는데 전체 기간과 같습니다"
    # 시작 자산을 맞춰야 수익률 비교가 성립한다.
    assert out["start_value"] == pytest.approx(config.backtest.initial_cash)


def test_fixed_strategy_over_restores_the_configured_strategy(project):
    """설정 객체를 빌려 쓰고 돌려놓지 않으면 뒤 명령이 엉뚱한 전략을 쓴다."""
    from stockbot.pipeline import fixed_strategy_over
    from stockbot.portfolio.fixed import EqualWeight

    config_path, _ = project
    config = load_config(config_path)
    prices = load_prices(config)
    before = config.strategy

    index = prices.close.index
    fixed_strategy_over(config, prices, EqualWeight(), index[len(index) // 2], index[-1])
    assert config.strategy is before

    with pytest.raises(ValueError):  # 구간이 너무 짧으면 실패하되
        fixed_strategy_over(config, prices, EqualWeight(), index[-1], index[-1])
    assert config.strategy is before, "실패 경로에서 전략이 복원되지 않았습니다"
