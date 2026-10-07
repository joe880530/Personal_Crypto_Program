"""결과 출력과 저장.

외부 차트 라이브러리를 쓰지 않는다. 터미널에서 바로 읽히는 표가 자동화
파이프라인(크론, 알림)에 붙이기도 쉽다.
"""

from __future__ import annotations

import json
import pathlib
import unicodedata

import pandas as pd

from .execution.guards import GuardReport
from .execution.order import Order, Side

def display_width(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글·한자는 2칸을 쓴다.

    파이썬의 문자열 길이로 자리를 맞추면 한글이 섞인 표가 전부 어긋난다.
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int, align: str = "left") -> str:
    """표시 너비 기준으로 자리를 맞춘다."""
    padding = " " * max(0, width - display_width(text))
    if align == "right":
        return padding + text
    return text + padding


_LABELS = {
    "start_value": "시작 자산",
    "end_value": "최종 자산",
    "total_return": "총 수익률",
    "cagr": "연평균 수익률(CAGR)",
    "volatility": "연변동성",
    "sharpe": "샤프",
    "sortino": "소르티노",
    "max_drawdown": "최대 낙폭(MDD)",
    "max_drawdown_days": "최대 낙폭 지속(봉)",
    "calmar": "칼마",
    "best_day": "최고 일간",
    "worst_day": "최저 일간",
    "positive_days": "상승일 비율",
    "total_costs": "총 거래비용",
    "cost_drag_pct_of_initial": "비용/초기자산",
    "annual_turnover": "연 회전율",
    "num_trades": "체결 건수",
    "windows": "검증 구간 수",
}

_PERCENT_KEYS = {
    "total_return", "cagr", "volatility", "max_drawdown", "best_day",
    "worst_day", "positive_days", "cost_drag_pct_of_initial",
}
_MONEY_KEYS = {"start_value", "end_value", "total_costs"}


def format_summary(summary: dict[str, float], title: str = "백테스트 결과") -> str:
    """성과 지표를 표로."""
    lines = [title, "=" * max(len(title), 46)]
    for key, value in summary.items():
        label = _LABELS.get(key, key)
        if value != value:  # NaN
            text = "-"
        elif key in _PERCENT_KEYS:
            text = f"{value:.2%}"
        elif key in _MONEY_KEYS:
            text = f"{value:,.0f}"
        elif key in {"num_trades", "max_drawdown_days", "windows"}:
            text = f"{int(value):,}"
        else:
            text = f"{value:.3f}"
        lines.append(f"  {pad(label, 22)} {pad(text, 18, 'right')}")
    return "\n".join(lines)


def format_weights(
    weights: pd.Series, names: dict[str, str] | None = None, title: str = "목표 비중"
) -> str:
    """비중 Series를 표로. 0인 종목은 생략한다."""
    names = names or {}
    active = weights[weights.abs() > 1e-6].sort_values(ascending=False)
    lines = [title, "-" * max(len(title), 46)]
    if active.empty:
        lines.append("  (전량 현금)")
        return "\n".join(lines)
    for ticker, w in active.items():
        label = names.get(ticker, ticker)
        bar = "█" * int(round(float(w) * 30))
        lines.append(f"  {pad(label, 18)} {w:>7.2%} {bar}")
    lines.append(f"  {pad('합계', 18)} {active.sum():>7.2%}")
    return "\n".join(lines)


def format_comparison(rows: dict[str, dict[str, float]]) -> str:
    """여러 전략의 성과를 나란히 비교한다."""
    keys = ["cagr", "volatility", "max_drawdown", "sharpe", "calmar", "annual_turnover", "total_costs"]
    header = pad("전략", 26) + "".join(pad(_LABELS[k], 16, "right") for k in keys)
    lines = [header, "-" * display_width(header)]
    for name, summary in rows.items():
        cells = []
        for key in keys:
            value = summary.get(key, float("nan"))
            if value != value:
                cells.append(f"{'-':>15} ")
            elif key in _PERCENT_KEYS:
                cells.append(f"{value:>15.2%} ")
            elif key in _MONEY_KEYS:
                cells.append(f"{value:>15,.0f} ")
            else:
                cells.append(f"{value:>15.2f} ")
        lines.append(pad(name, 26) + "".join(cells))
    return "\n".join(lines)


def format_orders(orders: list[Order], title: str = "주문 계획") -> str:
    """주문 목록을 표로."""
    lines = [title, "-" * max(len(title), 46)]
    if not orders:
        lines.append("  (주문 없음 — 이미 목표 비중 범위 안)")
        return "\n".join(lines)
    total_buy = sum(o.notional for o in orders if o.side is Side.BUY)
    total_sell = sum(o.notional for o in orders if o.side is Side.SELL)
    for order in orders:
        lines.append(f"  {pad(order.describe(), 40)} {order.notional:>14,.0f}  # {order.note}")
    lines.append(f"  {pad('매도 합계', 40)} {total_sell:>14,.0f}")
    lines.append(f"  {pad('매수 합계', 40)} {total_buy:>14,.0f}")
    return "\n".join(lines)


def format_guard(report: GuardReport) -> str:
    return report.describe()


def save_results(result, outdir: str | pathlib.Path, name: str = "backtest") -> pathlib.Path:
    """자산곡선·비중·체결·요약을 파일로 남긴다."""
    out = pathlib.Path(outdir) / name
    out.mkdir(parents=True, exist_ok=True)
    result.equity.to_csv(out / "equity.csv")
    result.weights.to_csv(out / "weights.csv")
    result.trades.to_csv(out / "trades.csv", index=False)
    (out / "summary.json").write_text(
        json.dumps(result.summary(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out


def format_walkforward(result, index, risk_free: float = 0.0) -> str:
    """워크포워드 결과를 표로.

    핵심은 자산곡선이 아니라 **학습 성과와 검증 성과의 차이**다. 그 차이가
    이 전략을 실제로 굴렸을 때 기대와 현실이 벌어질 폭이다.
    """
    objective = result.objective
    lines = ["워크포워드 검증", "=" * 78]
    lines.append(
        f"  구간 {len(result.windows)}개 · 선택 기준 {objective}"
        f" · 검증 대상 {result.equity.index[0]:%Y-%m-%d} ~ {result.equity.index[-1]:%Y-%m-%d}"
    )
    lines.append("")

    name_width = max([display_width(w.selected) for w in result.windows] + [8]) + 2
    header = (
        "  " + pad("학습 -> 검증", 40) + pad("선택", name_width)
        + pad("학습", 9, "right") + pad("검증", 9, "right")
        + pad("검증 CAGR", 11, "right") + pad("검증 MDD", 11, "right")
    )
    rule = "  " + "-" * (display_width(header) - 2)
    lines.append(header)
    lines.append(rule)
    for window in result.windows:
        test_score = result.objective_fn(window.test_summary)
        lines.append(
            "  " + pad(window.window.label(index), 40) + pad(window.selected, name_width)
            + f"{window.train_score:>9.2f}{test_score:>9.2f}"
            + f"{window.test_summary.get('cagr', float('nan')):>11.1%}"
            + f"{window.test_summary.get('max_drawdown', float('nan')):>11.1%}"
        )

    lines.append("")
    lines.append(rule)
    in_sample = result.in_sample_mean()
    out_sample = result.out_of_sample_mean()
    efficiency = result.efficiency()
    lines.append(f"  {pad('학습 구간 평균 ' + objective, 28)}{in_sample:>8.3f}")
    lines.append(f"  {pad('검증 구간 평균 ' + objective, 28)}{out_sample:>8.3f}")
    single = getattr(result, "n_candidates", 1) <= 1
    lines.append(
        f"  {pad('워크포워드 효율 (검증/학습)', 28)}{efficiency:>8.2f}"
        f"   {_efficiency_verdict(efficiency, single)}"
    )

    churn = result.selection_churn()
    if single:
        # 후보가 하나면 뒤집힐 대상이 없어 뒤집힘은 항상 0이다. 그 0을
        # '안정적'이라고 칭찬하면 검증한 적 없는 것을 검증했다고 믿게 된다.
        lines.append(
            f"  {pad('선택 뒤집힘 비율', 28)}{'해당 없음':>8}"
            "   후보가 1개라 '선택'이 없었습니다"
        )
    elif churn == churn:
        lines.append(
            f"  {pad('선택 뒤집힘 비율', 28)}{churn:>8.2f}   {_churn_verdict(churn)}"
        )
    counts = result.selection_counts()
    if len(counts) > 1:
        picked = ", ".join(f"{name} {n}회" for name, n in counts.items())
        lines.append(f"  선택 분포: {picked}")

    if single:
        lines.append("")
        lines.extend(_single_candidate_notice())

    lines.append("")
    lines.append(format_summary(result.summary(risk_free), "검증 구간만 이어 붙인 성과"))
    return "\n".join(lines)


def _single_candidate_notice() -> list[str]:
    """후보가 1개일 때, 이 실행이 무엇을 검증하지 *않았는지* 밝힌다."""
    return [
        "  ※ 후보가 1개인 실행입니다. 위 두 지표는 '여러 전략 중 고른 것이",
        "     과최적화였는지'를 재는 값인데, 고를 대상이 없었으므로 그 의미로",
        "     읽을 수 없습니다. 지금 보고 있는 것은 같은 전략의 **구간별 편차**",
        "     뿐입니다.",
        "     전략 선택까지 검증하려면 설정에 후보를 넣고 다시 돌리세요:",
        "",
        "       walkforward:",
        "         candidates:",
        "           - {name: 균등, type: equal}",
        "           - {name: 리스크패리티, type: risk_parity}",
        "           - {name: 듀얼모멘텀, type: dual_momentum}",
        "           - {name: 모멘텀+리스크패리티, type: momentum_risk_parity}",
    ]


def _efficiency_verdict(efficiency: float, single_candidate: bool = False) -> str:
    """효율 수치를 말로 옮긴다. 숫자만 보면 좋고 나쁨을 판단하기 어렵다."""
    if efficiency != efficiency:
        return "판단 불가"
    if efficiency < 0:
        return "학습 구간에서 좋아 보인 것이 검증 구간에서는 손해였음"
    if efficiency < 0.3:
        return "대부분 운이었음. 이 전략 선택을 믿지 마세요"
    if efficiency < 0.6:
        return "절반 가까이 사라짐. 기대치를 크게 낮추세요"
    if efficiency < 0.9:
        return "어느 정도 재현됨. 성과는 학습 구간보다 낮게 잡으세요"
    if efficiency > 1.2:
        # 검증이 학습보다 좋다는 것은 전략이 튼튼하다는 뜻이 아니다.
        # 대개 학습 구간이 검증 구간보다 어려운 시기였다는 뜻이다.
        return "검증이 학습보다 좋음 — 전략이 튼튼한 게 아니라 구간 난이도 차이입니다"
    if single_candidate:
        return "구간 간 편차가 작음 (과최적화 지표로는 읽지 마세요 — 아래 참고)"
    return "잘 재현됨. 다만 구간 수가 적으면 이 값도 우연일 수 있습니다"


def _churn_verdict(churn: float) -> str:
    if churn >= 0.7:
        return "매번 1등이 바뀜 — 데이터가 아니라 잡음이 고르고 있습니다"
    if churn >= 0.4:
        return "선택이 자주 바뀜 — 후보를 줄이는 편이 낫습니다"
    return "선택이 안정적"
