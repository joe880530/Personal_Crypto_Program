"""학습/검증 구간 분할.

백테스트로 전략을 고르는 순간, 그 백테스트 성과는 더 이상 성과가 아니라
**선택의 근거**가 된다. 같은 데이터로 고르고 같은 데이터로 평가하면 항상
좋아 보인다. 고른 구간과 평가하는 구간을 분리해야 실력과 운이 갈린다.
"""

from __future__ import annotations

import dataclasses

import pandas as pd


@dataclasses.dataclass(frozen=True)
class Window:
    """하나의 학습/검증 구간.

    Attributes:
        train_start / train_end: 전략을 고르는 데 쓰는 구간의 위치 색인(끝은 미포함).
        test_start / test_end: 고른 전략을 평가하는 구간. 학습 구간 바로 뒤에 온다.
    """

    train_start: int
    train_end: int
    test_start: int
    test_end: int

    @property
    def train_length(self) -> int:
        return self.train_end - self.train_start

    @property
    def test_length(self) -> int:
        return self.test_end - self.test_start

    def label(self, index: pd.DatetimeIndex) -> str:
        """사람이 읽을 구간 표시."""
        return (
            f"{index[self.train_start]:%Y-%m} ~ {index[self.train_end - 1]:%Y-%m}"
            f" -> {index[self.test_start]:%Y-%m} ~ {index[self.test_end - 1]:%Y-%m}"
        )

    def test_dates(self, index: pd.DatetimeIndex) -> tuple[pd.Timestamp, pd.Timestamp]:
        return index[self.test_start], index[self.test_end - 1]


def make_windows(
    n_bars: int,
    train: int,
    test: int,
    step: int | None = None,
    mode: str = "rolling",
) -> list[Window]:
    """워크포워드 구간을 만든다.

    Args:
        n_bars: 전체 봉 개수.
        train: 학습 구간 길이(봉).
        test: 검증 구간 길이(봉).
        step: 다음 구간까지 이동할 간격. None이면 test와 같게 두어 검증 구간이
            겹치지 않는다. 겹치면 같은 기간을 여러 번 세게 되어 성과가 부풀려진다.
        mode: ``"rolling"``은 학습 구간 길이를 고정하고 창을 밀고,
            ``"anchored"``는 시작점을 고정한 채 학습 구간을 늘린다.
            시장 구조가 변했다고 보면 rolling, 데이터가 많을수록 낫다고 보면 anchored.

    Returns:
        시간순 Window 목록. 구간을 하나도 만들 수 없으면 빈 목록.
    """
    if mode not in {"rolling", "anchored"}:
        raise ValueError("mode는 'rolling' 또는 'anchored'여야 합니다")
    if train <= 0 or test <= 0:
        raise ValueError("train과 test는 양수여야 합니다")
    step = test if step is None else step
    if step <= 0:
        raise ValueError("step은 양수여야 합니다")

    windows: list[Window] = []
    train_end = train
    while train_end + test <= n_bars:
        train_start = 0 if mode == "anchored" else train_end - train
        windows.append(
            Window(
                train_start=train_start,
                train_end=train_end,
                test_start=train_end,
                test_end=train_end + test,
            )
        )
        train_end += step
    return windows


def describe_coverage(windows: list[Window], index: pd.DatetimeIndex) -> str:
    """검증 구간이 전체 기간 중 어디를 덮는지 요약한다."""
    if not windows:
        return "검증 구간 없음"
    first = index[windows[0].test_start]
    last = index[windows[-1].test_end - 1]
    covered = sum(w.test_length for w in windows)
    return (
        f"{len(windows)}개 구간, 검증 대상 {first:%Y-%m-%d} ~ {last:%Y-%m-%d}"
        f" ({covered}봉 / 전체 {len(index)}봉)"
    )
