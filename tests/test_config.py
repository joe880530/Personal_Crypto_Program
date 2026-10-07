import pytest

from stockbot.config import from_dict, load_config
from stockbot.portfolio import DualMomentum, EqualWeight, FixedWeight, MomentumRiskParity, RiskParity

BASE = {
    "name": "t",
    "assets": [
        {"ticker": "SPY", "market": "US", "currency": "USD"},
        {"ticker": "069500", "market": "KR_ETF", "currency": "KRW"},
    ],
}


def test_builds_each_strategy_type():
    cases = {
        "equal": EqualWeight,
        "risk_parity": RiskParity,
        "dual_momentum": DualMomentum,
        "momentum_risk_parity": MomentumRiskParity,
    }
    for kind, expected in cases.items():
        cfg = from_dict({**BASE, "strategy": {"type": kind}})
        assert isinstance(cfg.strategy, expected)

    cfg = from_dict({**BASE, "strategy": {"type": "fixed", "params": {"weights": {"SPY": 1.0}}}})
    assert isinstance(cfg.strategy, FixedWeight)


def test_nested_combo_params_reach_sub_strategies():
    cfg = from_dict({
        **BASE,
        "strategy": {
            "type": "momentum_risk_parity",
            "params": {"momentum": {"top_n": 1, "safe_asset": "069500"}, "sizer": {"lookback": 60}},
        },
    })
    assert cfg.strategy.momentum.top_n == 1
    assert cfg.strategy.sizer.lookback == 60


def test_unknown_strategy_type_is_rejected():
    with pytest.raises(ValueError, match="알 수 없는 전략"):
        from_dict({**BASE, "strategy": {"type": "magic"}})


def test_empty_assets_rejected():
    with pytest.raises(ValueError, match="assets가 비어"):
        from_dict({"name": "t", "assets": []})


def test_duplicate_tickers_rejected():
    with pytest.raises(ValueError, match="중복된 티커"):
        from_dict({"name": "t", "assets": [{"ticker": "SPY"}, {"ticker": "SPY"}]})


def test_cost_book_uses_market_presets():
    cfg = from_dict(BASE)
    book = cfg.cost_book()
    assert book.for_ticker("069500").sell_tax_bps == 0.0   # 국내 ETF는 거래세 없음
    assert book.for_ticker("SPY").commission_bps > 0


def test_cost_overrides_replace_preset():
    cfg = from_dict({**BASE, "costs": {"overrides": {"US": "ZERO"}}})
    assert cfg.cost_book().for_ticker("SPY").commission_bps == 0.0


def test_guard_defaults_to_configured_tickers():
    cfg = from_dict(BASE)
    assert cfg.guard.allowed_tickers == {"SPY", "069500"}


def test_missing_config_file_gives_helpful_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="portfolio.example.yaml"):
        load_config(tmp_path / "nope.yaml")


def test_example_config_is_valid():
    """리포지터리에 들어 있는 예시 설정이 항상 파싱돼야 한다."""
    cfg = load_config("config/portfolio.example.yaml")
    assert len(cfg.assets) == 6
    assert cfg.base_currency == "KRW"
    # 데이터가 warmup보다 짧으면 전략이 돌지 않는다. 시작일과 견줘 확인한다.
    assert cfg.strategy.warmup() >= 1
    # 워크포워드로 다시 재볼 수 있도록 후보 목록은 남아 있어야 한다.
    assert len(cfg.candidate_strategies()) >= 2


def test_kr_paper_config_orders_pass_the_guard():
    """모의투자용 설정은 안전장치를 실제로 통과해야 한다.

    2종목 균등이면 한 종목 비중이 0.50이 된다. 기본값(max_position_weight 0.40)을
    그대로 두면 매수 주문이 전부 취소돼, 아무 오류 없이 "주문 0건"만 보게 된다.
    조용히 아무것도 안 하는 실패라 눈치채기 어려우므로 여기에 못을 박아 둔다.
    """
    import pandas as pd

    from stockbot.execution.order import Account
    from stockbot.execution.planner import plan_orders

    cfg = load_config("config/kr_paper.example.yaml")
    assert cfg.execution.broker == "kis"
    # KIS 브로커는 국내주식 주문만 구현돼 있다. 미국 티커가 섞이면 주문에서 깨진다.
    assert {a.market for a in cfg.assets} == {"KR_ETF"}

    # 예수금은 모의계좌마다 다르다(확인된 실제 계좌는 1천만원). 한도가 비율
    # 기준이므로 규모가 달라져도 그대로 통과해야 한다.
    prices = pd.Series({"069500": 30000.0, "114260": 110000.0})
    history = pd.DataFrame(
        {t: [float(v)] * 5 for t, v in prices.items()},
        index=pd.date_range("2026-01-05", periods=5),
    )
    target = cfg.strategy.target_weights(history)

    ex = cfg.execution
    for cash in (5_000_000.0, 10_000_000.0, 100_000_000.0):
        account = Account(cash=cash, currency="KRW", positions={})
        orders = plan_orders(
            target_weights=target,
            positions=account.positions,
            prices=prices,
            cash=account.cash,
            band=ex.band,
            lot_sizes=cfg.lot_sizes,
            allow_fractional=ex.allow_fractional,
            min_trade_value=ex.min_trade_value,
            cash_buffer=ex.cash_buffer,
            order_type=ex.as_order_type(),
            limit_slippage=ex.limit_slippage,
        )
        report = cfg.guard.check(orders, account, dict(prices))
        assert len(orders) == 2, f"예수금 {cash:,.0f}: 주문이 2건 나와야 합니다"
        assert report.ok, f"예수금 {cash:,.0f}: {report.describe()}"
        assert len(report.approved) == 2


# ------------------------------------------------------------------ 안내 메시지
def test_copy_command_uses_windows_syntax():
    """cmd에는 `cp`가 없고 경로 구분자도 역슬래시다."""
    import pathlib

    from stockbot.config import copy_command

    command = copy_command(
        pathlib.Path("config/portfolio.example.yaml"),
        pathlib.Path("config/portfolio.yaml"),
        windows=True,
    )
    assert command == r"copy config\portfolio.example.yaml config\portfolio.yaml"
    assert not command.startswith("cp ")


def test_copy_command_uses_posix_syntax():
    import pathlib

    from stockbot.config import copy_command

    command = copy_command(
        pathlib.Path("config/portfolio.example.yaml"),
        pathlib.Path("config/portfolio.yaml"),
        windows=False,
    )
    assert command == "cp config/portfolio.example.yaml config/portfolio.yaml"


def test_missing_config_suggests_runnable_copy_command(tmp_path):
    """예시 파일이 옆에 있으면, 그대로 붙여넣을 수 있는 명령을 알려준다."""
    (tmp_path / "portfolio.example.yaml").write_text("name: x", encoding="utf-8")

    with pytest.raises(FileNotFoundError) as exc:
        load_config(tmp_path / "portfolio.yaml")

    message = str(exc.value)
    assert "portfolio.example.yaml" in message
    assert "portfolio.yaml" in message
    assert ("copy " in message) or ("cp " in message)


def test_missing_example_reports_working_directory(tmp_path):
    """예시 파일조차 없으면 실행 위치가 틀린 것이므로 그걸 알려준다."""
    with pytest.raises(FileNotFoundError) as exc:
        load_config(tmp_path / "nowhere" / "portfolio.yaml")

    message = str(exc.value)
    assert "현재 작업 디렉터리" in message
    assert "프로젝트 루트" in message


# ------------------------------------------------------------------ 워크포워드 설정
def test_walkforward_defaults_are_three_years_train_one_year_test():
    cfg = from_dict(BASE)
    assert cfg.walkforward.train == 756
    assert cfg.walkforward.test == 252
    assert cfg.walkforward.mode == "rolling"


def test_walkforward_candidates_are_built():
    cfg = from_dict({
        **BASE,
        "walkforward": {
            "train": 500,
            "candidates": [
                {"name": "균등", "type": "equal"},
                {"name": "리스크패리티", "type": "risk_parity", "params": {"lookback": 60}},
            ],
        },
    })
    candidates = cfg.candidate_strategies()
    assert list(candidates) == ["균등", "리스크패리티"]
    assert candidates["리스크패리티"].lookback == 60


def test_without_candidates_only_the_configured_strategy_is_evaluated():
    cfg = from_dict({**BASE, "strategy": {"type": "risk_parity"}})
    assert list(cfg.candidate_strategies()) == ["risk_parity"]


def test_duplicate_candidate_names_rejected():
    with pytest.raises(ValueError, match="중복된 이름"):
        from_dict({
            **BASE,
            "walkforward": {"candidates": [
                {"name": "같음", "type": "equal"},
                {"name": "같음", "type": "risk_parity"},
            ]},
        })


def test_example_config_walkforward_section_is_valid():
    cfg = load_config("config/portfolio.example.yaml")
    assert len(cfg.candidate_strategies()) == 4


# --- 전략 type만 바꾸고 params를 남겼을 때 -----------------------------------
# 실제로 겪은 상황: strategy.type을 equal로 바꿨는데 momentum_risk_parity의
# params(momentum, sizer)가 남아 있었다. 그대로 EqualWeight(**params)로
# 넘어가 TypeError 트레이스백이 났고, 설정 파일 문제라는 걸 알기 어려웠다.


def test_leftover_params_from_a_previous_strategy_are_explained():
    from stockbot.config import _build_strategy

    with pytest.raises(ValueError) as exc:
        _build_strategy({
            "type": "equal",
            "params": {"momentum": {"top_n": 3}, "sizer": {"lookback": 120}},
        })

    message = str(exc.value)
    assert "momentum" in message and "sizer" in message, "무엇이 문제인지 짚어야 합니다"
    assert "strategy.params" in message, "어느 설정 줄인지 알려줘야 합니다"
    assert "받는 항목" in message, "무엇을 쓸 수 있는지 알려줘야 합니다"
    assert "TypeError" not in message


def test_nested_block_typos_name_the_right_block():
    """하위 블록 오타는 그 블록을 지목해야 한다."""
    from stockbot.config import _build_strategy

    with pytest.raises(ValueError, match=r"strategy\.params\.sizer"):
        _build_strategy({
            "type": "momentum_risk_parity",
            "params": {"sizer": {"lookbak": 120}},  # lookback 오타
        })

    with pytest.raises(ValueError, match=r"strategy\.params\.momentum"):
        _build_strategy({
            "type": "momentum_risk_parity",
            "params": {"momentum": {"top_nn": 3}},
        })


def test_config_errors_are_valueerror_so_the_cli_can_show_them():
    """CLI는 ValueError를 잡아 안내로 바꾼다. TypeError면 트레이스백이 뜬다."""
    from stockbot.config import _build_strategy

    for spec in [
        {"type": "equal", "params": {"lookback": 120}},
        {"type": "risk_parity", "params": {"top_n": 3}},
        {"type": "dual_momentum", "params": {"method": "erc"}},  # 리스크패리티 항목
    ]:
        with pytest.raises(ValueError):
            _build_strategy(spec)


def test_valid_strategies_still_build():
    """오류 처리를 넣다가 정상 경로를 막으면 안 된다."""
    from stockbot.config import _build_strategy

    assert _build_strategy({"type": "equal"}).name == "equal_weight"
    assert _build_strategy({"type": "risk_parity", "params": {"lookback": 60}}).lookback == 60
    combo = _build_strategy({
        "type": "momentum_risk_parity",
        "params": {"momentum": {"top_n": 2}, "sizer": {"lookback": 90, "max_weight": 0.5}},
    })
    assert combo.name == "momentum_risk_parity"
