# Personal Stock Program

[![CI](https://github.com/joe880530/Personal_Stock_Program/actions/workflows/ci.yml/badge.svg)](https://github.com/joe880530/Personal_Stock_Program/actions/workflows/ci.yml)

포트폴리오 기반 개인 주식 투자 자동화 프로그램.

목표 비중을 정하는 **전략**, 그 비중이 과거에 통했는지 확인하는 **백테스트**,
현재 보유와 목표의 차이를 주문으로 바꾸는 **리밸런서**, 그리고 주문을 내보내는
**브로커 계층**으로 나뉩니다.

> **경고**: 이 프로그램은 투자 조언이 아닙니다. 백테스트 성과는 미래를 보장하지
> 않습니다. 실제 돈을 넣기 전에 반드시 모의계좌로 충분히 검증하세요.

## 현재 상태

| 단계 | 기능 | 상태 |
|---|---|---|
| 1 | 데이터 수집(미국/국내/환율) + 캐시 | 완료 |
| 2 | 포트폴리오 전략 (고정·균등·리스크패리티·듀얼모멘텀·결합) | 완료 |
| 3 | 백테스트 (수수료·세금·슬리피지·밴드 리밸런싱) | 완료 |
| 4 | 주문 생성 + 안전장치 + 모의 브로커 | 완료 |
| 5 | 워크포워드 검증 (과최적화 측정) | 완료 |
| 6 | 한국투자증권(KIS) 브로커 — 모의/실계좌 | 구현, **실통신 미검증** |
| 7 | Alpaca(미국) 브로커 | **미구현** |
| 8 | 스케줄러 무인 운영 + 알림 | **미구현** |

6단계는 로직만 테스트했습니다. 실제 KIS 서버와의 통신은 API 키가 있어야 하므로
사용자가 모의투자로 먼저 확인해야 합니다. 신호 → 모의 → 실계좌 순서를 건너뛰면
버그의 대가를 돈으로 치르게 됩니다.

## 설치

먼저 저장소를 받아서 그 폴더 안으로 들어가야 합니다. 아래 명령은 모두
**프로젝트 루트**(`pyproject.toml`이 보이는 위치)에서 실행합니다.

```bash
git clone https://github.com/joe880530/Personal_Stock_Program.git
cd Personal_Stock_Program
pip install -e .
```

`pip install -e .`가 `pyproject.toml을 찾을 수 없다`고 하면 다른 폴더에 있는 것입니다.
`cd`로 프로젝트 루트에 들어왔는지 확인하세요.

### 명령 실행 방법

설치하면 `stockbot` 명령이 생기지만, PATH에 잡히지 않는 환경(특히 Windows의
`--user` 설치)이 있습니다. **어디서든 확실한 쪽은 모듈 실행입니다.**

```bash
python -m stockbot.cli backtest -c config/portfolio.yaml   # 항상 동작
stockbot backtest -c config/portfolio.yaml                 # PATH가 잡혔을 때
```

### Windows 사용자

명령 프롬프트(cmd)는 `cp`가 없고 경로 구분자가 `\`입니다.

```cmd
git clone https://github.com/joe880530/Personal_Stock_Program.git
cd Personal_Stock_Program
pip install -e ".[us,kr]"
copy config\portfolio.example.yaml config\portfolio.yaml
python -m stockbot.cli backtest -c config\portfolio.yaml
```

PowerShell이나 Git Bash를 쓰면 `cp`와 `/`를 그대로 쓸 수 있습니다.

## 빠른 시작 (네트워크 불필요)

실제 데이터 없이 파이프라인이 도는지 먼저 확인해보는 경로입니다.

```bash
python scripts/make_sample_data.py --out data/csv

python -m stockbot.cli backtest -c config/portfolio.sample.yaml   # 과거 성과
python -m stockbot.cli compare  -c config/portfolio.sample.yaml   # 전략별 비교
python -m stockbot.cli signal   -c config/portfolio.sample.yaml   # 지금 사야 할 비중
python -m stockbot.cli trade    -c config/portfolio.sample.yaml   # 주문 계획 (모의 출력)
python -m stockbot.cli walkforward -c config/portfolio.sample.yaml --compare-full
```

샘플 데이터는 **합성 난수**입니다. 파이프라인 확인용이며 성과 수치에 의미가 없습니다.

> 설정 파일 안의 `data.path`, `execution.state_path` 같은 상대 경로는 **현재 작업
> 디렉터리 기준**으로 해석됩니다. 다른 폴더에서 실행하면 데이터를 못 찾습니다.
> 그럴 때 프로그램이 어느 경로를 찾아봤는지 출력하니 메시지를 보세요.

## 실제 데이터로 쓰기

```bash
pip install -e ".[us,kr]"     # yfinance + finance-datareader
cp config/portfolio.example.yaml config/portfolio.yaml
# config/portfolio.yaml 에서 assets / strategy / risk 를 본인 상황에 맞게 수정
python -m stockbot.cli backtest -c config/portfolio.yaml --out reports/out
```

`config/portfolio.yaml`은 `.gitignore`에 있습니다. API 키와 계좌 정보가
저장소에 올라가지 않게 하려는 의도이니 유지하세요.

## 설정 한눈에 보기

```yaml
base_currency: KRW          # 국내+미국 혼합이면 환율로 통일해 평가

assets:
  - {ticker: SPY,      market: US,     currency: USD, name: "미국 S&P500"}
  - {ticker: "069500", market: KR_ETF, currency: KRW, name: "KODEX 200"}

strategy:
  type: equal               # 균등 비중 (워크포워드 결과에 따른 기본값 — 아래 참고)

backtest:
  rebalance: M              # 월 1회
  band: 0.03                # 3%p 이상 벌어질 때만 거래
  execution: next_open      # 신호 다음 봉 시가 체결

risk:
  max_position_weight: 0.40
  max_drawdown_stop: 0.25   # 고점 대비 -25%면 신규 매수 중단
```

전체 옵션은 `config/portfolio.example.yaml`에 주석과 함께 있습니다.

## 전략

| type | 설명 |
|---|---|
| `fixed` | 정해진 비중 유지. **모든 비교의 기준선으로 먼저 돌려보세요.** |
| `equal` | 균등 비중 |
| `risk_parity` | 위험 기여도를 균등하게. `erc`(상관관계 반영) 또는 `inverse_vol` |
| `dual_momentum` | 상대 모멘텀으로 고르고, 절대 모멘텀으로 하락장에 대피 |
| `momentum_risk_parity` | 위 둘의 결합. 무엇을 살지는 모멘텀, 얼마나 살지는 변동성 |

**리스크 패리티**는 금액이 아니라 위험을 균등 배분합니다. 주식 60 / 채권 40은
금액은 6:4지만 위험 기여도는 대략 9:1입니다. ERC는 Spinu(2013)의 볼록 문제를
순환 좌표하강법으로 풀어 scipy 없이 정확히 수렴합니다.

**듀얼 모멘텀**의 핵심은 절대 모멘텀입니다. 이게 없으면 전체 시장이 빠질 때
'덜 빠지는 종목'을 계속 들고 있게 됩니다.

## 설계상 중요한 두 가지

**미래를 보지 않습니다.** 전략에는 판단 시점까지 잘린 가격만 넘기고, 체결은
반드시 다음 봉에서 합니다. `tests/test_backtest.py::test_backtest_does_not_peek_into_the_future`가
이를 직접 검증합니다 — 분기점 이후 가격을 3배로 바꿔도 그 이전 자산곡선은 한 푼도
달라지지 않아야 합니다.

**시세 공백을 0원으로 치지 않습니다.** 거래일 달력이 다른 시장(국내+미국)을 섞으면
한쪽만 쉬는 날이 반드시 생깁니다. 그날 보유 종목을 0원으로 평가하면 가격이 전혀
변하지 않았는데도 자산곡선에 가짜 폭락과 가짜 급등이 생깁니다. 평가에는 마지막으로
알려진 종가를 쓰고(앞으로만 채움), 시세가 없는 종목은 거래하지 않습니다.

**비용을 먼저 뺍니다.** 수수료, 증권거래세(국내 매도 0.18%), 슬리피지를 매 체결마다
차감합니다. 리밸런싱이 잦은 전략은 비용만으로 수익이 통째로 사라지기도 합니다.
`stockbot compare`의 `연 회전율`과 `총 거래비용`을 항상 같이 보세요.

## 워크포워드 검증 — 그 백테스트를 믿어도 되는가

백테스트로 전략을 고르는 순간, 그 성과는 더 이상 성과가 아니라 **선택의 근거**가
됩니다. 같은 데이터로 고르고 같은 데이터로 평가하면 항상 좋아 보입니다.
`compare`의 1등을 그대로 쓰면 안 되는 이유입니다.

워크포워드는 앞 구간(학습)에서 전략을 고르고 **바로 뒤 구간(검증)에서만** 평가합니다.
창을 밀며 반복하고, 검증 구간의 수익률만 이어 붙입니다. 그 곡선은 "그때그때 가진
데이터로 전략을 골랐다면 실제로 어땠을까"에 대한 답입니다.

```bash
python -m stockbot.cli walkforward -c config/portfolio.yaml --compare-full
```

```
  학습 -> 검증                            선택               학습     검증  검증 CAGR   검증 MDD
  ----------------------------------------------------------------------------------------------
  2016-01 ~ 2018-11 -> 2018-11 ~ 2019-11  듀얼모멘텀         0.94     1.46      21.2%      -6.9%
  2018-11 ~ 2021-10 -> 2021-10 ~ 2022-10  리스크패리티       2.31    -0.21       1.9%      -5.9%
  ...
  학습 구간 평균 sharpe          1.190
  검증 구간 평균 sharpe          0.991
  워크포워드 효율 (검증/학습)     0.83   어느 정도 재현됨
  선택 뒤집힘 비율                0.86   매번 1등이 바뀜 — 잡음이 고르고 있습니다
```

읽는 법:

| 지표 | 의미 |
|---|---|
| **워크포워드 효율** | 검증 성과 / 학습 성과. 1에 가까우면 재현됨. 0.5 아래면 절반은 운. **음수면 학습에서 좋아 보인 것이 실제로는 손해** |
| **선택 뒤집힘 비율** | 구간마다 1등이 바뀐 비율. 0.7 이상이면 데이터가 아니라 잡음이 전략을 고르고 있다는 신호 |
| **전체 기간 vs 워크포워드** | `--compare-full`로 나란히 봅니다. 그 차이가 과최적화의 크기입니다 |

설정은 `walkforward` 블록에서 합니다.

```yaml
walkforward:
  train: 756                 # 학습 3년(거래일)
  test: 252                  # 검증 1년
  mode: rolling              # rolling(창을 민다) | anchored(시작점 고정)
  objective: sharpe          # 학습 구간에서 1등을 고르는 기준
  candidates:                # 비우면 위 strategy 하나의 구간별 안정성만 봅니다
    - {name: "균등비중", type: equal}
    - {name: "리스크패리티", type: risk_parity, params: {lookback: 120}}
```

**워크포워드도 만능은 아닙니다.** 과최적화를 *측정*할 뿐 없애지 못합니다. 같은
데이터로 `train`/`test`/`objective`를 계속 바꿔가며 돌리면, 그 결과 역시 결국
과최적화됩니다. 설정을 정한 뒤 한 번 돌리고 그 결과를 받아들이는 편이 낫습니다.

## 구조

```
src/stockbot/
  data/        가격 수집 (yahoo / krx / csv), 캐시, 환율 변환, 달력 정렬
  validation/  워크포워드 검증 — 학습/검증 구간 분할과 과최적화 측정
  portfolio/   목표 비중 산출 전략
  backtest/    시뮬레이션 엔진, 거래비용, 리밸런싱 스케줄
  execution/   주문 생성 → 안전장치 → 브로커 제출
  config.py    YAML 설정 로딩
  pipeline.py  설정 → 데이터 → 전략 → 결과 조립
  cli.py       명령줄 인터페이스
```

## 실전에서 드러난 것 — 겪은 문제와 대응

### 결제 전 예수금을 '남은 현금'으로 읽으면 같은 날 또 산다

국내 주식 대금은 **D+2 결제**다. 그래서 잔고조회의 `dnca_tot_amt`(예수금총금액)에는
오늘 산 금액이 아직 빠져 있지 않다. 그것을 남은 현금으로 읽으면 프로그램은
"아직 현금이 그대로네"라고 판단하고 다시 산다.

실제로 069500을 45주씩 **두 번** 샀다. 두 번째 실행의 주문 계획에 `현재 24.84%`가
찍혀 있었는데, 이미 절반을 샀는데도 현금이 1,000만원으로 보여 목표 50%를 채우려
또 매수한 것이다.

- `prvs_rcdl_excc_amt`(가수도정산금액, D+2 기준)를 쓴다. 오늘 매수·매도가 반영돼 있다.
- `account`가 두 숫자를 나란히 보여준다. 하나만 보이면 "현금이 그대로네"의 이유를
  알 수 없다.

### 같은 날 두 번 제출을 막는다

현금 계산은 고쳤지만, 무인 운영에는 방어선이 하나 더 필요하다. `trade --execute`는
그날 이미 제출했으면 거부하고 종료코드 3으로 끝난다. 의도적으로 한 번 더 내려면
`--again`을 붙인다. 계획만 보는 `trade`는 그날의 제출로 세지 않는다.

기록은 **제출 직전에** 남긴다. 제출 후에 쓰면 중간에 죽었을 때 다시 낸다.

### 미수(외상매수) 상태에서는 더 사지 않는다

중복 매수로 1천만원 계좌에 1,968만원어치가 들어가 D+2 현금이 −9,682,552원이
됐다. 보유 비중 합이 196%, 약 2배 레버리지다.

현금 계산과 중복 제출은 각각 고쳤지만, 다른 경로로 같은 상태가 될 수 있다.
그래서 규칙을 하나 더 둔다: **현금이 마이너스면 매수 주문을 내지 않는다.**
매도는 막지 않는다 — 막으면 벗어날 수가 없다.

바꾸려면 `risk.allow_margin: true`. 무인 운용에서는 권하지 않는다.

### 안전장치가 복구를 막으면 안 된다

중복 매수로 미수(외상매수)가 생긴 상태를 정리하려 했더니, 총 거래대금 한도에
걸려 **승인 0건**으로 계좌가 잠겼다. 벗어나라고 만든 장치가 벗어나는 것을 막았다.

원인은 한도의 기준이 **평가액**이었던 것이다. 평가액 = 자산 − 부채라 빚이 끼면
작아지는데, 그 상태를 벗어나려면 평가액보다 큰 금액만큼 팔아야 한다.

기준을 **총자산(보유평가 + 현금)**으로 바꿨다. 빚이 없는 계좌에서는 두 값이 같아
동작이 달라지지 않고, 폭주 매수는 여전히 막힌다.

### 접수 != 체결

주문 API는 접수번호만 돌려준다. 체결은 `account`로 다시 확인해야 한다. 제출 직후
조회하면 일부만 반영돼 있을 수 있다(유동성이 얇은 종목일수록 늦다).

## 분봉 모으기 — 1년 뒤를 위한 준비

KIS는 분봉을 **1년만** 보관한다. 지금 1년치로 일중 전략을 검증하면 워크포워드
구간이 2~3개뿐이라 과최적화를 가려낼 수 없다(월간 검증은 9개 구간으로도
학습↔검증 상관이 −0.109였다). 그래서 **지금부터 매일 쌓는다.** 1년 뒤에는 2년치가
되고, 그때 제대로 된 판별이 가능해진다.

```bash
python -m stockbot.cli collect -c config/portfolio.yaml
```

- 기본은 최근 7일 중 아직 안 받은 날만 받는다. 매일 한 번 돌리면 충분하다.
- 과거를 한꺼번에 채우려면 `--start 2025-10-01` (1년치는 20분쯤 걸린다).
- **장중에는 오늘 날짜를 건너뛴다.** 반쪽짜리 하루가 저장되면 나중에 쓸 수 없다.
- 휴장일은 0건으로 오는데, 그 사실을 기록해 **다시 묻지 않는다.** 안 그러면
  1년치를 채우는 동안 헛호출이 수백 번 쌓인다(모의투자는 초당 제한이 빡빡하다).
- 저장은 `data/minutes/`. 저장소에는 올리지 않는다.

NAS 작업 스케줄러에 매 거래일 16:00(장 마감 후)으로 걸어두면 된다.

## 일중 전략 — 변동성 돌파 검증

장중에 사고파는 전략을 붙이기 전에, **비용을 넣고도 남는지**부터 잰다.

```bash
python -m stockbot.cli breakout -c config/portfolio.yaml --ticker 069500
```

11년치 일봉으로 워크포워드를 돌려, 돌리기 전에 정해 둔 다섯 기준에 대고
합격/불합격을 찍는다. 하나라도 못 넘으면 모의계좌에 올리지 않는다.

| 기준 | 합격선 | 이유 |
|---|---|---|
| 비용 차감 검증 샤프 | > 0.5 | 비용 넣고도 남는가 |
| 학습↔검증 샤프 상관 | > 0 | 최소한 음수는 아닐 것 |
| 연간 진입 횟수 | < 150 | 왕복 11bp × 150 = 연 17%가 비용 상한 |
| 최대낙폭 | > −25% | 현재 균등비중이 −17% |
| buy&hold 대비 | 우위 | 수고한 값어치가 있었나 |

기준값은 `intraday/evaluate.py`의 `Criteria`에 박혀 있고 테스트로 고정돼 있다.
결과를 보고 고치면 판정이 아니라 변명이 된다.

### 왜 분봉이 아니라 일봉으로 먼저 재는가

KIS는 분봉을 1년만 보관한다. 1년이면 워크포워드 구간이 2~3개뿐이라 과최적화를
판별할 수 없다(월간 검증에서 9개 구간으로도 학습↔검증 상관이 −0.109였다).
변동성 돌파는 전일 고가·저가만 있으면 일봉으로 근사할 수 있고, 일봉은 11년치가
있다.

**근사는 실제보다 좋게 나온다.** 기준가에 정확히 체결됐다고 가정하고, 고가가
돌파 전에 나왔는지 후에 나왔는지 구분하지 못한다. 그래서 거르는 용도로만 쓰고,
통과한 전략은 1년치 분봉으로 다시 재서 얼마나 부풀려졌는지 확인해야 한다.

### 매매가 잦을수록 불리한 이유

국내 ETF 왕복 1회 비용은 약 **0.110%**(수수료 0.015% + 슬리피지 0.04%, 양방향).

| 진입 빈도 | 연 왕복 | 연 비용 |
|---|---:|---:|
| 매일 | 250 | 31.6% |
| 2일에 1번 | 125 | 14.7% |
| 주 1번 | 50 | 5.7% |
| 월 1번 (기본 설정) | 12 | 1.3% |

수익률이 0%여도 원금이 줄어든다. 전략을 고르는 것보다 **덜 사고파는 것**이
영향이 크다.

## 한국투자증권(KIS) 연동

국내주식 브로커가 구현돼 있습니다(`src/stockbot/execution/kis.py`).
**기본값은 모의투자**이며, 실계좌는 `KIS_PAPER=false`를 직접 넣어야만 열립니다.

### 1. 키 발급

[KIS Developers](https://apiportal.koreainvestment.com/)에서 앱키·앱시크릿을
발급받습니다. **모의투자용 키와 실전용 키는 서로 다릅니다.** 섞어 쓰면 인증이
깨집니다. 모의투자 계좌는 HTS/MTS에서 따로 개설해야 합니다.

### 2. .env 작성

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

```ini
KIS_APP_KEY=발급받은_앱키
KIS_APP_SECRET=발급받은_앱시크릿
KIS_ACCOUNT=12345678      # 계좌번호 앞 8자리
KIS_PRODUCT_CODE=01       # 뒤 2자리
KIS_PAPER=true            # 모의투자
```

`.env`는 `.gitignore`에 있어 커밋되지 않습니다. 절대 공유하지 마세요.

### 3. 설정 바꾸기

**KIS 브로커는 국내주식 주문 API만 구현돼 있습니다.** 포트폴리오에 SPY 같은
미국 티커가 있으면 그 종목에서 주문이 실패합니다. 처음 붙일 때는 국내 ETF
2종목만 담은 검증용 설정을 쓰세요.

```bash
cp config/kr_paper.example.yaml config/portfolio.yaml
# Windows: copy config\kr_paper.example.yaml config\portfolio.yaml
```

직접 고칠 때 빠뜨리기 쉬운 두 곳:

```yaml
execution:
  broker: kis
  allow_fractional: false   # 국내주식은 소수점 주문 불가

risk:
  # 2종목 균등이면 한 종목이 50%다. 기본값 0.40이면 매수가 전부 취소되고
  # 오류 없이 "주문 0건"만 나와서 원인을 찾기 어렵다.
  max_position_weight: 0.55
  # 모의계좌 예수금에 맞춰야 한다. 예수금 1억 x 50% = 주문 1건 5천만원.
  max_order_value: 60000000
```

### 4. 순서대로 확인

```bash
python -m stockbot.cli checkenv -c config/portfolio.yaml  # 키/설정이 맞는가
python -m stockbot.cli account  -c config/portfolio.yaml  # 잔고가 보이는가
python -m stockbot.cli trade   -c config/portfolio.yaml   # 주문 계획만(제출 안 함)
python -m stockbot.cli trade   -c config/portfolio.yaml --execute
```

`--execute` 없이는 주문이 나가지 않습니다. 실계좌(`is_live=True`)에서는 제출
직전에 확인 프롬프트가 뜹니다.

`checkenv`는 주문을 내지 않고 세 가지만 순서대로 봅니다: ① `.env` 파일과 값
(네트워크 없이), ② 설정의 브로커와 종목 시장, ③ KIS 서버 인증. 나눠 보는 이유는
한꺼번에 시도하면 "인증 실패" 한 줄만 남아 오타인지, 키가 틀린 건지, 설정을 안
바꾼 건지 구분할 수 없기 때문입니다. 키 값은 앞뒤 몇 글자만 찍히므로 이 화면은
그대로 공유해도 됩니다. `--offline`을 붙이면 ③을 건너뜁니다.

### 알아둘 것

- **접수 ≠ 체결.** 주문 API는 접수번호만 돌려줍니다. 결과 상태가 `accepted`로
  표시되는 이유이며, 실제 체결은 `account`로 다시 확인해야 합니다.
- **장 운영시간에만 됩니다.** 장이 닫혀 있으면 KIS가 거부하고, 그 메시지가
  그대로 화면에 나옵니다.
- **토큰은 24시간 유효**하고 발급 횟수 제한이 있어 `.state/kis_token.json`에
  캐시합니다. 이 파일은 계좌 접근 권한 그 자체이므로 권한 600으로 저장합니다.
- 엔드포인트와 TR ID는 한국투자증권 공식 예제에서 확인했습니다. 인터넷에 널리
  퍼진 구버전(`TTTC0802U` 등)과 다릅니다.
- **실제 KIS 서버와의 통신은 검증하지 못했습니다.** API 키가 있어야 하기
  때문입니다. 모듈 로직은 가짜 전송 계층으로 테스트했습니다(`tests/test_kis.py`).
  처음 붙일 때 문제가 생기면 오류 메시지를 그대로 알려주세요.

### NAS / 서버에서 무인 운영

컨테이너 정의(`Dockerfile`, `docker-compose.yml`)가 들어 있습니다. NAS에 파이썬을
직접 깔면 OS 업데이트 때 날아가거나 다른 패키지와 충돌하므로 컨테이너를 씁니다.

```bash
docker compose build
docker compose run --rm bot checkenv -c config/portfolio.yaml
docker compose run --rm bot account  -c config/portfolio.yaml
```

- 키는 이미지에 굽지 않고 실행할 때 `--env-file`(compose는 `env_file`)로 넘깁니다.
  이미지에 들어가면 이미지를 가진 사람이 곧 계좌 접근 권한을 갖습니다. CI가
  이미지에 `.env`가 없는지 매번 확인합니다.
- `.env` 파일 없이 환경변수만으로도 돕니다(NAS 관리화면에서 넣는 방식).
- 컨테이너를 24시간 띄워 두지 않습니다. NAS의 작업 스케줄러가 필요할 때
  한 번 실행하고 끝내는 구조입니다.
- 시간대는 이미지 안에서 `Asia/Seoul`로 고정됩니다.
- CI가 amd64와 **arm64** 양쪽으로 빌드합니다. NAS는 대부분 ARM이라,
  amd64에서만 되는 이미지를 넘기면 NAS에서 처음 빌드할 때에야 깨집니다.

아직 없는 것: 정기 실행 스케줄과 실행 결과 알림. 알림 없이 무인으로 돌리면
조용한 실패(토큰 만료, 시세 제공처 장애, 안전장치로 주문 0건)를 알 수 없습니다.

### NAS에서 직접 git pull 하기 (권장)

File Station으로 파일을 올리는 방식은 번거롭고, **윈도우를 거치면서 줄바꿈이
CRLF로 바뀌는 문제**도 거기서 생긴다. NAS에서 바로 받으면 둘 다 사라진다.

비공개 저장소라 인증이 필요한데, **배포 키**를 쓴다. 읽기 전용이고 이 저장소
하나에만 쓰이며 만료 관리가 필요 없다 — 계정 전체에 권한을 주는 토큰보다 안전하다.

**1. NAS에서 키 만들기** (SSH로 접속해 root로)

```bash
ssh-keygen -t ed25519 -f /root/.ssh/stockbot_deploy -N "" -C "stockbot-nas"
cat /root/.ssh/stockbot_deploy.pub
```

**2. GitHub에 공개키 등록**

저장소 → Settings → Deploy keys → Add deploy key → 위 출력을 붙여넣기.
**"Allow write access"는 체크하지 않는다.** NAS는 받기만 하면 된다.

**3. NAS가 그 키를 쓰도록**

```bash
cat >> /root/.ssh/config <<'CONF'
Host github.com
    HostName ssh.github.com
    Port 443
    User git
    IdentityFile /root/.ssh/stockbot_deploy
    IdentitiesOnly yes
CONF
chmod 600 /root/.ssh/config
```

`ssh.github.com:443`을 쓰는 이유: 22번 포트는 가정용 공유기나 통신사에서 막는
경우가 있다. 443은 거의 열려 있다.

**4. 원격 주소를 SSH로 바꾸기**

```bash
cd /volume1/docker/stockbot/Personal_Stock_Program
git remote set-url origin git@github.com:joe880530/Personal_Stock_Program.git
ssh -T git@github.com     # "Hi ...! You've successfully authenticated" 가 나오면 성공
```

**5. 이제 한 줄로 업데이트**

```bash
bash scripts/nas_update.sh
```

받고 → 바뀐 게 있으면 이미지를 다시 만들고 → 새 이미지로 점검까지 한 번 돌린다.

- 바뀐 것이 없으면 다시 빌드하지 않는다(`--force`로 강제).
- **빌드 실패 시 이전 이미지를 그대로 둔다.** 운용이 멈추지 않는다.
- 빌드가 됐다고 도는 것은 아니므로, 새 이미지로 `checkenv --offline`을 한 번
  돌려본다. 여기서 실패하면 알려준다.
- File Station으로 덮어쓴 파일이 남아 있으면 **조용히 버리지 않고** 무엇이
  사라질지 보여주며 멈춘다. 버려도 되면 `--reset`.
- `.env`와 `config/portfolio.yaml`은 저장소에 없으므로 건드리지 않는다.

#### 업데이트를 자동 실행에 끼워 넣지 않는다

주문 직전에 코드를 자동으로 받아오면, 검증하지 않은 커밋이 그날 주문을 내게
된다. 작업 스케줄러에는 `nas_run.sh trade`만 걸고, `nas_update.sh`는 사람이
부를 때만 돌린다. **배포와 실행은 분리한다.**

### 시놀로지 NAS에 올리기 (DS220+ 기준)

DS220+는 x86-64라 amd64 이미지가 그대로 돕니다. DSM 7.2 이상이면 Container
Manager가 패키지 센터에 있습니다.

**1. 파일 올리기** — File Station으로 프로젝트 폴더를 통째로 올립니다.
예: `/volume1/docker/stockbot`. `.env`와 `config/portfolio.yaml`도 같이 둡니다
(깃에는 없으니 직접 만들어 올려야 합니다).

**2. 이미지 만들기** — SSH로 들어가 한 번만 합니다 (제어판 → 터미널 및 SNMP →
SSH 서비스 활성화).

```bash
sudo -i
cd /volume1/docker/stockbot
docker build -t stockbot .
```

> 윈도우를 거쳐 파일을 옮겼다면 셸 스크립트의 줄 끝이 CRLF로 바뀌어
> `$'\r': command not found` 가 날 수 있습니다. `.gitattributes`가 막고 있지만,
> 이미 그렇게 된 파일은 NAS에서 한 번 고치면 됩니다:
> `sed -i 's/\r$//' scripts/nas_run.sh`

**3. 손으로 한 번 확인**

```bash
bash scripts/nas_run.sh checkenv
bash scripts/nas_run.sh account
bash scripts/nas_run.sh trade        # 주문 계획만, 제출 안 함
```

**4. 작업 스케줄러 등록** — 제어판 → 작업 스케줄러 → 생성 → 예약된 작업 →
사용자 정의 스크립트

| 항목 | 값 |
|---|---|
| 사용자 | `root` (docker 명령에 권한이 필요) |
| 일정 | 매월 원하는 날, 평일 장중 (09:00~15:30) |
| 명령 | `bash /volume1/docker/stockbot/scripts/nas_run.sh trade` |

설정 탭에서 **실행 결과를 이메일로 받도록** 켜고, "비정상 종료 시에만 보내기"를
선택합니다. 평소엔 조용하고 문제가 생겼을 때만 옵니다. 메일 주소는
제어판 → 알림에서 먼저 설정해야 합니다.

`nas_run.sh`가 하는 일:

- 표준출력은 짧게(요약), 전체는 `logs/stockbot.log`에. 메일에 수백 줄을 쏟으면
  정작 읽어야 할 줄이 묻힙니다. 실패했을 때만 전문을 냅니다.
- 실패하면 0이 아닌 값으로 끝납니다. DSM은 종료코드로 실패를 판단하므로,
  이걸 삼키면 알림이 오지 않습니다.
- 스케줄러는 작업 디렉터리를 보장하지 않습니다(대개 `/`). 스크립트 위치를
  기준으로 경로를 잡습니다.
- 컨테이너는 uid 1000으로 도는데 NAS에서 만든 폴더는 root 소유입니다.
  그대로 두면 토큰·시세 캐시를 쓰지 못해 "매번 토큰 재발급"처럼 엉뚱한
  증상이 납니다. 실행 전에 소유자를 맞춥니다.
- 인자를 빠뜨려도 `trade`(계획만)까지만 합니다. 실주문은 `--execute`를
  직접 붙여야 나갑니다.

### 미국 주식

두 경로 모두 아직 없습니다.

- **KIS 해외주식 API**: 미구현. 현재 `KISBroker`는 국내주식 엔드포인트만
  씁니다. 미국 ETF를 KIS로 사려면 해외주식 주문/잔고 API를 따로 붙여야 하고,
  환전과 양도소득세도 같이 다뤄야 합니다.
- **국내 상장 대체 ETF**: 코드 변경 없이 지금 됩니다. TIGER 미국S&P500
  (`360750`), TIGER 미국나스닥100(`133690`), TIGER 미국채10년선물(`305080`),
  KODEX 골드선물(H)(`132030`) 등을 `market: KR_ETF`로 넣으면 됩니다.
  환전이 필요 없고 국내 ETF는 배당소득세(15.4%) 과세라 해외주식
  양도소득세(22%)를 피합니다. 대신 총보수가 조금 높고 추적오차가 있습니다.

Alpaca는 아직 미구현입니다. 같은 `Broker` 인터페이스를 구현하면 됩니다
(`paper-api.alpaca.markets`부터).

### 지키면 좋은 순서

1. `signal`로 몇 주 돌려보며 신호가 납득 가는지 눈으로 확인
2. 모의 브로커로 `trade --execute`를 리밸런싱 주기만큼 반복
3. 실계좌 브로커를 붙이되 소액으로, `risk.max_order_value`를 작게
4. 그다음에 금액을 늘리고 스케줄러에 올리기

## 안전장치

주문은 `RiskGuard`를 통과해야만 나갑니다.

| 항목 | 역할 |
|---|---|
| `max_position_weight` | 한 종목 쏠림 방지 |
| `max_order_value` | 주문 1건 금액 상한 |
| `max_orders` | 폭주(무한 루프) 방지 |
| `max_drawdown_stop` | 고점 대비 일정 낙폭이면 신규 매수 중단(매도는 허용) |
| `allowed_tickers` | 설정에 없는 종목은 절대 거래하지 않음 |
| `min_price` | 데이터 오류로 가격이 0이 되는 경우 차단 |

## 테스트

```bash
pip install -e ".[dev]"
python -m pytest -q
```

### `invalid choice: 'walkforward'` 같은 오류가 나면

새로 추가된 명령을 옛 설치본이 모르는 상태입니다. 먼저 뭐가 깔려 있는지 확인하세요:

```bash
python -m stockbot.cli --version
```

```
stockbot 0.1.0
  코드 위치: C:\Users\lenovo\Personal_Stock_Program\src\stockbot
  설치 형태: 편집 설치(-e) 또는 소스 직접 실행
  사용 가능한 명령: backtest, compare, walkforward, signal, checkenv, account, trade
```

`사용 가능한 명령`에 쓰려는 명령이 없거나, `코드 위치`가 예상과 다르면 **저장소
루트에서** 아래를 실행합니다:

```bash
git pull
pip install -e ".[us,kr]"     # -e 로 깔아두면 다음부터는 git pull 만으로 끝납니다
```

`설치 형태`가 `일반 설치`로 나오면 `git pull`만으로는 코드가 바뀌지 않습니다.
매번 `pip install .`을 다시 해야 하므로, 개발 중에는 `-e`를 권합니다.

### 설치가 제대로 됐는지 확인

`pytest`는 `pyproject.toml`의 `pythonpath = ["src"]` 설정 때문에 소스를 직접
import합니다. 그래서 **패키지 설정이 깨져 있어도 전부 통과할 수 있습니다.**
실제로 `.gitignore`의 `data/` 규칙이 `src/stockbot/data/`까지 제외해서, 로컬
테스트는 181개 모두 통과하는데 clone한 환경에서는 `ModuleNotFoundError`가 난
적이 있습니다.

그 구멍을 막는 검사입니다. **저장소 밖에서** 실행해야 의미가 있습니다:

```bash
pip install .            # 편집 설치(-e)가 아니라 실제 설치
cd /tmp                  # Windows: cd %TEMP%
python <저장소경로>/scripts/check_install.py
```

### CI (자동 검사)

`.github/workflows/ci.yml` — 푸시할 때마다 GitHub가 깨끗한 리눅스 컨테이너에서
자동으로 돌립니다. 결과는 PR 화면과 저장소의 **Actions** 탭에 초록/빨간 표시로
나타납니다.

| 잡 | 하는 일 |
|---|---|
| `테스트` | Python 3.10과 3.13 양쪽에서 `pytest` 전체 실행 |
| `설치 가능성 + 오프라인 스모크` | 비편집 설치 → 저장소 밖에서 import 검사 → 합성 데이터로 `backtest`/`compare`/`signal`/`walkforward` 실행 |

실제 시세 제공자(Yahoo·FinanceDataReader)는 CI에서 부르지 않습니다. 외부
서비스가 장애를 겪으면 우리 코드와 무관하게 빨간불이 되고, 그러면 아무도 CI를
믿지 않게 됩니다. 네트워크가 필요한 경로는 로컬에서 확인하세요.

## 한계 (알고 쓰세요)

- **과최적화**: 같은 데이터로 전략을 고르면 우연히 좋은 것이 하나는 나옵니다.
  `walkforward`로 그 크기를 재기 전에는 `compare` 결과의 1등을 믿지 마세요.
  워크포워드 설정 자체를 여러 번 바꿔가며 돌리는 것도 같은 함정입니다.
- **생존 편향**: 지금 상장된 종목만으로 백테스트하면 실패한 종목이 빠져 성과가
  부풀려집니다. ETF 위주로 구성하면 영향이 작습니다.
- **세금**: 국내 매도 거래세는 반영하지만 해외주식 양도소득세(22%)와 배당소득세는
  반영하지 않습니다. 실제 수익률은 더 낮습니다.
- **체결 가정**: 원하는 수량이 기준가 근처에서 다 체결된다고 봅니다. 거래량이 적은
  종목에서는 낙관적인 가정입니다.
- **시장 데이터 제공자**: `yfinance`와 `finance-datareader`는 비공식 소스입니다.
  장애나 스펙 변경이 있을 수 있습니다. 종목 데이터를 하나라도 못 받으면 기본적으로
  **중단**합니다(`data.on_missing`). 조용히 빠진 채로 도는 것보다 낫기 때문입니다 —
  안전자산 하나가 빠지면 하락장 방어가 사라지는데도 성과 숫자는 멀쩡해 보입니다.
