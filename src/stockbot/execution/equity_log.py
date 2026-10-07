"""계좌 평가액 이력.

**지나간 날은 되돌려 받을 수 없다.** 분봉과 같은 이유로 오늘부터 쌓는다.
몇 달 뒤 "실계좌로 넘어갈까"를 판단할 때 근거가 될 유일한 기록이고,
그때 가서 만들 수는 없다.

기록이 없어 못 하던 일이 두 가지 있었다.

1. 성과를 따질 수 없었다. 로그에 찍힌 글자는 2만 줄이 넘으면 잘려 나간다.
2. 낙폭 중단 장치가 발동할 수 없었다. 고점을 모르면 낙폭을 못 센다. 설정에
   `max_drawdown_stop: 0.25`라고 써 있고 README에도 설명돼 있는데, 고점을
   넘겨주는 곳이 없어 조건문이 늘 건너뛰어졌다. 있다고 믿게 만드는 장치는
   없느니만 못하다.

형식은 CSV로 둔다. 사람이 열어 볼 수 있고, 표 프로그램에 바로 붙으며,
한 줄씩 덧붙이는 데 적합하다.
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib

FIELDS = ("date", "equity", "cash", "invested", "holdings")

#: 보유 내역을 한 칸에 적을 때의 구분자. 쉼표는 CSV와 겹쳐서 못 쓴다.
HOLDING_SEP = " "


class EquityLog:
    """하루 한 줄씩 쌓는 평가액 기록.

    같은 날짜가 다시 들어오면 **덮어쓴다**. 하루에 두 번 돌리는 일이 생겨도
    그날이 두 줄이 되면 안 된다 — 수익률 계산이 조용히 틀어진다.
    """

    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    # ------------------------------------------------------------ 읽기
    def rows(self) -> list[dict]:
        """날짜순으로 정렬된 기록. 읽지 못하면 빈 목록."""
        try:
            with self.path.open(encoding="utf-8", newline="") as fh:
                raw = list(csv.DictReader(fh))
        except (OSError, ValueError):
            return []

        out = []
        for row in raw:
            try:
                out.append({
                    "date": dt.date.fromisoformat(row["date"]),
                    "equity": float(row["equity"]),
                    "cash": float(row["cash"]),
                    "invested": float(row["invested"]),
                    "holdings": row.get("holdings", ""),
                })
            except (KeyError, TypeError, ValueError):
                # 한 줄이 깨졌다고 전체를 버리지 않는다. 몇 달치 기록이다.
                continue
        return sorted(out, key=lambda r: r["date"])

    def peak(self) -> float | None:
        """지금까지의 최고 평가액. 기록이 없으면 None.

        None은 '낙폭 0'이 아니라 **'모른다'**는 뜻이다. 호출하는 쪽이 0으로
        때우면 첫날부터 낙폭 100%로 읽힌다.
        """
        values = [r["equity"] for r in self.rows() if r["equity"] > 0]
        return max(values) if values else None

    def last(self) -> dict | None:
        rows = self.rows()
        return rows[-1] if rows else None

    # ------------------------------------------------------------ 쓰기
    def append(self, day: dt.date, equity: float, cash: float,
               holdings: dict[str, float] | None = None) -> None:
        """하루치를 남긴다. 같은 날짜는 덮어쓴다."""
        kept = [r for r in self.rows() if r["date"] != day]
        kept.append({
            "date": day,
            "equity": float(equity),
            "cash": float(cash),
            "invested": float(equity) - float(cash),
            "holdings": _format_holdings(holdings or {}),
        })
        kept.sort(key=lambda r: r["date"])

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 통째로 다시 쓴다. 하루 한 줄이라 몇 년치라도 수천 줄이고, 덧붙이기만
        # 하면 같은 날짜가 쌓이는 것을 막을 수 없다.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            for row in kept:
                writer.writerow({
                    "date": row["date"].isoformat(),
                    "equity": f"{row['equity']:.2f}",
                    "cash": f"{row['cash']:.2f}",
                    "invested": f"{row['invested']:.2f}",
                    "holdings": row["holdings"],
                })
        # 쓰다 말고 죽으면 기록이 통째로 날아간다. 다 쓴 뒤에 바꿔 끼운다.
        tmp.replace(self.path)


def _format_holdings(holdings: dict[str, float]) -> str:
    return HOLDING_SEP.join(f"{t}:{q:g}" for t, q in sorted(holdings.items()) if q)
