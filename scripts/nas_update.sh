#!/bin/bash
#
# NAS에서 최신 코드를 받아 이미지를 다시 만든다.
#
#   bash scripts/nas_update.sh            바뀐 게 있을 때만 다시 빌드
#   bash scripts/nas_update.sh --force    무조건 다시 빌드
#   bash scripts/nas_update.sh --reset    직접 올린 파일을 버리고 저장소 것으로
#
# **배포와 실행은 일부러 분리한다.** 주문 직전에 코드를 자동으로 받아오면,
# 검증하지 않은 커밋이 그날 주문을 내게 된다. 업데이트는 사람이 부를 때만 한다.
#
# 인증은 배포 키(읽기 전용 SSH 키)로 한다. README의 'NAS에서 직접 git pull' 참고.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT" || { echo "프로젝트 폴더로 이동하지 못했습니다: $ROOT"; exit 1; }

IMAGE="${STOCKBOT_IMAGE:-stockbot}"
FORCE=0
RESET=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --reset) RESET=1 ;;
        *) echo "모르는 옵션: $arg"; exit 1 ;;
    esac
done

DOCKER="$(command -v docker 2>/dev/null)"
for candidate in /usr/local/bin/docker /usr/bin/docker /usr/syno/bin/docker; do
    [ -n "$DOCKER" ] && break
    [ -x "$candidate" ] && DOCKER="$candidate"
done
[ -z "$DOCKER" ] && { echo "docker를 찾지 못했습니다. 작업 스케줄러의 사용자가 root인지 확인하세요."; exit 1; }

# ---------------------------------------------------------------- 받기
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "HEAD" ]; then
    echo "지금 어느 브랜치도 아닙니다(detached HEAD). 먼저 브랜치로 옮기세요:"
    echo "    git checkout <브랜치이름>"
    exit 1
fi

BEFORE="$(git rev-parse HEAD)"
echo "현재: ${BEFORE:0:7} ($BRANCH)"

# 실패 원인을 '인증 미설정'으로 단정하면 안 된다. 배포 키는 root 홈에 있으므로
# 다른 계정으로 접속하면 키가 멀쩡해도 같은 자리에서 실패한다. 그때 '배포 키
# 절차를 따르라'고 안내하면 멀쩡한 키를 다시 만들게 된다 (실제로 wallabi
# 계정으로 접속했다가 여기서 헤맸다).
FETCH_ERR="$(git fetch origin "$BRANCH" 2>&1)"
FETCH_STATUS=$?
[ -n "$FETCH_ERR" ] && printf '%s\n' "$FETCH_ERR" | sed 's/^/    /'
if [ $FETCH_STATUS -ne 0 ]; then
    echo
    case "$FETCH_ERR" in
        *"Bad owner or permissions"*|*"Permission denied"*|*"publickey"*)
            if [ "$(id -u)" -ne 0 ]; then
                echo "받지 못했습니다. 지금 사용자는 '$(id -un)' 인데, 배포 키는 root 홈에 있습니다."
                echo "  이렇게 바꾸세요:"
                echo "      sudo -i"
                echo "      cd $ROOT && bash scripts/$(basename "${BASH_SOURCE[0]}") $*"
                exit 1
            fi
            echo "받지 못했습니다. 배포 키 문제로 보입니다. README의 '배포 키' 절차를 확인하세요."
            exit 1
            ;;
    esac
    echo "받지 못했습니다. 인증이 안 돼 있으면 README의 '배포 키' 절차를 따르세요."
    exit 1
fi

if [ "$RESET" -eq 1 ]; then
    # reset을 쓰는 이유: File Station으로 올린 파일은 지금 HEAD에서 '미추적'이라
    # checkout이 건드리지 못하고, 그대로 두면 merge가
    # "untracked working tree files would be overwritten"으로 거부한다.
    # reset --hard는 대상 커밋에 있는 파일이면 미추적이어도 덮어쓴다.
    # .env, config/portfolio.yaml 처럼 저장소에 없는 파일은 그대로 남는다.
    echo "저장소 내용으로 되돌립니다 (직접 올린 파일은 덮어씁니다)."
    git reset --hard "origin/$BRANCH" || exit 1
elif ! git merge --ff-only "origin/$BRANCH" 2>&1 | sed 's/^/    /'; then
    echo
    echo "직접 고치거나 올린 파일이 있어 받지 못했습니다(위 목록)."
    echo "  그 파일들이 저장소 내용의 사본이라면 버려도 잃는 것이 없습니다:"
    echo "      bash scripts/nas_update.sh --reset"
    echo "  (.env 와 config/portfolio.yaml 은 저장소에 없으므로 남습니다)"
    exit 1
fi
AFTER="$(git rev-parse HEAD)"

if [ "$BEFORE" = "$AFTER" ] && [ "$FORCE" -eq 0 ] && "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "바뀐 것이 없습니다. 다시 빌드하지 않습니다 (--force로 강제)."
    exit 0
fi

# ---------------------------------------------------------------- 다시 빌드
echo
echo "이미지를 다시 만듭니다 (${BEFORE:0:7} -> ${AFTER:0:7})..."
if ! "$DOCKER" build -t "$IMAGE" . ; then
    echo
    echo "빌드에 실패했습니다. 이전 이미지가 그대로 남아 있으니 운용은 계속됩니다."
    exit 1
fi

# ---------------------------------------------------------------- 바로 확인
# 빌드가 됐다고 도는 것은 아니다. 주문과 무관한 점검을 한 번 돌려본다.
echo
echo "새 이미지 점검..."
if ! bash "$HERE/nas_run.sh" checkenv --offline; then
    echo
    echo "새 이미지가 점검을 통과하지 못했습니다. 위 메시지를 확인하세요."
    exit 1
fi

echo
echo "업데이트 완료: ${AFTER:0:7}"
git log --oneline "${BEFORE}..${AFTER}" 2>/dev/null | sed 's/^/    /'
