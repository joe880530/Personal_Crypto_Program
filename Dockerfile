# NAS나 서버에서 무인 운영하기 위한 이미지.
#
# 왜 컨테이너인가: NAS에 파이썬을 직접 깔면 DSM 업데이트 때 날아가거나
# 다른 패키지와 충돌한다. 컨테이너는 의존성을 통째로 들고 다니므로
# NAS를 갈아타도 그대로 돈다.
#
# 만들기:  docker build -t stockbot .
# 쓰기:    docker run --rm --env-file .env \
#            -v "$PWD/config:/app/config:ro" \
#            -v "$PWD/.state:/app/.state" \
#            -v "$PWD/data/cache:/app/data/cache" \
#            stockbot account -c config/portfolio.yaml
#
# 주의: .env를 이미지에 넣지 않는다. --env-file로 실행할 때만 넘긴다.
#       이미지에 구우면 이미지를 가진 사람은 누구나 계좌에 접근할 수 있다.

FROM python:3.12-slim

# 국내 시세는 finance-datareader로 받는다. 미국 종목을 쓸 거면 [kr,us]로 바꾼다.
# 시간대를 한국으로 고정한다. 리밸런싱 날짜와 장 운영시간 판단이 여기에 걸린다.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul

WORKDIR /app

# 의존성 먼저 설치해 레이어를 재사용한다(코드만 바뀌면 다시 안 받는다).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[kr]"

# 루트로 돌리지 않는다. 컨테이너가 뚫려도 NAS 파일을 헤집지 못하게.
RUN useradd --create-home --uid 1000 stockbot \
 && mkdir -p /app/.state /app/data/cache /app/data/minutes \
 && chown -R stockbot:stockbot /app
USER stockbot

# 설정은 읽기 전용으로, 상태·캐시·분봉은 쓰기 가능하게 마운트한다.
# 마운트를 빠뜨리면 --rm으로 도는 컨테이너가 끝날 때 쓴 것이 통째로 사라진다.
VOLUME ["/app/config", "/app/.state", "/app/data/cache", "/app/data/minutes"]

ENTRYPOINT ["stockbot"]
CMD ["--help"]
