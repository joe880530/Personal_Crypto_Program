#!/usr/bin/env python3
"""설치된 stockbot이 실제로 쓸 수 있는 상태인지 검사한다.

    python scripts/check_install.py

**저장소 밖에서 실행해야 의미가 있다.** 저장소 루트에서 `pytest`를 돌리면
pyproject의 `pythonpath = ["src"]` 설정 덕분에 소스가 직접 import되므로,
패키지 설정이 깨져 있어도 전부 통과한다. 실제로 그렇게 통과시켜 놓고
clone한 환경에서 `ModuleNotFoundError: No module named 'stockbot.data'`가
난 적이 있다(.gitignore의 `data/` 규칙이 src/stockbot/data/까지 제외했다).

그래서 이 스크립트는 sys.path에 src/가 없는 상태에서 설치본만 보고
모든 하위 모듈을 import해본다. CI가 매 푸시마다 이걸 돌린다.
"""

from __future__ import annotations

import importlib
import pathlib
import pkgutil
import sys

# 반드시 설치본에서 와야 하는 모듈. 과거에 커밋에서 누락된 적이 있어 못박는다.
REQUIRED = [
    "stockbot.cli",
    "stockbot.config",
    "stockbot.pipeline",
    "stockbot.data.loader",
    "stockbot.backtest.engine",
    "stockbot.portfolio.risk_parity",
    "stockbot.execution.paper",
    "stockbot.validation.walkforward",
    "stockbot.data.kis_quotes",
    "stockbot.data.calendar",
    "stockbot.data.minute_store",
]


def main() -> int:
    try:
        import stockbot
    except ImportError as exc:
        print(f"실패: stockbot을 import할 수 없습니다 — {exc}", file=sys.stderr)
        print("  pip install . 이 성공했는지 확인하세요.", file=sys.stderr)
        return 1

    origin = pathlib.Path(stockbot.__file__).resolve()
    print(f"stockbot 위치: {origin}")

    # src/ 아래에서 왔다면 편집 설치(pip install -e)이거나 소스를 직접 보고 있다.
    # 둘 다 정상적인 개발 환경이지만, 패키지 설정이 깨져도 통과할 수 있는 조건이다.
    # 그래서 CI는 항상 비편집 설치(pip install .)로 이 검사를 돌린다.
    if origin.parent.parent.name == "src":
        print("  (저장소 src/에서 왔습니다 — 편집 설치이거나 소스 직접 참조)")
    else:
        print("  (site-packages 설치본 — clone한 사용자와 같은 조건)")

    found = {info.name for info in pkgutil.walk_packages(stockbot.__path__, "stockbot.")}

    missing = [name for name in REQUIRED if name not in found]
    if missing:
        print(
            "실패: 설치본에 다음 모듈이 없습니다 — 커밋이나 패키지 설정에서 빠졌습니다:\n  "
            + "\n  ".join(missing),
            file=sys.stderr,
        )
        return 1

    failed: list[str] = []
    for name in sorted(found):
        try:
            importlib.import_module(name)
        except Exception as exc:
            failed.append(f"{name}: {type(exc).__name__}: {exc}")

    if failed:
        print("실패: import 중 오류\n  " + "\n  ".join(failed), file=sys.stderr)
        return 1

    print(f"통과: {len(found)}개 하위 모듈 전부 import 성공")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
