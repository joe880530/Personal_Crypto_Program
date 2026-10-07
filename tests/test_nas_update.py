"""nas_update.sh 검증 — 가짜 git 저장소와 가짜 docker로.

bash와 git이 없는 환경(윈도우 등)에서는 건너뛴다.
"""
from __future__ import annotations
import os, shutil, subprocess, pathlib
import pytest

# 절대경로를 박으면 내 기계에서만 돈다. 실제로 CI에서 8건이 깨졌다.
SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
SCRIPT = SCRIPTS / "nas_update.sh"
RUN = SCRIPTS / "nas_run.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="bash 또는 git이 없는 환경에서는 건너뛴다",
)

FAKE_DOCKER = """#!/bin/bash
echo "CALL: $*" >> "$FAKE_LOG"
case "$1" in
  image) [ "${FAKE_IMAGE_MISSING:-0}" = "1" ] && exit 1 || exit 0 ;;
  build) exit "${FAKE_BUILD_STATUS:-0}" ;;
  run)   echo "점검 통과"; exit "${FAKE_RUN_STATUS:-0}" ;;
esac
exit 0
"""

def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

@pytest.fixture
def nas(tmp_path):
    origin = tmp_path / "origin"; origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "app.txt").write_text("v1\n")
    (origin / "Dockerfile").write_text("FROM scratch\n")
    git(origin, "add", "-A"); git(origin, "commit", "-qm", "v1")

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    (work / "scripts").mkdir()
    shutil.copy(SCRIPT, work / "scripts" / "nas_update.sh")
    shutil.copy(RUN, work / "scripts" / "nas_run.sh")
    (work / "config").mkdir(exist_ok=True)
    (work / "config" / "portfolio.yaml").write_text("dummy")
    (work / ".env").write_text("KIS_APP_KEY=x\n")

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    d = bin_dir / "docker"; d.write_text(FAKE_DOCKER); d.chmod(0o755)
    calls = tmp_path / "calls.log"; calls.touch()

    def run(*args, **env):
        e = dict(os.environ)
        e["PATH"] = f"{bin_dir}{os.pathsep}{e['PATH']}"
        e["FAKE_LOG"] = str(calls)
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run(["bash", str(work / "scripts" / "nas_update.sh"), *args],
                              cwd=str(work), env=e, capture_output=True, text=True)
    run.work, run.origin, run.calls = work, origin, calls
    return run


#: fetch 만 실패시키고 나머지는 진짜 git 에 넘기는 껍데기.
FAKE_GIT = """#!/bin/bash
if [ "$1" = "fetch" ] && [ "${FAKE_FETCH_FAIL:-0}" = "1" ]; then
    echo "Bad owner or permissions on /var/services/homes/wallabi/.ssh/config" >&2
    echo "fatal: Could not read from remote repository." >&2
    exit 128
fi
exec REAL_GIT "$@"
"""

#: root 가 아닌 계정으로 접속한 상황.
FAKE_ID = """#!/bin/bash
case "$1" in
  -u) echo 1026 ;;
  -un) echo wallabi ;;
  *) echo wallabi ;;
esac
"""


def shim(bin_dir, name, body):
    path = bin_dir / name
    path.write_text(body.replace("REAL_GIT", shutil.which(name) or f"/usr/bin/{name}"))
    path.chmod(0o755)


def test_a_failed_fetch_does_not_blame_the_deploy_key_outright(nas, tmp_path):
    """배포 키는 root 홈에 있다. 다른 계정이면 키가 멀쩡해도 같은 자리에서 막힌다.

    그때 '배포 키 절차를 따르라'고 안내하면 멀쩡한 키를 다시 만들게 된다.
    실제로 wallabi 계정으로 접속했다가 여기서 헤맸다.
    """
    shim(tmp_path / "bin", "git", FAKE_GIT)
    r = nas(FAKE_FETCH_FAIL=1)

    assert r.returncode == 1
    assert "받지 못했습니다" in r.stdout
    assert "Bad owner or permissions" in r.stdout, "원래 오류를 숨기면 안 됩니다"
    assert "인증이 안 돼 있으면" not in r.stdout, (
        "원인을 알아볼 수 있는 오류인데 뭉뚱그리면 엉뚱한 절차를 밟게 됩니다"
    )


def test_a_non_root_account_is_named_as_the_cause(nas, tmp_path):
    """무엇이 틀렸는지가 아니라 어떻게 고치는지를 알려줘야 한다."""
    shim(tmp_path / "bin", "git", FAKE_GIT)
    shim(tmp_path / "bin", "id", FAKE_ID)
    r = nas(FAKE_FETCH_FAIL=1)

    assert r.returncode == 1
    assert "wallabi" in r.stdout, "지금 누구로 접속했는지 보여줘야 합니다"
    assert "sudo -i" in r.stdout, "고치는 방법을 알려줘야 합니다"


def push_new_commit(origin):
    (origin / "app.txt").write_text("v2\n")
    git(origin, "add", "-A"); git(origin, "commit", "-qm", "v2")


def test_no_change_skips_the_rebuild(nas):
    r = nas()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "바뀐 것이 없습니다" in r.stdout
    assert "CALL: build" not in nas.calls.read_text()


def test_new_commit_triggers_rebuild_and_smoke_check(nas):
    push_new_commit(nas.origin)
    r = nas()
    assert r.returncode == 0, r.stdout + r.stderr
    calls = nas.calls.read_text()
    assert "CALL: build" in calls
    assert "CALL: run" in calls, "빌드만 하고 돌려보지 않으면 깨진 이미지를 못 잡습니다"
    assert (nas.work / "app.txt").read_text() == "v2\n"


def test_locally_overwritten_files_block_the_pull(nas):
    """File Station으로 덮어쓴 파일을 조용히 버리면 안 된다."""
    (nas.work / "app.txt").write_text("손으로 고침\n")
    push_new_commit(nas.origin)
    r = nas()
    assert r.returncode == 1
    assert "직접 고치거나 올린 파일" in r.stdout
    assert "--reset" in r.stdout
    assert (nas.work / "app.txt").read_text() == "손으로 고침\n", "버리면 안 됩니다"


def test_reset_discards_them_when_asked(nas):
    (nas.work / "app.txt").write_text("손으로 고침\n")
    push_new_commit(nas.origin)
    r = nas("--reset")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (nas.work / "app.txt").read_text() == "v2\n"


def test_untracked_secrets_are_left_alone(nas):
    """.env와 config/portfolio.yaml은 저장소에 없다. 건드리면 안 된다."""
    push_new_commit(nas.origin)
    assert nas().returncode == 0
    assert (nas.work / ".env").read_text() == "KIS_APP_KEY=x\n"
    assert (nas.work / "config" / "portfolio.yaml").read_text() == "dummy"


def test_failed_build_keeps_the_old_image(nas):
    push_new_commit(nas.origin)
    r = nas(FAKE_BUILD_STATUS=1)
    assert r.returncode == 1
    assert "이전 이미지가 그대로" in r.stdout
    assert "CALL: run" not in nas.calls.read_text(), "실패한 이미지를 돌려보면 안 됩니다"


def test_broken_new_image_is_reported(nas):
    """빌드가 됐다고 도는 것은 아니다."""
    push_new_commit(nas.origin)
    r = nas(FAKE_RUN_STATUS=1)
    assert r.returncode == 1
    assert "점검을 통과하지 못했습니다" in r.stdout


def test_force_rebuilds_without_changes(nas):
    assert nas("--force").returncode == 0
    assert "CALL: build" in nas.calls.read_text()


def test_reset_also_clears_untracked_files_that_block_the_pull(nas):
    """File Station으로 올린 파일은 지금 HEAD에서 '미추적'이다.

    checkout은 그것을 건드리지 못하고, 그대로 두면 pull이
    "untracked working tree files would be overwritten"으로 거부한다.
    실제로 NAS에서 그렇게 막혔다.
    """
    # 다음 커밋에서 추가될 파일을 미리 손으로 올려놓은 상황
    (nas.work / "newfile.txt").write_text("File Station으로 올린 사본\n")
    (nas.origin / "newfile.txt").write_text("v2에서 추가됨\n")
    push_new_commit(nas.origin)

    r = nas("--reset")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (nas.work / "newfile.txt").read_text() == "v2에서 추가됨\n"
    # 저장소에 없는 파일은 그대로 남아야 한다.
    assert (nas.work / ".env").read_text() == "KIS_APP_KEY=x\n"
