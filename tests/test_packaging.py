"""저장소 위생 검사.

여기 있는 테스트는 코드의 동작이 아니라 **배포 가능성**을 지킨다.
소스 파일 하나가 .gitignore에 걸려 커밋에서 빠지면, 로컬 테스트는 전부
통과하는데 clone한 사람은 import조차 못 하는 상황이 된다. 실제로 한 번
겪었기 때문에 테스트로 고정한다.
"""

from __future__ import annotations

import importlib
import pathlib
import pkgutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


def _git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_available() -> bool:
    return (REPO_ROOT / ".git").exists() and _git("rev-parse", "--git-dir").returncode == 0


requires_git = pytest.mark.skipif(not _git_available(), reason="git 저장소가 아닙니다")


@requires_git
def test_no_source_file_is_git_ignored():
    """src/ 아래 파이썬 파일이 .gitignore에 걸리면 안 된다.

    'data/' 처럼 앞에 '/'가 없는 규칙은 하위 경로 어디에나 매칭되므로
    src/stockbot/data/ 같은 패키지 디렉터리까지 제외해버린다.
    """
    sources = sorted(str(p.relative_to(REPO_ROOT)) for p in SRC.rglob("*.py"))
    assert sources, "src/ 아래에 파이썬 파일이 없습니다"

    result = _git("check-ignore", "--stdin", stdin="\n".join(sources))
    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, (
        "다음 소스 파일이 .gitignore에 걸려 커밋되지 않습니다:\n  "
        + "\n  ".join(ignored)
        + "\n.gitignore의 디렉터리 규칙은 '/data/' 처럼 루트에 고정하세요."
    )


@requires_git
def test_every_source_file_is_tracked():
    """작업 트리의 소스 파일이 전부 git에 들어 있어야 한다."""
    untracked = _git(
        "ls-files", "--others", "--exclude-standard", "--", "src", "scripts", "tests"
    ).stdout.split()
    python_files = [f for f in untracked if f.endswith((".py", ".yaml", ".yml"))]
    assert not python_files, f"추적되지 않는 소스 파일: {python_files}"


def test_every_package_directory_has_init():
    """하위 패키지에 __init__.py가 빠지면 설치 후 import가 깨진다."""
    missing = [
        str(d.relative_to(REPO_ROOT))
        for d in SRC.rglob("*")
        if d.is_dir()
        and d.name != "__pycache__"
        and any(f.suffix == ".py" for f in d.iterdir())
        and not (d / "__init__.py").exists()
    ]
    assert not missing, f"__init__.py가 없는 패키지 디렉터리: {missing}"


def test_all_submodules_import_cleanly():
    """stockbot의 모든 하위 모듈이 import 가능해야 한다."""
    import stockbot

    failed: list[str] = []
    for info in pkgutil.walk_packages(stockbot.__path__, prefix="stockbot."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # pragma: no cover - 실패 시에만 실행
            failed.append(f"{info.name}: {type(exc).__name__}: {exc}")
    assert not failed, "import 실패:\n  " + "\n  ".join(failed)


def test_declared_packages_cover_every_subpackage():
    """pyproject의 패키지 탐색 설정이 모든 하위 패키지를 잡아야 한다."""
    expected = {
        str(d.relative_to(SRC)).replace("/", ".")
        for d in SRC.rglob("*")
        if d.is_dir() and (d / "__init__.py").exists()
    }
    assert "stockbot.data" in expected  # 과거에 빠졌던 패키지

    try:
        from setuptools import find_packages
    except ImportError:  # pragma: no cover - setuptools 없는 환경
        pytest.skip("setuptools가 없습니다")

    found = set(find_packages(where=str(SRC), include=["stockbot*"]))
    assert expected <= found, f"탐색에서 누락된 패키지: {sorted(expected - found)}"


def test_shell_scripts_use_unix_line_endings():
    """셸 스크립트에 CRLF가 섞이면 리눅스에서 통째로 못 돈다.

    윈도우를 거쳐 NAS로 옮겼을 때 bash가 \r을 명령의 일부로 읽어
    "$'\\r': command not found"로 죽었다. .gitattributes로 막고 있지만,
    누군가 그 설정을 지우거나 파일을 잘못 커밋하면 조용히 되돌아간다.
    """
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parent.parent
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.sh", "Dockerfile"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.split("\0")

    offenders = []
    for name in filter(None, tracked):
        data = (root / name).read_bytes()
        if b"\r\n" in data:
            offenders.append(name)
    assert not offenders, f"CRLF가 섞인 파일: {offenders}"


def test_no_absolute_paths_from_a_developer_machine():
    """내 기계의 절대경로를 박으면 다른 곳에서 전부 깨진다.

    실제로 테스트 8건이 CI에서만 깨졌다. 로컬은 통과했으므로 푸시 전에는
    알 수 없었다. 경로는 __file__ 기준으로 잡는다.
    """
    import pathlib
    import re
    import subprocess

    root = pathlib.Path(__file__).resolve().parent.parent
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.py", "*.sh", "*.yml", "*.yaml"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.split("\0")

    # 개발자 홈 디렉터리 형태의 절대경로. /usr, /opt 같은 시스템 경로는 정상이다.
    suspicious = re.compile(r"[\"']/(home|Users|root)/[^\"'\s]+")
    offenders = []
    for name in filter(None, tracked):
        for number, line in enumerate(
            (root / name).read_text(encoding="utf-8").splitlines(), 1
        ):
            if suspicious.search(line):
                offenders.append(f"{name}:{number}")
    assert not offenders, f"내 기계 경로가 박혀 있습니다: {offenders}"
