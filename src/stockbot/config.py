"""YAML 설정 로딩과 객체 조립.

전략 파라미터를 코드에 박아두면 바꿀 때마다 코드를 고쳐야 하고, 어떤 설정으로
돌린 결과인지 추적이 안 된다. 설정은 파일로 두고 결과와 함께 보관한다.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
from typing import Any

import yaml

from .backtest.costs import PRESETS, CostBook, CostModel
from .data.loader import AssetSpec
from .execution.guards import RiskGuard
from .execution.order import OrderType
from .portfolio.base import Strategy
from .portfolio.fixed import EqualWeight, FixedWeight
from .portfolio.momentum import DualMomentum, MomentumRiskParity
from .portfolio.risk_parity import RiskParity


@dataclasses.dataclass
class BacktestConfig:
    start: str = "2015-01-01"
    end: str | None = None
    initial_cash: float = 10_000_000.0
    rebalance: str = "M"
    band: float = 0.0
    execution: str = "next_open"
    allow_fractional: bool = False
    cash_rate: float = 0.0
    min_trade_value: float = 0.0
    risk_free: float = 0.0


@dataclasses.dataclass
class ExecutionConfig:
    broker: str = "paper"
    state_path: str = ".state/paper_account.json"
    cash_buffer: float = 0.005
    min_trade_value: float = 0.0
    band: float = 0.0
    order_type: str = "market"
    limit_slippage: float = 0.003
    allow_fractional: bool = False
    dry_run: bool = True

    def as_order_type(self) -> OrderType:
        return OrderType(self.order_type)


@dataclasses.dataclass
class WalkForwardConfig:
    """워크포워드 검증 설정.

    기본값은 학습 3년 / 검증 1년이다. 학습 구간이 너무 짧으면 전략을 고를
    근거가 없고, 너무 길면 구간 수가 줄어 결과 자체가 우연에 좌우된다.
    """

    train: int = 756          # 3년(거래일)
    test: int = 252           # 1년
    step: int | None = None   # None이면 검증 구간이 겹치지 않게 test와 동일
    mode: str = "rolling"     # rolling | anchored
    objective: str = "sharpe"
    candidates: list[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class AppConfig:
    """설정 파일 전체."""

    name: str
    assets: list[AssetSpec]
    strategy: Strategy
    backtest: BacktestConfig
    execution: ExecutionConfig
    guard: RiskGuard
    walkforward: WalkForwardConfig = dataclasses.field(default_factory=WalkForwardConfig)
    base_currency: str = "KRW"
    default_market: str = "US"
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def tickers(self) -> list[str]:
        return [a.ticker for a in self.assets]

    @property
    def lot_sizes(self) -> dict[str, int]:
        return {a.ticker: a.lot_size for a in self.assets}

    def candidate_strategies(self) -> dict[str, Strategy]:
        """워크포워드에서 비교할 전략들.

        설정에 candidates가 없으면 설정 전략 하나만 넣는다. 그 경우 선택은
        일어나지 않고, 그 전략이 구간마다 얼마나 안정적인지만 보게 된다.
        """
        specs = self.walkforward.candidates
        if not specs:
            return {self.strategy.name: self.strategy}
        return {
            name: _build_strategy(spec) for name, spec in _named_candidates(specs)
        }

    def cost_book(self) -> CostBook:
        """종목별 비용표. 설정의 `costs.overrides`로 시장 프리셋을 덮어쓸 수 있다."""
        overrides = (self.raw.get("costs") or {}).get("overrides") or {}
        per_ticker: dict[str, CostModel] = {}
        for asset in self.assets:
            preset = overrides.get(asset.market, asset.market)
            if preset not in PRESETS:
                raise ValueError(
                    f"{asset.ticker}: 알 수 없는 비용 프리셋 {preset!r}. 사용 가능: {sorted(PRESETS)}"
                )
            per_ticker[asset.ticker] = PRESETS[preset]
        return CostBook(default=PRESETS[self.default_market], per_ticker=per_ticker)


def _make(cls, params: dict[str, Any], kind: str, where: str = "params"):
    """전략을 만들되, 맞지 않는 설정 항목은 **무엇이 문제인지** 알려준다.

    전략 type만 바꾸고 이전 params를 지우지 않는 실수가 흔하다. 그대로
    넘기면 `TypeError: __init__() got an unexpected keyword argument`라는
    트레이스백이 뜨는데, 설정 파일을 고쳐야 한다는 걸 알아채기 어렵다.
    """
    import inspect

    accepted = set(inspect.signature(cls).parameters) - {"self"}
    unknown = sorted(set(params) - accepted)
    if unknown:
        raise ValueError(
            f"설정 strategy.{where} 에 쓸 수 없는 항목이 있습니다: {', '.join(unknown)}\n"
            f"  전략 type: {kind!r} (이 자리는 {cls.__name__})\n"
            f"  여기서 받는 항목: {sorted(accepted) or '(없음)'}\n"
            f"  전략 type만 바꾸고 이전 전략의 params를 지우지 않으면 이 오류가 납니다.\n"
            f"  설정 파일을 열어 해당 줄을 지우거나 주석(#) 처리하세요."
        )
    return cls(**params)


def _build_strategy(spec: dict[str, Any]) -> Strategy:
    """설정의 strategy 블록을 전략 객체로 만든다."""
    kind = str(spec.get("type", "")).strip().lower()
    params = dict(spec.get("params") or {})

    if kind in {"fixed", "fixed_weight"}:
        return _make(FixedWeight, params, kind)
    if kind in {"equal", "equal_weight"}:
        return _make(EqualWeight, params, kind)
    if kind in {"risk_parity", "rp"}:
        return _make(RiskParity, params, kind)
    if kind in {"dual_momentum", "momentum"}:
        return _make(DualMomentum, params, kind)
    if kind in {"momentum_risk_parity", "combo"}:
        momentum_spec = params.pop("momentum", {}) or {}
        sizer_spec = params.pop("sizer", {}) or {}
        momentum = _make(DualMomentum, momentum_spec, kind, "params.momentum")
        sizer = _make(RiskParity, sizer_spec, kind, "params.sizer")
        rest = {"momentum": momentum, "sizer": sizer, **params}
        return _make(MomentumRiskParity, rest, kind)
    raise ValueError(
        f"알 수 없는 전략 type: {spec.get('type')!r}. "
        "사용 가능: fixed, equal, risk_parity, dual_momentum, momentum_risk_parity"
    )


def copy_command(
    source: pathlib.Path, target: pathlib.Path, windows: bool | None = None
) -> str:
    r"""현재 OS의 셸에서 그대로 붙여넣을 수 있는 복사 명령.

    Windows 명령 프롬프트에는 `cp`가 없고 경로 구분자도 `\`다. 안내 문구가
    한 OS만 가정하면, 쓰는 사람은 같은 자리에서 반복해서 막힌다.

    Args:
        windows: None이면 실행 중인 OS로 판단한다. 테스트에서 명시적으로
            지정할 수 있도록 인자로 받는다(`os.name`을 바꾸면 pathlib이 깨진다).
    """
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return f"copy {_backslash(source)} {_backslash(target)}"
    return f"cp {source.as_posix()} {target.as_posix()}"


def _backslash(path: pathlib.Path) -> str:
    return path.as_posix().replace("/", "\\")


def _named_candidates(specs: list[dict]) -> list[tuple[str, dict]]:
    """후보 목록에 이름을 붙이고 중복을 막는다.

    이름이 겹치면 한쪽이 조용히 사라져, 비교한 줄 알았던 전략이 실제로는
    빠진 채 결과가 나온다.
    """
    named: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for i, spec in enumerate(specs):
        spec = dict(spec)
        name = spec.pop("name", None) or spec.get("type") or f"candidate{i}"
        if name in seen:
            raise ValueError(f"walkforward.candidates에 중복된 이름: {name!r}")
        seen.add(name)
        named.append((name, spec))
    return named


def load_config(path: str | pathlib.Path) -> AppConfig:
    """YAML 설정 파일을 읽어 AppConfig로 만든다."""
    path = pathlib.Path(path)
    if not path.exists():
        example = path.parent / "portfolio.example.yaml"
        lines = [f"설정 파일이 없습니다: {path}"]
        if example.exists():
            lines += [
                "예시 설정을 복사해서 시작하세요:",
                f"  {copy_command(example, path)}",
            ]
        else:
            lines += [
                f"예시 설정도 찾지 못했습니다: {example}",
                f"  현재 작업 디렉터리: {os.getcwd()}",
                "  프로젝트 루트에서 실행했는지 확인하세요.",
            ]
        raise FileNotFoundError("\n".join(lines))
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return from_dict(raw)


def from_dict(raw: dict[str, Any]) -> AppConfig:
    """딕셔너리에서 설정을 만든다(테스트와 프로그램 내 구성용)."""
    base_currency = raw.get("base_currency", "KRW")
    default_market = raw.get("default_market", "US")

    assets_raw = raw.get("assets") or []
    if not assets_raw:
        raise ValueError("설정에 assets가 비어 있습니다")
    assets = [
        AssetSpec(
            ticker=str(a["ticker"]),
            market=a.get("market", default_market),
            currency=a.get("currency", "KRW" if a.get("market") == "KR" else "USD"),
            name=a.get("name", ""),
            lot_size=int(a.get("lot_size", 1)),
        )
        for a in assets_raw
    ]

    duplicates = {t for t in (a.ticker for a in assets) if [x.ticker for x in assets].count(t) > 1}
    if duplicates:
        raise ValueError(f"assets에 중복된 티커가 있습니다: {sorted(duplicates)}")

    strategy = _build_strategy(raw.get("strategy") or {"type": "equal"})
    backtest = BacktestConfig(**(raw.get("backtest") or {}))
    execution = ExecutionConfig(**(raw.get("execution") or {}))
    walkforward = WalkForwardConfig(**(raw.get("walkforward") or {}))
    _named_candidates(walkforward.candidates)  # 설정을 읽는 시점에 이름 충돌을 잡는다

    risk_raw = dict(raw.get("risk") or {})
    allowed = risk_raw.pop("allowed_tickers", None)
    guard = RiskGuard(
        allowed_tickers=set(allowed) if allowed else {a.ticker for a in assets},
        **risk_raw,
    )

    return AppConfig(
        name=raw.get("name", "portfolio"),
        assets=assets,
        strategy=strategy,
        backtest=backtest,
        execution=execution,
        guard=guard,
        walkforward=walkforward,
        base_currency=base_currency,
        default_market=default_market,
        raw=raw,
    )
