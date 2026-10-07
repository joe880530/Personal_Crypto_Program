#!/usr/bin/env python3
"""업비트 캔들을 CSV로 받는다. **표준 라이브러리만 쓴다.**

pandas도, 이 저장소의 패키지도 필요 없다. 파이썬만 있으면 어디서든 돈다.
개발 샌드박스에서 api.upbit.com 이 차단돼 있어, 인터넷이 되는 쪽(NAS 등)에서
데이터만 받아 오려고 만들었다.

    python3 scripts/fetch_upbit_csv.py KRW-BTC --days 760 --out data/csv/KRW-BTC.csv

출력은 `data/csv/{티커}.csv` 형식으로, 이 저장소의 CsvProvider가 그대로 읽는다
(날짜 index + open/high/low/close/volume).

**업비트 응답의 함정 두 가지를 그대로 반영했다.**

  - count는 200이 한도인데 넘겨도 오류 없이 잘린다. 그래서 요청한 개수가
    아니라 **받은 것의 시각**으로 다음 구간을 정한다.
  - `to`는 UTC이고 그 시각을 포함하지 않는다. 받은 것 중 가장 이른 시각을
    다음 `to`로 넣으면 겹치지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.upbit.com/v1"
MAX_COUNT = 200
PACE = 0.12          # 초당 10건이 한도지만 그대로 쓰면 분 한도(600)에 걸린다
TIMEOUT = 45
RETRIES = 3

FIELDS = ("opening_price", "high_price", "low_price", "trade_price",
          "candle_acc_trade_volume")
HEADER = ["datetime", "open", "high", "low", "close", "volume"]


def get(url: str):
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < RETRIES - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise SystemExit(f"업비트 응답 오류 HTTP {exc.code}: {exc.read()[:200]!r}")
        except Exception as exc:                       # 끊긴 호출은 다시 시도한다
            if attempt == RETRIES - 1:
                raise SystemExit(f"업비트에 접속하지 못했습니다: {exc}")
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def fetch(market: str, days: int, unit: int | None) -> list[dict]:
    path = "/candles/days" if unit is None else f"/candles/minutes/{unit}"
    rows: dict[str, dict] = {}
    cursor = dt.datetime.now(dt.timezone.utc)
    pages = 0

    while len(rows) < days:
        params = {"market": market, "count": MAX_COUNT,
                  "to": cursor.strftime("%Y-%m-%dT%H:%M:%SZ")}
        page = get(f"{BASE}{path}?{urllib.parse.urlencode(params)}")
        if not isinstance(page, list) or not page:
            break
        missing = [f for f in FIELDS if f not in page[0]]
        if missing:
            raise SystemExit(f"응답에 필요한 필드가 없습니다: {missing}\n"
                             f"  받은 필드: {sorted(page[0])}")

        oldest = None
        for row in page:
            stamp = row["candle_date_time_utc"]
            rows[stamp] = row
            if oldest is None or stamp < oldest:
                oldest = stamp
        pages += 1
        print(f"  {pages}쪽 · 누적 {len(rows)}건 · {oldest}", file=sys.stderr)

        # 받은 것 중 가장 이른 시각을 다음 to 로. 요청한 개수를 믿으면 안 된다.
        nxt = dt.datetime.strptime(oldest, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc)
        if nxt >= cursor:
            break                                      # 더 과거가 없다
        cursor = nxt
        time.sleep(PACE)

    ordered = sorted(rows.values(), key=lambda r: r["candle_date_time_utc"])
    return ordered[-days:]


def main() -> int:
    ap = argparse.ArgumentParser(description="업비트 캔들을 CSV로 받는다")
    ap.add_argument("market", help="예: KRW-BTC")
    ap.add_argument("--days", type=int, default=760, help="받을 봉 개수 (기본 760)")
    ap.add_argument("--unit", type=int, default=None, help="분봉 단위. 생략하면 일봉")
    ap.add_argument("--out", default=None, help="출력 파일 (기본 data/csv/{마켓}.csv)")
    args = ap.parse_args()

    out = pathlib.Path(args.out or f"data/csv/{args.market}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"{args.market} · {'일봉' if args.unit is None else f'{args.unit}분봉'}"
          f" {args.days}건", file=sys.stderr)
    rows = fetch(args.market, args.days, args.unit)
    if not rows:
        raise SystemExit("한 건도 받지 못했습니다.")

    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for r in rows:
            writer.writerow([
                r["candle_date_time_utc"],
                r["opening_price"], r["high_price"], r["low_price"],
                r["trade_price"], r.get("candle_acc_trade_volume", 0),
            ])

    first, last = rows[0]["candle_date_time_utc"], rows[-1]["candle_date_time_utc"]
    print(f"\n{len(rows)}건 저장: {out}", file=sys.stderr)
    print(f"구간: {first} ~ {last}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
