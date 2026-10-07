"""명령줄 인터페이스.

    stockbot backtest -c config/portfolio.yaml
    stockbot compare  -c config/portfolio.yaml
    stockbot walkforward -c config/portfolio.yaml --compare-full
    stockbot signal   -c config/portfolio.yaml
    stockbot trade    -c config/portfolio.yaml --execute
    stockbot account  -c config/portfolio.yaml
"""

from __future__ import annotations

import argparse
import sys
import datetime as _dt
import json
import pathlib

from . import pipeline, reporting
from .config import AppConfig, load_config
from .backtest.schedule import normalize_freq
from .execution.broker import BrokerError
from .pipeline import (
    build_broker,
    current_target,
    load_prices,
    plan_rebalance,
    run_backtest,
)
from .portfolio.fixed import EqualWeight
from .portfolio.momentum import DualMomentum, MomentumRiskParity
from .portfolio.risk_parity import RiskParity
from .validation.walkforward import OBJECTIVES, walk_forward


#: 제공하는 서브커맨드. --version 출력과 서브파서 등록이 어긋나지 않도록
#: 한 곳에서 관리하고, 테스트가 양쪽이 일치하는지 확인한다.
COMMANDS = (
    "backtest", "compare", "walkforward", "breakout", "signal",
    "checkenv", "minutes", "collect", "account", "snapshot", "report", "trade",
)


def _names(config: AppConfig) -> dict[str, str]:
    return {a.ticker: a.label for a in config.assets}


def cmd_backtest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    prices = load_prices(config, cache_dir=args.cache)
    print(f"데이터: {prices.close.shape[1]}종목 × {len(prices.close)}봉 "
          f"({prices.close.index[0]:%Y-%m-%d} ~ {prices.close.index[-1]:%Y-%m-%d})\n")

    result = run_backtest(config, prices)
    print(reporting.format_summary(result.summary(config.backtest.risk_free),
                                   f"{config.name} — {config.strategy.name}"))
    print()
    print(reporting.format_weights(result.weights.iloc[-1], _names(config), "최종 보유 비중"))

    if args.out:
        path = reporting.save_results(result, args.out, config.name)
        print(f"\n결과 저장: {path}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """설정의 전략을 기본 벤치마크들과 나란히 비교한다."""
    config = load_config(args.config)
    prices = load_prices(config, cache_dir=args.cache)

    candidates = {
        f"설정 전략 ({config.strategy.name})": config.strategy,
        "균등비중": EqualWeight(),
        "리스크패리티": RiskParity(lookback=120),
        "듀얼모멘텀": DualMomentum(top_n=max(1, len(config.assets) // 2)),
        "모멘텀+리스크패리티": MomentumRiskParity(
            DualMomentum(top_n=max(1, len(config.assets) // 2)), RiskParity(lookback=120)
        ),
    }

    rows = {}
    for label, strategy in candidates.items():
        original = config.strategy
        config.strategy = strategy
        try:
            rows[label] = run_backtest(config, prices).summary(config.backtest.risk_free)
        except Exception as exc:  # 한 전략이 실패해도 나머지 비교는 계속한다
            print(f"[건너뜀] {label}: {exc}", file=sys.stderr)
        finally:
            config.strategy = original

    print(reporting.format_comparison(rows))
    print("\n주의: 같은 데이터로 여러 전략을 비교하면 우연히 좋아 보이는 것이 하나는 나옵니다.")
    print("      기간을 나눠 검증(워크포워드)하기 전에는 1등을 믿지 마세요.")
    return 0


def cmd_walkforward(args: argparse.Namespace) -> int:
    """학습 구간에서 고른 전략을 그 다음 구간에서만 평가한다."""
    config = load_config(args.config)
    prices = load_prices(config, cache_dir=args.cache)
    wf = config.walkforward

    candidates = config.candidate_strategies()
    print(f"데이터: {prices.close.shape[1]}종목 × {len(prices.close)}봉 "
          f"({prices.close.index[0]:%Y-%m-%d} ~ {prices.close.index[-1]:%Y-%m-%d})")
    print(f"후보 {len(candidates)}개: {', '.join(candidates)}\n")

    bt = config.backtest
    result = walk_forward(
        prices=prices.close,
        candidates=candidates,
        train=args.train or wf.train,
        test=args.test or wf.test,
        step=wf.step,
        mode=args.mode or wf.mode,
        objective=args.objective or wf.objective,
        costs=config.cost_book(),
        open_prices=prices.open,
        initial_cash=bt.initial_cash,
        risk_free=bt.risk_free,
        rebalance=bt.rebalance,
        band=bt.band,
        execution=bt.execution,
        allow_fractional=bt.allow_fractional,
        cash_rate=bt.cash_rate,
        min_trade_value=bt.min_trade_value,
    )

    print(reporting.format_walkforward(result, prices.close.index, bt.risk_free))

    if result.n_candidates > 1:
        # 핵심 질문: 구간마다 전략을 바꾼 수고가 값어치를 했나?
        # 같은 검증 기간에 각 후보를 고정해서 들고 있었을 때와 나란히 놓는다.
        oos_start, oos_end = result.equity.index[0], result.equity.index[-1]
        rows = {"워크포워드 (구간마다 선택)": result.summary(bt.risk_free)}
        for label, strategy in candidates.items():
            try:
                rows[f"고정: {label}"] = pipeline.fixed_strategy_over(
                    config, prices, strategy, oos_start, oos_end
                )
            except Exception as exc:  # 하나 실패해도 나머지는 보여준다
                print(f"[건너뜀] 고정 {label}: {exc}", file=sys.stderr)
        print()
        print(reporting.format_comparison(rows))
        print(f"\n같은 기간({oos_start:%Y-%m-%d} ~ {oos_end:%Y-%m-%d}) 비교입니다.")
        print("고정 전략 중 하나가 워크포워드보다 좋다면, 전략을 골라가며 바꾼 수고가")
        print("값어치를 하지 못했다는 뜻입니다. 그럼 그 하나를 고정해서 쓰는 게 낫습니다.")

    if args.compare_full:
        full = run_backtest(config, prices).summary(bt.risk_free)
        print()
        print(reporting.format_comparison({
            "전체 기간 백테스트": full,
            "워크포워드 검증": result.summary(bt.risk_free),
        }))
        print()
        if result.n_candidates <= 1:
            # 후보가 하나면 두 행이 '같은 고정 전략을 겹치는 기간에 돌린 것'이라
            # 거의 같게 나온다. 그 일치를 과최적화가 없다는 증거로 읽으면 안 된다.
            print("두 행의 차이는 과최적화가 아니라 **기간 차이**입니다. 후보가 1개라")
            print("고정된 전략을 겹치는 기간에 두 번 돌린 셈이어서, 비슷하게 나오는")
            print("것이 당연합니다. 과최적화를 재려면 후보를 여러 개 넣으세요.")
        else:
            print("전체 기간 백테스트가 더 좋다면, 그 차이가 과최적화의 크기입니다.")
        print("\n워크포워드 행의 회전율·거래비용은 '-'로 나옵니다. 구간별로 따로 계산해")
        print("합산하지 않기 때문이며, 수익률에는 이미 비용이 반영돼 있습니다.")
    return 0


def cmd_signal(args: argparse.Namespace) -> int:
    """지금 시점의 목표 비중만 보여준다. 주문은 만들지 않는다."""
    config = load_config(args.config)
    prices = load_prices(config, cache_dir=args.cache)
    target = current_target(config, prices)
    asof = prices.close.index[-1]
    print(f"기준일: {asof:%Y-%m-%d} / 전략: {config.strategy.name}\n")
    print(reporting.format_weights(target, _names(config)))
    return 0


#: 진단 줄 앞에 붙이는 표시. 터미널에서 한눈에 훑을 수 있게.
_MARKS = {"ok": "  OK ", "warn": "  ！ ", "bad": "  X  "}


def cmd_breakout(args: argparse.Namespace) -> int:
    """변동성 돌파를 워크포워드로 재고 미리 정한 기준에 대고 판정한다.

    합격선은 코드에 박혀 있다(intraday/evaluate.py의 Criteria). 결과를 보고
    기준을 고치면 판정이 아니라 변명이 된다.
    """
    from .backtest.costs import PRESETS
    from .data.ohlc import load_daily_ohlc
    from .intraday.breakout import BreakoutParams
    from .intraday.evaluate import judge, walk_forward_breakout

    config = load_config(args.config)
    ticker = args.ticker or _first_kr_ticker(config)
    market = next((a.market for a in config.assets if a.ticker == ticker), "KR_ETF")
    costs = config.cost_book().for_ticker(ticker) if not args.no_costs else PRESETS["ZERO"]

    print(f"종목: {ticker} · 시장: {market} · 기간: {args.start} ~ {args.end or '오늘'}")
    ohlc = load_daily_ohlc(ticker, args.start, args.end, cache_dir=args.cache)
    print(f"일봉 {len(ohlc)}개 ({ohlc.index[0]:%Y-%m-%d} ~ {ohlc.index[-1]:%Y-%m-%d})")
    print(f"비용: 편도 {costs.commission_bps + costs.slippage_bps:.1f}bp"
          f" (수수료 {costs.commission_bps} + 슬리피지 {costs.slippage_bps})")
    if args.no_costs:
        print("  ⚠ --no-costs: 비용을 0으로 뒀습니다. 판정에 쓰지 마세요.")

    candidates = [BreakoutParams(k=k, range_lookback=args.lookback) for k in args.k]
    print(f"후보 K: {args.k} · 학습 {args.train}일 / 검증 {args.test}일 ({args.mode})\n")

    result = walk_forward_breakout(
        ohlc, candidates, costs,
        train=args.train, test=args.test, mode=args.mode,
        initial_cash=config.backtest.initial_cash, cash_rate=config.backtest.cash_rate,
    )

    print("구간별 (앞에서 고르고 뒤에서 평가)")
    print(f"  {'검증구간':<25}{'고른 K':>8}{'학습샤프':>10}{'검증샤프':>10}{'진입':>7}")
    for w in result.windows:
        span = f"{w.test_span[0]:%y-%m-%d} ~ {w.test_span[1]:%y-%m-%d}"
        print(f"  {span:<25}{w.selected.k:>8.2f}{w.train_sharpe:>10.2f}"
              f"{w.test_sharpe:>10.2f}{w.test_trades:>7}")

    churn = result.selection_churn()
    print(f"\n선택 뒤집힘: {'해당 없음 (후보 1개)' if churn != churn else f'{churn:.2f}'}")

    # 판정 기준은 누적수익 하나지만, 그것만 보면 '덜 벌고 덜 잃는' 전략을
    # 어떻게 봐야 할지 알 수 없다. 기준은 그대로 두고 근거만 더 보여준다.
    mine, held = result.summary(), result.hold_summary()
    print("\n전략 vs 그냥 보유 (같은 기간)")
    print(f"  {'':<14}{'전략':>12}{'보유':>12}")
    for label, key, fmt in [
        ("누적수익", "total_return", "{:+.1%}"),
        ("연수익(CAGR)", "cagr", "{:+.1%}"),
        ("변동성", "volatility", "{:.1%}"),
        ("샤프", "sharpe", "{:.2f}"),
        ("최대낙폭", "max_drawdown", "{:.1%}"),
    ]:
        print(f"  {label:<14}{fmt.format(mine[key]):>12}{fmt.format(held[key]):>12}")

    print("\n" + "=" * 62)
    print("판정 (기준은 돌리기 전에 정해 둔 것입니다)")
    print("=" * 62)
    checks = judge(result)
    for check in checks:
        mark = "합격" if check.passed else "불합격"
        print(f"  [{mark}] {check.name:<22}{check.value:>10.4g}  기준 {check.threshold}"
              + (f"  — {check.note}" if check.note else ""))

    failed = [c for c in checks if not c.passed]
    print()
    if failed:
        print(f"불합격 {len(failed)}건: {', '.join(c.name for c in failed)}")
        print("  이 전략은 모의계좌에 올리지 않습니다.")
        print("  백테스트에서 떨어진 것을 '실전은 다를 수도'라며 밀어붙이는 것이")
        print("  이 바닥에서 돈을 잃는 가장 흔한 경로입니다.")
        return 1

    print("다섯 기준을 모두 통과했습니다.")
    print("  다만 이 백테스트는 일봉 근사라 실제보다 좋게 나옵니다")
    print("  (기준가에 정확히 체결됐다고 가정하고, 고가가 언제 나왔는지 무시합니다).")
    print("  다음: 1년치 분봉으로 다시 재서 얼마나 부풀려졌는지 확인합니다.")
    return 0


def _first_kr_ticker(config) -> str:
    """설정에서 국내 종목 하나를 고른다. 돌파 전략은 단일 종목이다."""
    for asset in config.assets:
        if asset.market.startswith("KR"):
            return asset.ticker
    raise ValueError(
        "설정에 국내 종목이 없습니다. --ticker로 직접 지정하세요 (예: --ticker 069500)."
    )


def cmd_checkenv(args: argparse.Namespace) -> int:
    """주문을 내기 전에 접속 정보가 제대로 들어갔는지만 확인한다.

    실패를 두 단계로 나눠 본다. 먼저 파일과 값(네트워크 없이), 그다음 인증.
    한꺼번에 시도하면 "인증 실패" 한 줄만 보고 오타인지, 키가 틀린 건지,
    장이 닫힌 건지 구분할 수 없다.
    """
    import pathlib

    from .execution.kis import KISError, diagnose_env, load_dotenv

    print(f"작업 디렉터리: {pathlib.Path.cwd()}\n")

    print("[1/3] .env 파일과 값")
    lines, bad = diagnose_env(load_dotenv())
    for level, text in lines:
        print(f"{_MARKS[level]}{text}")
    if bad:
        print("\n  -> 위 X 항목부터 고치세요. 여기서 멈춥니다.")
        return 1

    print(f"\n[2/3] 설정 파일 ({args.config})")
    config = load_config(args.config)
    broker_kind = config.execution.broker
    print(f"{_MARKS['ok']}execution.broker: {broker_kind}")
    markets = sorted({a.market for a in config.assets})
    print(f"{_MARKS['ok']}종목 {len(config.assets)}개 · 시장 {markets}")
    if broker_kind != "kis":
        print(f"{_MARKS['bad']}브로커가 'kis'가 아닙니다. 설정을 바꾸지 않으면 KIS로 주문이 나가지 않습니다.")
        print("       copy config\\kr_paper.example.yaml config\\portfolio.yaml")
        return 1
    # KIS 브로커는 국내주식 엔드포인트만 쓴다. 해외 종목이 섞이면 주문에서 깨진다.
    foreign = sorted({a.market for a in config.assets if not a.market.startswith("KR")})
    if foreign:
        print(f"{_MARKS['bad']}KIS 브로커는 국내주식만 주문할 수 있는데 {foreign} 시장 종목이 있습니다.")
        return 1

    print("\n[3/3] KIS 서버 인증")
    if args.offline:
        print(f"{_MARKS['warn']}--offline 이라 건너뜁니다.")
        return 0
    broker = build_broker(config)
    try:
        print(f"{_MARKS['ok']}{broker.ping()}")
    except KISError as exc:
        print(f"{_MARKS['bad']}{exc}")
        return 1
    if broker.is_live:
        print(f"{_MARKS['warn']}실계좌입니다. 모의투자로 먼저 검증하려면 .env의 KIS_PAPER=true 로 두세요.")
    # 여기까지는 '앱키가 맞다'까지만 증명한다. 토큰 발급은 계좌번호를 쓰지 않으므로
    # 계좌번호·상품코드가 틀려도 통과한다. 다 됐다고 말하면 다음 실패에서 헤맨다.
    print("\n앱키 인증까지 확인했습니다. 계좌번호와 상품코드는 아직 확인되지 않았습니다")
    print("  (토큰 발급은 계좌번호를 쓰지 않습니다). 잔고 조회로 이어서 확인하세요:")
    # 실행 경로를 단정하지 않는다. 컨테이너 안에서는 'stockbot',
    # NAS에서는 'bash scripts/nas_run.sh'로 부르는데 여기서는 알 수 없다.
    print(f"  이 명령을 부른 것과 같은 방식으로:  account -c {args.config}")
    print("  거기서 계좌 관련 오류가 나면 .env의 KIS_ACCOUNT / KIS_PRODUCT_CODE를 먼저 의심하세요.")
    return 0


def cmd_minutes(args: argparse.Namespace) -> int:
    """분봉이 이 계좌에서 실제로 나오는지 확인한다.

    일중 전략은 분봉 없이는 검증도 운용도 안 된다. 그런데 과거 분봉 API 문서에는
    "실전계좌의 경우"라고만 적혀 있어 모의계좌 지원 여부가 불분명하다. 백테스트를
    다 만든 뒤에 데이터가 없다는 걸 알면 헛수고가 되므로 여기서 먼저 확인한다.
    """
    import datetime as dt
    import pathlib

    from .data.calendar import TradingCalendar
    from .data.kis_quotes import KISQuotes

    config = load_config(args.config)
    if config.execution.broker.lower() != "kis":
        print(f"오류: 분봉 조회는 KIS 브로커에서만 됩니다 (지금: {config.execution.broker})",
              file=sys.stderr)
        return 1

    broker = build_broker(config)
    quotes = KISQuotes(broker)
    calendar = TradingCalendar(quotes, cache_path=str(
        pathlib.Path(config.execution.state_path).parent / "trading_days.json"))

    now = dt.datetime.now()
    day = dt.date.fromisoformat(args.day) if args.day else calendar.previous_trading_day(now.date())

    # 확인하려는 것은 분봉이다. 달력은 곁다리이므로 먼저 하지 않는다 —
    # 모의계좌는 휴장일 TR을 지원하지 않아서, 여기에 기대면 정작 알고 싶은
    # 답을 못 듣고 끝난다(실제로 그렇게 막혔다).
    print(f"지금: {now:%Y-%m-%d %H:%M} · 조회 기준일: {day}\n")
    print("[1/3] 과거 분봉 (일중 백테스트의 전제)")

    ok_all = True
    for asset in config.assets:
        result = quotes.probe_history(asset.ticker, day)
        name = f"{asset.ticker} {asset.name or ''}".strip()
        if result["ok"]:
            print(f"  OK  {name}: {result['bars']}건"
                  f"  {result['first']:%H:%M} ~ {result['last']:%H:%M}")
        else:
            ok_all = False
            print(f"  X   {name}: {result['reason']}")

    print("\n[2/3] 보관된 분봉 (그날 치가 온전한가)")
    _report_stored_day(config, args.store, day)

    print("\n[3/3] 휴장일 조회 (없어도 됨)")
    calendar.is_trading_day(now.date())      # 한 번 물어봐야 지원 여부를 알 수 있다
    if calendar.knows_holidays():
        print(f"  OK  공휴일까지 반영됩니다 · 오늘 장 국면: {calendar.phase(now)}")
    else:
        reason = calendar.holiday_error or "조회 수단 없음"
        print(f"  !   쓸 수 없어 주말만 거릅니다: {reason}")
        print("      공휴일에도 깨어나지만, 그날은 시세가 없어 주문 없이 끝납니다.")

    print()
    if ok_all:
        print("과거 분봉이 이 계좌에서 나옵니다. 일중 백테스트를 만들 수 있습니다.")
        return 0
    print("과거 분봉을 받지 못했습니다. 모의계좌가 이 API를 지원하지 않을 수 있습니다.")
    print("  백테스트용 데이터를 다른 곳에서 구해야 하니 위 메시지를 그대로 알려주세요.")
    return 1


#: 분봉이 나올 수 있는 최대 개수. **정규장 전체(09:00~15:30, 391분)가 아니다.**
#: 15:20~15:30은 장마감 동시호가라 연속 체결이 없고 그래서 분봉도 없다. 연속매매는
#: 09:00~15:20이고 마지막 봉은 15:19에 찍힌다 — 09:00부터 세면 380개다.
#:
#: 391을 기준으로 두면 완벽한 날에도 "11분 없음"이 매일 나온다. 늘 뜨는 경고는
#: 경고가 아니라 소음이고, 진짜 빠진 날을 가린다. 실제로 069500이 380건을
#: 받은 날 11분이 비었다고 찍혔다.
SESSION_MINUTES = 380


def _report_stored_day(config, store_path: str, day) -> None:
    """보관소에 그날 분봉이 어떻게 들어와 있는지."""
    from .data.minute_store import MinuteStore

    store = MinuteStore(store_path)
    for asset in config.assets:
        name = f"{asset.ticker} {asset.name or ''}".strip()
        frame = store.read(asset.ticker)
        same_day = frame[frame.index.date == day] if not frame.empty else frame
        if same_day.empty:
            mark = "-" if day in store.stored_days(asset.ticker) else "X"
            note = "빈 날로 기록됨(휴장)" if mark == "-" else "아직 받지 않았습니다"
            print(f"  {mark}   {name}: {note}")
            continue
        first, last = same_day.index.min(), same_day.index.max()
        # 빠진 분이 곧 결함은 아니다. 체결이 없던 분은 봉도 없는 것이 정상이라,
        # 거래가 뜸한 종목일수록 많이 빈다(국고채 ETF는 하루 70~90분이 빈다).
        quiet = SESSION_MINUTES - len(same_day)
        print(f"  OK  {name}: {len(same_day)}건  {first:%H:%M} ~ {last:%H:%M}"
              f"  (연속매매 {SESSION_MINUTES}분 중 체결 없는 분 {quiet}분)")
        if first.time() > _dt.time(9, 5):
            print(f"      ! 첫 봉이 {first:%H:%M}입니다. 아침이 빠졌을 수 있습니다.")


def cmd_collect(args: argparse.Namespace) -> int:
    """분봉을 모아 둔다. 매일 한 번 돌리는 용도.

    KIS는 분봉을 1년만 보관한다. 지금부터 쌓아야 1년 뒤에 2년치가 되고, 그때야
    일중 전략을 제대로 판별할 구간 수가 나온다. 지금 1년치로 재면 워크포워드
    구간이 2~3개뿐이라 과최적화를 가려낼 수 없다.
    """
    import datetime as dt
    import pathlib

    from .data.calendar import TradingCalendar
    from .data.kis_quotes import KISQuotes
    from .data.minute_store import MinuteStore

    config = load_config(args.config)
    if config.execution.broker.lower() != "kis":
        print(f"오류: 분봉 수집은 KIS 브로커에서만 됩니다 (지금: {config.execution.broker})",
              file=sys.stderr)
        return 1

    quotes = KISQuotes(build_broker(config))
    calendar = TradingCalendar(quotes, cache_path=str(
        pathlib.Path(config.execution.state_path).parent / "trading_days.json"))
    store = MinuteStore(args.store)

    today = dt.date.today()
    end = dt.date.fromisoformat(args.end) if args.end else today
    start = (dt.date.fromisoformat(args.start) if args.start
             else end - dt.timedelta(days=args.days))
    # 당일 분봉은 장이 끝나야 온전하다. 장중에 받으면 반쪽짜리가 저장된다.
    if end >= today and calendar.phase(dt.datetime.now()) in {"before_open", "open"}:
        end = today - dt.timedelta(days=1)
        print(f"장중이라 오늘({today})은 건너뜁니다. 마감 후에 다시 도세요.")

    days = [d for d in calendar.trading_days(start, end)]
    if not days:
        print(f"{start} ~ {end} 사이에 거래일이 없습니다.")
        return 0

    print(f"대상: {start} ~ {end} · 거래일 {len(days)}일 · 종목 {len(config.assets)}개")
    total_new = total_bars = 0
    attempted = 0
    failed: list[tuple[str, object]] = []
    for asset in config.assets:
        if not asset.market.startswith("KR"):
            print(f"  건너뜀 {asset.ticker}: 국내 종목만 받습니다 ({asset.market})")
            continue
        have = store.stored_days(asset.ticker)
        todo = [d for d in days if d not in have]
        print(f"  {asset.ticker}: 보유 {len(have)}일 · 받을 날 {len(todo)}일")
        attempted += len(todo)
        for day in todo:
            try:
                bars = quotes.day_minutes(asset.ticker, day)
            except Exception as exc:                 # 하루 실패가 전체를 멈추면 안 된다
                print(f"    {day} 실패: {exc}")
                failed.append((asset.ticker, day))
                continue
            count = store.save(asset.ticker, day, bars)
            total_new += 1
            total_bars += count
            if args.verbose:
                print(f"    {day}: {count}건")
        span = store.span(asset.ticker)
        if span:
            print(f"    -> 보관 구간 {span[0]} ~ {span[1]}")

    print(f"\n새로 받은 날 {total_new}일 · 분봉 {total_bars:,}건")
    print(f"저장 위치: {pathlib.Path(args.store).resolve()}")

    # 실패한 날은 '빈 날'로 기록하지 않으므로 다음 실행에서 다시 받는다.
    # 그래도 몇 건인지는 말해줘야 한다. 260줄이 흘러간 뒤 끝 줄만 보고
    # "다 받았구나" 하면, 빠진 날이 있다는 걸 1년 뒤에나 알게 된다.
    if failed:
        print(f"못 받은 날 {len(failed)}일. 다음 실행에서 다시 받습니다.")
        for ticker, day in failed[:10]:
            print(f"  {ticker} {day}")
        if len(failed) > 10:
            print(f"  ... 외 {len(failed) - 10}일")
    # 한 건도 못 받았으면 조용히 성공으로 끝내면 안 된다. 매일 도는 작업에서는
    # 이게 '오늘 치를 통째로 놓쳤다'는 뜻이고, 스케줄러가 알려줘야 한다.
    if attempted and not total_new:
        print("한 건도 받지 못했습니다. 접속 정보나 KIS 서버 상태를 확인하세요.",
              file=sys.stderr)
        return 1
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """오늘의 평가액을 기록에 남긴다. 매일 마감 후 한 번 돌리는 용도.

    지나간 날은 되돌려 받을 수 없다. 몇 달 뒤 '실계좌로 넘어갈까'를 판단할
    근거가 이 기록뿐이고, 그때 가서 만들 수는 없다.
    """
    from .execution.equity_log import EquityLog

    config = load_config(args.config)
    prices = load_prices(config, cache_dir=args.cache)
    broker = build_broker(config, prices)

    account = broker.get_account()
    price_map = {t: float(p) for t, p in prices.close.ffill().iloc[-1].items()
                 if _is_number(p)}
    # 시세를 못 받은 종목을 0으로 때우면 평가액이 조용히 줄어든다. 그 상태로
    # 기록에 남으면 가짜 낙폭이 되고, 낙폭 중단 장치가 엉뚱하게 발동한다.
    unpriced = [t for t in account.positions if t not in price_map]
    if unpriced:
        print(f"오류: 시세를 받지 못한 보유 종목이 있습니다: {', '.join(unpriced)}",
              file=sys.stderr)
        print("  평가액이 실제보다 작게 기록되면 가짜 낙폭이 됩니다. 남기지 않았습니다.",
              file=sys.stderr)
        return 1

    equity = account.equity(price_map)
    log = EquityLog(pipeline.equity_log_path(config))
    previous = log.last()
    log.append(
        _dt.date.today(), equity, account.cash,
        {t: pos.quantity for t, pos in account.positions.items()},
    )

    print(f"{_dt.date.today()} · 평가액 {equity:,.0f} {account.currency}"
          f" (현금 {account.cash:,.0f})")
    if previous:
        change = equity / previous["equity"] - 1.0 if previous["equity"] else 0.0
        print(f"  직전 기록({previous['date']}) 대비 {change:+.2%}")
    peak = log.peak()
    if peak and peak > 0:
        print(f"  고점 {peak:,.0f} 대비 {equity / peak - 1:+.2%}")
    print(f"기록: {pipeline.equity_log_path(config)}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """쌓인 평가액 기록으로 성과를 본다.

    기준선을 같이 낸다. 수익률만 보면 "올랐으니 잘했다"가 되는데, 그냥 들고만
    있어도 올랐을 수 있다. 전략이 한 일은 **그 차이**다.
    """
    from .execution.equity_log import EquityLog

    config = load_config(args.config)
    rows = EquityLog(pipeline.equity_log_path(config)).rows()
    if len(rows) < 2:
        print(f"기록이 {len(rows)}일치뿐이라 아직 볼 것이 없습니다.")
        print(f"  매일 쌓입니다: {pipeline.equity_log_path(config)}")
        return 0

    first, last = rows[0], rows[-1]
    days = (last["date"] - first["date"]).days
    total = last["equity"] / first["equity"] - 1.0 if first["equity"] else 0.0

    peak = run = 0.0
    for row in rows:
        peak = max(peak, row["equity"])
        if peak > 0:
            run = min(run, row["equity"] / peak - 1.0)

    print(f"기간: {first['date']} ~ {last['date']} ({days}일 · {len(rows)}회 기록)")
    print(f"  평가액  {first['equity']:,.0f} -> {last['equity']:,.0f}")
    print(f"  수익률  {total:+.2%}")
    print(f"  최대낙폭 {run:.2%}")

    bench = _benchmark_return(config, first["date"], last["date"], args.cache)
    if bench is None:
        print("\n  (기준선을 계산할 시세가 없습니다)")
    else:
        name, ret = bench
        print(f"\n기준선 {name} 보유: {ret:+.2%}")
        print(f"  차이: {total - ret:+.2%}p")

    # 이 경고는 기준선이 나왔든 아니든 떠야 한다. 기준선 계산에 매달아 뒀다가,
    # 시세가 없는 경우에 정작 제일 중요한 문구가 빠졌다.
    if days < 180:
        print("\n※ 기간이 짧습니다. 이 수익률은 전략의 실력이 아니라 운입니다.")
        print("  지금 확인할 수 있는 것은 '시스템이 제대로 도는가'까지입니다.")
    return 0


def _benchmark_return(config, start, end, cache_dir) -> tuple[str, float] | None:
    """첫 종목을 그냥 들고 있었을 때의 수익률."""
    if not config.assets:
        return None
    asset = config.assets[0]
    try:
        prices = load_prices(config, cache_dir=cache_dir)
        series = prices.close[asset.ticker].dropna()
        window = series[(series.index.date >= start) & (series.index.date <= end)]
        if len(window) < 2:
            return None
        return (asset.label, float(window.iloc[-1] / window.iloc[0] - 1.0))
    except Exception:
        return None


def _is_number(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def cmd_account(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    prices = load_prices(config, cache_dir=args.cache)
    broker = build_broker(config, prices)
    account = broker.get_account()
    price_map = broker.get_prices(list(account.positions.keys())) if account.positions else {}
    equity = account.equity(price_map)

    print(f"브로커: {broker.name} (실계좌={broker.is_live})")
    print(f"현금: {account.cash:,.0f} {account.currency}  (D+2 정산 기준)")
    # 예수금총금액은 오늘 산 대금이 아직 안 빠진 숫자다. 둘을 나란히 보여주지
    # 않으면 "현금이 그대로네"를 보고도 왜 그런지 알 수 없다.
    if hasattr(broker, "cash_detail"):
        try:
            detail = broker.cash_detail()
        except Exception:                      # 조회 실패가 잔고 출력을 막을 이유는 없다
            detail = None
        if detail and (detail["raw"] != detail["settled"] or detail["bought_today"]):
            print(f"  예수금총금액: {detail['raw']:,.0f}"
                  f" · 금일매수: {detail['bought_today']:,.0f}")
            print("  (주식 대금은 D+2 결제라 예수금총금액에는 오늘 산 금액이 아직 남아 있습니다)")
    print(f"평가액: {equity:,.0f} {account.currency}\n")
    for ticker, pos in sorted(account.positions.items()):
        price = price_map.get(ticker, 0.0)
        value = pos.market_value(price)
        weight = value / equity if equity else 0.0
        pnl = pos.unrealized_pnl(price)
        print(f"  {ticker:<10} {pos.quantity:>10,.0f}주  평가 {value:>14,.0f}"
              f"  비중 {weight:>6.2%}  평가손익 {pnl:>+14,.0f}")
    if not account.positions:
        print("  (보유 종목 없음)")
    return 0


def cmd_trade(args: argparse.Namespace) -> int:
    """리밸런싱 주문을 만들고, --execute가 있을 때만 제출한다."""
    config = load_config(args.config)
    prices = load_prices(config, cache_dir=args.cache)
    broker = build_broker(config, prices)
    orders, report, target = plan_rebalance(config, broker, prices)

    print(f"기준일: {prices.close.index[-1]:%Y-%m-%d} / 브로커: {broker.name}"
          f" (실계좌={broker.is_live})\n")
    print(reporting.format_weights(target, _names(config)))
    print()
    print(reporting.format_orders(orders))
    print()
    print(reporting.format_guard(report))

    if report.halted:
        print("\n안전장치가 전체 실행을 중단했습니다. 설정을 확인하세요.", file=sys.stderr)
        return 2
    if not args.execute:
        print("\n(모의 출력입니다. 실제 제출하려면 --execute를 붙이세요)")
        return 0

    # 같은 날 두 번 제출하면 목표 비중의 두 배를 산다. 실제로 그렇게 됐다 —
    # 결제 전 예수금을 '남은 현금'으로 읽는 버그와 겹쳐 069500을 45주씩 두 번
    # 샀다. 현금 계산은 고쳤지만, 무인 운영에서는 마지막 방어선이 하나 더 필요하다.
    # 장이 닫혀 있는데 주문을 내면 어떻게 되는지 확정적으로 알 수 없다. 거부될
    # 수도, 다음 장으로 넘어갈 수도 있다. 무인으로 도는 작업에서 결과를 모르는
    # 것은 그 자체로 위험하다. 월 1회 리밸런싱을 고정 날짜로 걸어두면 그 날이
    # 주말·공휴일일 확률이 3분의 1쯤 된다.
    #
    # 한 달을 거르는 쪽이 모르는 채로 내는 쪽보다 낫다. 비중 이탈은 다음 달에
    # 밴드가 바로잡지만, 닫힌 장에 들어간 주문은 되돌릴 방법이 없다. 거르더라도
    # 0이 아닌 종료코드로 끝나므로 스케줄러가 알려준다.
    if not args.anytime:
        closed = _closed_reason(config, broker)
        if closed:
            print(f"\n{closed}", file=sys.stderr)
            print("  지금 꼭 내야 한다면 --anytime 을 붙이세요.", file=sys.stderr)
            return 4

    today = _dt.date.today()
    window = _execute_window(config, today)
    if _last_execute_window(config) == window and not args.again:
        last_day = _last_execute_day(config) or "?"
        print(f"\n이번 주기({window})에 이미 주문을 제출했습니다({last_day}).", file=sys.stderr)
        print("  중복 제출을 막았습니다. 월 1회 리밸런싱이라면 이것이 정상입니다.",
              file=sys.stderr)
        print("  의도적으로 한 번 더 내려면 --again 을 붙이세요.", file=sys.stderr)
        return 3

    if not report.approved:
        print("\n제출할 주문이 없습니다.")
        return 0

    if broker.is_live and not args.yes:
        answer = input(f"\n실계좌에 {len(report.approved)}건을 제출합니다. 진행할까요? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("취소했습니다.")
            return 1

    print()
    _mark_executed(config, today.isoformat())
    for fill in broker.submit_all(report.approved):
        mark = "OK " if fill.status == "filled" else "!! "
        print(f"  {mark}{fill.ticker:<10} {fill.side.value:<4} {fill.quantity:>10,.0f}주"
              f" @{fill.price:>12,.2f}  수수료 {fill.fee:>10,.0f}  {fill.status}")
    print("\n접수됐습니다. 체결은 account로 다시 확인하세요(접수 != 체결).")
    return 0


def _execute_log_path(config) -> pathlib.Path:
    return pathlib.Path(config.execution.state_path).parent / "last_execute.json"


def _closed_reason(config, broker) -> str | None:
    """장이 닫혀 있으면 그 이유를, 열려 있으면 None.

    두 단계로 본다. 달력이 먼저고, 그다음이 실제 체결이다.

    달력만으로는 부족하다. 모의투자 계좌는 휴장일 조회 TR을 지원하지 않아
    (`모의투자 TR 이 아닙니다`) 달력이 **주말만** 거르는 상태로 물러난다.
    그 상태에서는 삼일절 대체공휴일 같은 날이 평일=개장으로 보인다. 그래서
    달력이 '열렸다'고 해도, 공휴일을 모르는 상태라면 오늘 체결이 실제로
    있었는지 한 번 더 확인한다.

    **확인하지 못한 것은 막지 않는다.** 조회가 실패하거나 빈 응답이면 판단을
    보류한다. 여기서 막으면 장이 멀쩡히 열린 날에 리밸런싱이 조용히 걸러진다.
    """
    if config.execution.broker.lower() != "kis":
        return None          # 로컬 모의 브로커는 장 시간과 무관하다

    import pathlib

    from .data.calendar import TradingCalendar
    from .data.kis_quotes import KISQuotes

    quotes = KISQuotes(broker)
    calendar = TradingCalendar(quotes, cache_path=str(
        pathlib.Path(config.execution.state_path).parent / "trading_days.json"))

    now = _dt.datetime.now()
    phase = calendar.phase(now)
    if phase != "open":
        return {
            "holiday": f"오늘({now:%Y-%m-%d})은 휴장일입니다. 주문하지 않았습니다.",
            "before_open": f"아직 개장 전입니다({now:%H:%M}, 09:00부터). 주문하지 않았습니다.",
            "after_close": f"이미 마감했습니다({now:%H:%M}, 15:30까지). 주문하지 않았습니다.",
        }.get(phase, f"장이 열려 있지 않습니다({phase}). 주문하지 않았습니다.")

    if calendar.knows_holidays():
        return None          # 달력이 공휴일까지 안다. 더 볼 것이 없다.

    try:
        bars = quotes.today_minutes(config.assets[0].ticker)
    except Exception:
        return None          # 못 물어본 것을 '닫혔다'로 읽지 않는다
    if bars.empty:
        return None
    traded = bars.index.max().date()
    if traded != now.date():
        return (f"달력이 공휴일을 모르는 상태인데, 최근 체결이 {traded}입니다.\n"
                f"  오늘({now:%Y-%m-%d})은 장이 서지 않은 것으로 보입니다. 주문하지 않았습니다.")
    return None


def _execute_window(config, day: _dt.date) -> str:
    """그날이 속한 **리밸런싱 주기**의 이름.

    중복 제출을 막는 단위다. 하루 단위로만 막으면, 스케줄러를 15·16·17일로
    걸어둔 경우(15일이 주말이면 거르니까) 15일에 내고 16일에 또 낸다.
    주기가 한 달이면 한 달에 한 번이 맞다.

    기준은 설정의 리밸런싱 주기다. 'M'을 코드에 박으면 주간 전략으로 바꿨을 때
    조용히 한 달에 한 번만 내게 된다.
    """
    try:
        freq = normalize_freq(getattr(config.backtest, "rebalance", "M"))
    except ValueError:
        return day.isoformat()      # 모르는 주기는 하루 단위로 (막지 않는 쪽보다 낫다)
    if freq == "Y":
        return f"{day.year}"
    if freq == "Q":
        return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
    if freq == "M":
        return f"{day:%Y-%m}"
    if freq == "W":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    return day.isoformat()          # 'D' 그리고 모르는 값은 하루 단위로


def _last_execute_window(config) -> str | None:
    """마지막으로 주문을 낸 주기. 읽지 못하면 None(막지 않는다)."""
    try:
        saved = json.loads(_execute_log_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    window = saved.get("window")
    if window:
        return str(window)
    # 하루 단위만 기록하던 때의 파일. 날짜로부터 주기를 되살린다 — 여기서
    # None을 돌려주면 판올림 직후 한 번은 방어선이 비어 있게 된다.
    date = saved.get("date")
    try:
        return _execute_window(config, _dt.date.fromisoformat(str(date)))
    except (TypeError, ValueError):
        return None


def _last_execute_day(config) -> str | None:
    """마지막으로 주문을 제출한 날짜. 읽지 못하면 None(막지 않는다)."""
    try:
        return json.loads(_execute_log_path(config).read_text(encoding="utf-8")).get("date")
    except (OSError, ValueError):
        return None


def _mark_executed(config, day: str) -> None:
    """제출 **직전에** 기록한다. 제출 후에 쓰면 중간에 죽었을 때 다시 낸다."""
    path = _execute_log_path(config)
    window = _execute_window(config, _dt.date.fromisoformat(day))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"date": day, "window": window}), encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"제출 기록을 남기지 못해 중단합니다: {exc}\n"
            "  기록이 없으면 같은 날 중복 제출을 막을 수 없습니다."
        ) from exc


DEFAULT_CONFIG = "config/portfolio.yaml"
DEFAULT_CACHE = "data/cache"


def _common_options() -> argparse.ArgumentParser:
    """서브커맨드 앞뒤 어디에 써도 되는 공통 옵션.

    `stockbot -c x.yaml backtest`와 `stockbot backtest -c x.yaml`이 둘 다
    동작해야 한다. 한쪽만 되면 반드시 기억이 틀린 쪽으로 손이 간다.

    기본값은 SUPPRESS로 두고 `main`에서 파싱 후에 채운다. `parents=`로 넘긴
    Action 객체는 부모와 서브파서가 **공유**하기 때문에, 여기서 기본값을
    지정하면 서브커맨드가 값을 받지 않았을 때 부모가 받은 값을 덮어쓴다.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", default=argparse.SUPPRESS, help="설정 파일 경로"
    )
    common.add_argument(
        "--cache", default=argparse.SUPPRESS, help="가격 캐시 디렉터리"
    )
    return common


def _version_text() -> str:
    """버전과 **실제로 불린 파일 경로**를 함께 보여준다.

    구버전이 설치돼 있으면 새로 추가한 명령이 `invalid choice`로 거부되는데,
    메시지만 보고는 원인을 알 수 없다. 경로까지 찍어서 'git pull은 했지만
    재설치를 안 했다' 같은 상황을 한 줄로 확인할 수 있게 한다.
    """
    import pathlib

    from . import __file__ as pkg_file

    try:
        from importlib.metadata import version

        installed = version("stockbot")
    except Exception:  # pragma: no cover - 메타데이터 없는 실행 환경
        installed = "알 수 없음(설치 정보 없음)"

    location = pathlib.Path(pkg_file).resolve().parent
    editable = location.parent.name == "src"
    mode = "편집 설치(-e) 또는 소스 직접 실행" if editable else "일반 설치"
    return (
        f"stockbot {installed}\n"
        f"  코드 위치: {location}\n"
        f"  설치 형태: {mode}\n"
        f"  사용 가능한 명령: {', '.join(COMMANDS)}"
    )


class _VersionAction(argparse.Action):
    """`--version`을 원문 그대로 출력한다.

    argparse의 기본 `action="version"`은 HelpFormatter를 거치면서 줄바꿈을
    뭉개고 한 문단으로 흘려버린다. 경로를 한 줄씩 읽어야 하므로 직접 찍는다.
    """

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: D102
        print(_version_text())
        parser.exit()


class _Parser(argparse.ArgumentParser):
    """모르는 명령을 만났을 때 원인까지 같이 알려주는 파서.

    argparse의 `invalid choice`는 "그런 명령 없음"까지만 말한다. 실제 원인은
    대개 둘 중 하나인데 둘 다 메시지에 안 나온다: 프로젝트 폴더가 아닌 곳에서
    실행했거나(그래서 git pull도 실패했거나), 받기만 하고 재설치를 안 했거나.
    두 경우 모두 `--version`의 코드 위치 한 줄로 판별된다.
    """

    def error(self, message: str):  # noqa: D102
        if "invalid choice" in message:
            import pathlib

            message += (
                "\n\n원인은 대개 '코드가 오래됐거나 실행 위치가 틀린' 경우입니다.\n"
                + _version_text()
                + f"\n  현재 작업 디렉터리: {pathlib.Path.cwd()}\n"
                "\n  위 '코드 위치'가 뜻밖의 경로라면 프로젝트 폴더로 이동한 뒤\n"
                "  git pull 을 다시 하세요. 설정과 .env도 그 폴더 안에 있어야 합니다."
            )
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = _Parser(
        prog="stockbot",
        description="포트폴리오 기반 투자 자동화",
        parents=[common],
    )
    parser.add_argument(
        "--version",
        action=_VersionAction,
        nargs=0,
        help="버전과 코드 위치 출력 (구버전이 설치됐는지 확인할 때)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backtest", help="과거 성과 시뮬레이션", parents=[common])
    p.add_argument("--out", default=None, help="결과 저장 디렉터리")
    p.set_defaults(func=cmd_backtest)

    sub.add_parser("compare", help="여러 전략 성과 비교", parents=[common]).set_defaults(
        func=cmd_compare
    )

    p = sub.add_parser(
        "walkforward", help="워크포워드 검증 (과최적화 측정)", parents=[common]
    )
    p.add_argument("--train", type=int, default=None, help="학습 구간 길이(봉)")
    p.add_argument("--test", type=int, default=None, help="검증 구간 길이(봉)")
    p.add_argument("--mode", choices=["rolling", "anchored"], default=None)
    p.add_argument("--objective", choices=sorted(OBJECTIVES), default=None, help="전략 선택 기준")
    p.add_argument(
        "--compare-full", action="store_true", help="전체 기간 백테스트와 나란히 비교"
    )
    p.set_defaults(func=cmd_walkforward)
    p = sub.add_parser(
        "breakout", help="변동성 돌파 워크포워드 + 합격 판정", parents=[common]
    )
    p.add_argument("--ticker", default=None, help="단일 종목 (기본: 설정의 첫 국내 종목)")
    p.add_argument("--start", default="2015-01-01", help="일봉 조회 시작일")
    p.add_argument("--end", default=None, help="조회 종료일 (기본: 오늘)")
    p.add_argument("--k", type=float, nargs="+", default=[0.3, 0.5, 0.7, 1.0],
                   help="돌파 배수 후보")
    p.add_argument("--lookback", type=int, default=1, help="변동폭을 잴 거래일 수")
    p.add_argument("--train", type=int, default=756, help="학습 구간(거래일)")
    p.add_argument("--test", type=int, default=252, help="검증 구간(거래일)")
    p.add_argument("--mode", choices=["rolling", "anchored"], default="rolling")
    p.add_argument("--no-costs", action="store_true",
                   help="비용을 0으로 (비용이 얼마나 먹는지 볼 때만. 판정용 아님)")
    p.set_defaults(func=cmd_breakout)

    sub.add_parser("signal", help="현재 목표 비중 출력", parents=[common]).set_defaults(
        func=cmd_signal
    )
    p = sub.add_parser(
        "checkenv", help="KIS 접속 정보 점검 (주문 없이 인증만 확인)", parents=[common]
    )
    p.add_argument("--offline", action="store_true", help="서버 인증 단계를 건너뛴다")
    p.set_defaults(func=cmd_checkenv)

    p = sub.add_parser(
        "minutes", help="분봉 조회 가능 여부 확인 (일중 전략의 전제)", parents=[common]
    )
    p.add_argument("--day", default=None, help="조회할 날짜 YYYY-MM-DD (기본: 직전 거래일)")
    p.add_argument("--store", default="data/minutes", help="분봉 보관 폴더")
    p.set_defaults(func=cmd_minutes)

    p = sub.add_parser(
        "collect", help="분봉을 모아 둔다 (일중 전략 검증용, 매일 1회)", parents=[common]
    )
    p.add_argument("--days", type=int, default=7, help="며칠 전부터 받을지 (기본 7)")
    p.add_argument("--start", default=None, help="시작일 YYYY-MM-DD (--days보다 우선)")
    p.add_argument("--end", default=None, help="종료일 YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--store", default="data/minutes", help="보관 폴더")
    p.add_argument("--verbose", action="store_true", help="날짜별 건수 출력")
    p.set_defaults(func=cmd_collect)

    sub.add_parser("account", help="계좌 상태 조회", parents=[common]).set_defaults(
        func=cmd_account
    )

    p = sub.add_parser(
        "snapshot", help="오늘 평가액을 기록에 남긴다 (매일 마감 후 1회)", parents=[common]
    )
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser(
        "report", help="쌓인 평가액 기록으로 성과 보기", parents=[common]
    )
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("trade", help="리밸런싱 주문 생성/제출", parents=[common])
    p.add_argument("--execute", action="store_true", help="실제로 주문을 제출한다")
    p.add_argument("--yes", action="store_true", help="실계좌 확인 프롬프트를 건너뛴다")
    p.add_argument("--again", action="store_true",
                   help="같은 날 두 번째 제출을 허용한다 (기본은 막는다)")
    p.add_argument("--anytime", action="store_true",
                   help="장이 닫혀 있어도 제출한다 (기본은 막는다)")
    p.set_defaults(func=cmd_trade)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # SUPPRESS로 둔 공통 옵션의 기본값을 여기서 채운다.
    if not hasattr(args, "config"):
        args.config = DEFAULT_CONFIG
    if not hasattr(args, "cache"):
        args.cache = DEFAULT_CACHE
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, KeyError, NotImplementedError,
            ImportError, BrokerError) as exc:
        # 브로커 오류(키 누락, 주문 거부 등)와 선택 의존성 누락은 사용자가
        # 고칠 수 있는 문제다. 트레이스백으로 덮으면 정작 읽어야 할 안내가 묻힌다.
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
