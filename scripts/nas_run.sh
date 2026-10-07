#!/bin/bash
#
# 시놀로지 NAS의 작업 스케줄러가 부르는 실행 스크립트.
#
#   제어판 → 작업 스케줄러 → 생성 → 예약된 작업 → 사용자 정의 스크립트
#   사용자: root   (docker 명령에 권한이 필요하다)
#   명령:   bash /volume1/docker/stockbot/scripts/nas_run.sh trade
#
# 인자는 그대로 stockbot CLI에 넘어간다. 인자를 안 주면 trade(모의 출력)만 한다.
#   nas_run.sh checkenv --offline    설정만 점검
#   nas_run.sh account               잔고 조회
#   nas_run.sh trade                 주문 계획만 (제출 안 함)
#   nas_run.sh trade --execute       실제 주문 제출
#   nas_run.sh collect --start ...   분봉 수집
#
# 오래 걸리는 작업(분봉 1년치 백필은 20분)은 SSH 창에 매달아 두지 말 것.
# 창이 끊기면 작업도 같이 죽는다. 떼어놓고 돌린 뒤 로그로 따라가면 된다:
#   nohup bash scripts/nas_run.sh collect --start 2025-10-01 >/dev/null 2>&1 &
#   tail -f logs/stockbot.log
#
# 표준출력은 짧게, 전체 기록은 로그 파일에 남긴다. DSM 작업 스케줄러의
# '실행 세부 정보를 이메일로 보내기'가 표준출력을 그대로 메일로 보내기 때문에,
# 여기에 수백 줄을 쏟으면 정작 읽어야 할 줄이 묻힌다.
#
# 실패하면 0이 아닌 값으로 끝난다. DSM에서 '비정상 종료 시에만 보내기'를
# 켜두면 평소엔 조용하고 문제가 생겼을 때만 메일이 온다.

set -uo pipefail

# ---------------------------------------------------------------- 경로
# 이 스크립트가 있는 곳의 상위가 프로젝트 루트다. 스케줄러는 작업 디렉터리를
# 보장하지 않으므로(대개 /), 상대경로에 기대면 안 된다.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT" || { echo "프로젝트 폴더로 이동하지 못했습니다: $ROOT"; exit 1; }

IMAGE="${STOCKBOT_IMAGE:-stockbot}"
CONFIG="${STOCKBOT_CONFIG:-config/portfolio.yaml}"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/stockbot.log"
MAX_LOG_LINES="${STOCKBOT_MAX_LOG_LINES:-20000}"
SUMMARY_LINES="${STOCKBOT_SUMMARY_LINES:-40}"

# 컨테이너 안의 사용자 uid. Dockerfile과 맞춰야 한다.
CONTAINER_UID=1000

# ---------------------------------------------------------------- docker 찾기
# 스케줄러가 주는 PATH는 로그인 셸보다 빈약해서 docker를 못 찾는 경우가 있다.
DOCKER="$(command -v docker 2>/dev/null)"
for candidate in /usr/local/bin/docker /usr/bin/docker /usr/syno/bin/docker; do
    [ -n "$DOCKER" ] && break
    [ -x "$candidate" ] && DOCKER="$candidate"
done
if [ -z "$DOCKER" ]; then
    echo "docker 명령을 찾지 못했습니다."
    echo "  패키지 센터에서 Container Manager가 설치돼 있는지,"
    echo "  작업 스케줄러의 '사용자'가 root인지 확인하세요."
    exit 1
fi

# ---------------------------------------------------------------- 사전 점검
# 실패를 '이미지가 없다'로 뭉뚱그리면 안 된다. docker 소켓에 접근할 권한이
# 없을 때도 똑같이 실패하는데, 그때 "먼저 만드세요"라고 안내하면 빌드도 같은
# 이유로 실패한다. 틀린 안내는 없느니만 못하다 (실제로 wallabi 계정으로
# 접속했다가 여기서 헤맸다).
if ! DOCKER_ERR="$("$DOCKER" image inspect "$IMAGE" 2>&1 >/dev/null)"; then
    case "$DOCKER_ERR" in
        *"permission denied"*|*"Cannot connect to the Docker daemon"*|*"dial unix"*)
            echo "docker 를 쓸 권한이 없습니다. root 로 실행해야 합니다."
            echo "  지금 사용자: $(id -un)"
            echo "  이렇게 바꾸세요:"
            echo "      sudo -i"
            echo "      cd $ROOT && bash scripts/$(basename "${BASH_SOURCE[0]}") $*"
            echo "  작업 스케줄러에 등록할 때도 '사용자'를 root 로 두어야 합니다."
            echo
            echo "  원래 메시지: $DOCKER_ERR"
            exit 1
            ;;
    esac
    echo "이미지 '$IMAGE' 가 없습니다. 먼저 한 번 만들어야 합니다:"
    echo "  cd $ROOT && $DOCKER build -t $IMAGE ."
    exit 1
fi
if [ ! -f "$ROOT/.env" ]; then
    echo ".env 가 없습니다: $ROOT/.env"
    echo "  .env.example 을 복사해 KIS 키를 채우세요."
    exit 1
fi
if [ ! -f "$ROOT/$CONFIG" ]; then
    echo "설정 파일이 없습니다: $ROOT/$CONFIG"
    exit 1
fi

# 컨테이너는 uid 1000으로 돈다. NAS에서 만든 폴더는 root 소유라 그대로 두면
# 토큰 캐시와 시세 캐시를 쓰지 못한다. 읽기 실패가 아니라 쓰기 실패라
# 증상이 '매번 토큰 재발급'처럼 엉뚱하게 나타난다.
mkdir -p "$ROOT/.state" "$ROOT/data/cache" "$ROOT/data/minutes" "$LOG_DIR"
chown -R "$CONTAINER_UID:$CONTAINER_UID" \
    "$ROOT/.state" "$ROOT/data/cache" "$ROOT/data/minutes" 2>/dev/null || true

# ---------------------------------------------------------------- 실행
ARGS=("$@")
[ ${#ARGS[@]} -eq 0 ] && ARGS=(trade)
# -c 를 직접 주지 않았으면 기본 설정을 붙인다.
case " ${ARGS[*]} " in
    *" -c "*|*" --config "*) ;;
    *) ARGS+=(-c "$CONFIG") ;;
esac

STARTED="$(date '+%Y-%m-%d %H:%M:%S')"
{
    echo
    echo "=================================================================="
    echo "[$STARTED] stockbot ${ARGS[*]}"
    echo "=================================================================="
} >> "$LOG"

# 출력은 **나오는 즉시** 로그에 쓴다. 모았다가 끝에 한 번에 쓰면, 도중에 끊긴
# 실행(SSH가 끊기거나 Ctrl+C)은 로그에 아무것도 남기지 못한다. 20분짜리 백필이
# 18분에 끊기면 그 18분치 기록이 통째로 사라진다는 뜻이다.
# 덕분에 백그라운드로 돌려놓고 `tail -f logs/stockbot.log`로 따라갈 수도 있다:
#     nohup bash scripts/nas_run.sh collect --start 2025-10-01 >/dev/null 2>&1 &
#     tail -f logs/stockbot.log
# 사람이 직접 돌릴 때는 화면에도 같이 흘린다. 분봉 백필처럼 오래 걸리는 작업에서
# 끝까지 빈 화면이면 돌고 있는지 알 수가 없다(실제로 그랬다).
# 스케줄러가 부를 때는 터미널이 아니므로 예전처럼 끝에 요약만 낸다.
run_container() {
    "$DOCKER" run --rm \
        --env-file "$ROOT/.env" \
        -v "$ROOT/config:/app/config:ro" \
        -v "$ROOT/.state:/app/.state" \
        -v "$ROOT/data/cache:/app/data/cache" \
        -v "$ROOT/data/minutes:/app/data/minutes" \
        "$IMAGE" "${ARGS[@]}" 2>&1
}

# 이번 실행분만 요약하려면 어디서부터인지 알아야 한다.
BEFORE="$(wc -l < "$LOG")"
STREAMED=0
if [ -t 1 ]; then
    STREAMED=1
    run_container | tee -a "$LOG"
    STATUS=${PIPESTATUS[0]}
else
    run_container >> "$LOG"
    STATUS=$?
fi
OUTPUT="$(tail -n "+$((BEFORE + 1))" "$LOG")"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 종료코드 $STATUS" >> "$LOG"

# 로그가 무한정 자라지 않게 뒤쪽만 남긴다.
if [ "$(wc -l < "$LOG")" -gt "$MAX_LOG_LINES" ]; then
    tail -n "$MAX_LOG_LINES" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# ---------------------------------------------------------------- 요약 출력
if [ $STATUS -eq 0 ]; then
    echo "[성공] stockbot ${ARGS[*]}  ($STARTED)"
else
    echo "[실패] stockbot ${ARGS[*]}  ($STARTED)  종료코드=$STATUS"
fi
echo "전체 기록: $LOG"
if [ "$STREAMED" -eq 0 ]; then
    echo "------------------------------------------------------------------"
    # 실패했으면 전부, 성공했으면 뒷부분만. 실패 원인은 앞쪽에 있을 수 있다.
    if [ $STATUS -eq 0 ]; then
        echo "$OUTPUT" | tail -n "$SUMMARY_LINES"
    else
        echo "$OUTPUT"
    fi
fi

exit $STATUS
