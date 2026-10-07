"""NAS 작업 스케줄러가 부르는 실행 스크립트 검증.

이 스크립트는 사람이 안 보는 상태에서 도는 유일한 진입점이다. 여기가 조용히
망가지면 "몇 달째 리밸런싱이 안 됐다"를 나중에야 알게 된다. docker는 가짜로
갈아끼우고 스크립트의 판단만 본다.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "nas_run.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash가 없는 환경(예: 윈도우)에서는 건너뛴다"
)

FAKE_DOCKER = """#!/bin/bash
echo "CALL: $*" >> "$FAKE_LOG"
case "$1" in
  image)
    if [ "${FAKE_DOCKER_DENIED:-0}" = "1" ]; then
      echo "permission denied while trying to connect to the Docker daemon socket" >&2
      exit 1
    fi
    [ "${FAKE_IMAGE_MISSING:-0}" = "1" ] && exit 1 || exit 0 ;;
  run)
    echo "진행 중인 줄"
    # 아직 안 끝났는데 로그에 벌써 있는가? 이 시점의 로그를 그대로 떠 둔다.
    if [ -n "${FAKE_RUN_PEEK:-}" ]; then
      sleep 0.3
      cp "${FAKE_RUN_LOG}" "${FAKE_RUN_PEEK}" 2>/dev/null || touch "${FAKE_RUN_PEEK}"
    fi
    echo "브로커: kis-모의투자 (실계좌=False)"
    exit "${FAKE_RUN_STATUS:-0}" ;;
esac
exit 0
"""


@pytest.fixture
def nas(tmp_path):
    """가짜 docker와 최소 구성을 갖춘 프로젝트 폴더."""
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    (root / "config").mkdir()
    shutil.copy(SCRIPT, root / "scripts" / "nas_run.sh")
    (root / "config" / "portfolio.yaml").write_text("dummy", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)

    calls = tmp_path / "calls.log"
    calls.touch()

    def run(*args, cwd=None, **env):
        environ = dict(os.environ)
        environ["PATH"] = f"{bin_dir}{os.pathsep}{environ['PATH']}"
        environ["FAKE_LOG"] = str(calls)
        environ.update({k: str(v) for k, v in env.items()})
        return subprocess.run(
            ["bash", str(root / "scripts" / "nas_run.sh"), *args],
            cwd=str(cwd or root), env=environ, capture_output=True, text=True,
        )

    run.root = root
    run.calls = calls
    return run


def test_missing_env_file_stops_before_running_anything(nas):
    """키가 없는데 컨테이너를 띄우면 KIS 오류로 에둘러 실패한다."""
    result = nas("account")
    assert result.returncode == 1
    assert ".env 가 없습니다" in result.stdout
    # image inspect는 읽기만 하므로 불려도 된다. 컨테이너를 띄우지 않는 것이 핵심이다.
    assert "CALL: run" not in nas.calls.read_text(), "컨테이너를 띄우지 말았어야 합니다"


def test_missing_image_tells_how_to_build(nas):
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    result = nas("account", FAKE_IMAGE_MISSING=1)
    assert result.returncode == 1
    assert "docker build" in result.stdout, "무엇을 해야 하는지 알려줘야 합니다"


def test_default_command_does_not_submit_orders(nas):
    """인자를 빠뜨린 스케줄러 항목이 실주문을 내면 안 된다."""
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    result = nas()
    assert result.returncode == 0
    call = nas.calls.read_text()
    assert "stockbot trade -c config/portfolio.yaml" in call
    assert "--execute" not in call


def test_explicit_config_is_not_duplicated(nas):
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    nas("checkenv", "--offline", "-c", "config/other.yaml")
    call = nas.calls.read_text()
    assert call.count(" -c ") == 1, f"설정이 두 번 붙었습니다: {call}"
    assert "config/other.yaml" in call


def test_failure_exit_code_reaches_the_scheduler(nas):
    """DSM은 종료코드로 실패를 판단한다. 삼키면 알림이 안 온다."""
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    result = nas("account", FAKE_RUN_STATUS=3)
    assert result.returncode == 3
    assert "[실패]" in result.stdout


def test_runs_from_any_working_directory(nas, tmp_path):
    """작업 스케줄러는 작업 디렉터리를 보장하지 않는다(대개 /)."""
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    result = nas("account", cwd=elsewhere)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "stockbot account" in nas.calls.read_text()


def test_every_run_is_recorded(nas):
    """메일은 요약만 보낸다. 나중에 따질 수 있으려면 전체가 파일에 남아야 한다."""
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    nas("account")
    nas("trade")
    log = (nas.root / "logs" / "stockbot.log").read_text(encoding="utf-8")
    assert "stockbot account" in log
    assert "stockbot trade" in log
    assert log.count("종료코드 0") == 2


def test_no_permission_for_docker_is_not_reported_as_a_missing_image(nas):
    """둘 다 똑같이 실패하지만 해야 할 일이 다르다.

    wallabi 계정으로 접속했다가 "이미지를 먼저 만드세요"라는 안내를 받고
    헤맸다. 그대로 빌드해봐야 같은 이유로 실패한다. 틀린 안내는 없느니만 못하다.
    """
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    result = nas("account", FAKE_DOCKER_DENIED=1)

    assert result.returncode != 0
    out = result.stdout
    assert "root" in out, out
    assert "sudo -i" in out, "어떻게 고치는지 알려줘야 합니다"
    assert "이미지" not in out, "권한 문제인데 이미지 이야기를 하면 엉뚱한 곳을 보게 됩니다"


def test_a_genuinely_missing_image_still_says_so(nas):
    """권한은 멀쩡한데 이미지가 없는 경우까지 root 이야기를 하면 안 된다."""
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    result = nas("account", FAKE_IMAGE_MISSING=1)

    assert result.returncode != 0
    assert "이미지" in result.stdout
    assert "sudo -i" not in result.stdout


def test_output_is_logged_while_it_runs(nas):
    """끝나고 한 번에 쓰면, 도중에 끊긴 실행은 로그에 아무것도 남기지 못한다.

    SSH가 끊겨 20분짜리 백필이 18분에 죽은 일이 있다. 그때 그 18분치 기록이
    통째로 사라졌다. 살아 있는 동안 로그에 쌓여 있어야 `tail -f`로 따라가고,
    끊긴 뒤에도 어디까지 갔는지 볼 수 있다.
    """
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    log = nas.root / "logs" / "stockbot.log"
    # 컨테이너가 아직 도는 중에 로그를 들여다본다.
    peek = nas.root / "peek.txt"
    result = nas("collect", FAKE_RUN_PEEK=str(peek), FAKE_RUN_LOG=str(log))

    assert result.returncode == 0, result.stderr
    assert peek.exists(), "가짜 docker가 로그를 들여다보지 못했습니다"
    assert "진행 중인 줄" in peek.read_text(encoding="utf-8"), (
        "컨테이너가 도는 동안에는 로그가 비어 있었습니다 — 모았다가 끝에 쓰고 있습니다"
    )


def test_every_run_is_summarised_only_once(nas):
    """실시간 기록과 끝 요약을 둘 다 하다 보면 같은 줄을 두 번 쓰기 쉽다."""
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    nas("account")
    log = (nas.root / "logs" / "stockbot.log").read_text(encoding="utf-8")
    assert log.count("브로커: kis-모의투자") == 1, log


def test_every_written_directory_is_mounted(nas):
    """마운트를 빠뜨리면 --rm 컨테이너가 끝날 때 쓴 것이 통째로 사라진다.

    실제로 분봉 저장 폴더를 빠뜨려, 1년치 백필이 아무것도 남기지 않을 뻔했다.
    컨테이너 안에서 프로그램이 **쓰는** 경로는 전부 바깥과 이어져 있어야 한다.
    """
    (nas.root / ".env").write_text("KIS_APP_KEY=x\n", encoding="utf-8")
    nas("account")
    call = nas.calls.read_text()

    for path in ("/app/.state", "/app/data/cache", "/app/data/minutes"):
        assert f":{path}" in call, f"{path} 가 마운트되지 않았습니다"
    assert "/app/config:ro" in call, "설정은 읽기 전용으로 붙여야 합니다"


def test_dockerfile_declares_the_same_writable_paths():
    """Dockerfile과 실행 스크립트가 어긋나면 한쪽만 고치는 일이 생긴다."""
    dockerfile = (SCRIPT.parent.parent / "Dockerfile").read_text(encoding="utf-8")
    for path in ("/app/.state", "/app/data/cache", "/app/data/minutes"):
        assert path in dockerfile, f"Dockerfile에 {path} 선언이 없습니다"
