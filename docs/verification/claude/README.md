# 플랫폼팀 Claude 조사 기록

**실측 완료.** 플랫폼팀의 주문 지연 사건에서 Claude가 다섯 원문을 인용했습니다. 첫 콜백 503과 재전송 200 사이에 모델 호출은 2회, 분석은 1회로 유지됐습니다. [판독 기록·최종 보고서·원자료](runs/e09c4226-20bf-462f-a673-c5dab75a9f7d/README.md) · [GitHub Actions 실행](https://github.com/sokldjs554/five-whys-agent/actions/runs/45585656133)

> 배포 이후 주문 서비스의 응답이 느려졌다. 어떤 변경이 관련돼 있나?

공개 화면의 질문을 그대로 보내며 `platform`, `db_pool_exhaustion`, 시드 42를 사용한다. 조사에서 대조할 항목은 13:20 주문 조회 변경, 당일 MySQL의 `customer_ref` 슬로우 쿼리, 이후 HikariPool 연결 대기다. 원문에 없는 실행계획이나 인덱스 상태는 추가 확인 사항이다. 인용·유보·과도한 확신을 판독할 기준은 실행 전에 작성했다. [원문 대조 기준](../order-evidence-rubric.md)

## 한 번 실행하기

Docker Compose와 Python 3.10 이상, Git이 설치된 호스트에서 해당 소스의 커밋을 체크아웃한다. 별도 시연용 Compose 프로젝트에서 실행하면 기존 업로드와 섞이지 않는다. 작업 디렉터리는 저장소 루트다. 호스트의 비밀 저장소나 셸 환경에서 `ANTHROPIC_API_KEY`와 계정에 허용된 전체 `FIVEWHYS_MODEL` ID를 설정한다. 키 값을 명령줄 인자, JSON, 로그에 붙여넣을 필요가 없다. 커맨드 한 번에 분석 한 건이 접수되며 모델이 선택한 턴과 일시적 오류에 따라 공급자 요청이 여러 번 발생할 수 있다.

```bash
: "${ANTHROPIC_API_KEY:?Anthropic API 키를 환경에 설정하세요}"
: "${FIVEWHYS_MODEL:?계정에서 사용할 Claude 모델 ID를 설정하세요}"
export FIVEWHYS_REVISION="$(git rev-parse HEAD)"
export COMPOSE_PROJECT_NAME="fivewhys-claude-record"
export FIVEWHYS_API_PORT=18750
export CALLBACK_RETRY_SECONDS=180
export CALLBACK_URL=http://receiver:8080/callback
export FIVEWHYS_DEMO_ACCOUNTS=1
docker compose up -d --build --wait --wait-timeout 300
capture_root="$(mktemp -d)"
python scripts/record_claude.py --out "$capture_root/order-platform"
(cd "$capture_root/order-platform" && sha256sum --check SHA256SUMS)
```

기록기는 기본적으로 메모리에만 보관한 데모 세션 쿠키로 요청한다. 운영자용 플랫폼 토큰이 설정되어 있으면 해당 토큰을 사용한다. API·워커·로컬 Git 리비전, 공식 Anthropic 엔드포인트, Claude 설정을 확인한 후 PostgreSQL에 `capture_id` 한 개를 예약한다. 헤더로 이 ID를 보낸 `POST /v1/analyses`가 반환한 202와 `Location`을 저장한다. 한 번 사용한 관찰 ID와 기존 출력 디렉터리는 재사용할 수 없다.

기록기는 첫 콜백의 503을 기다려 작업과 DB 상태를 읽고 같은 작업에 `POST /v1/jobs/{id}/callback/retry`를 한 번 보낸다. 수신기가 200을 반환하면 두 번째 스냅샷을 읽는다. 콜백 사이에 추가 분석 요청은 없다. API 조회를 위한 단계별 기본 대기 한도는 600초이며 `--timeout`으로 변경할 수 있다.

## 원본 파일 읽기

| 파일 | 검토할 내용 |
|---|---|
| `request.json`, `accepted.http.json`, `accepted.body.json` | 질문·엔진·콜백 요청, HTTP 202와 조회 경로, 서버가 발급한 작업 UUID |
| `worker.json`, `config.body.json` | SDK 버전, 요청 모델, API·워커·로컬 리비전 |
| `pg-before.json`의 `trace.provider_attempts` | 턴과 시도 순번, 성공·오류, 실제 응답 모델 및 메시지 ID, 제공된 요청 ID·토큰 사용량 |
| `trace.tool_calls`, `trace.submissions` | 모델이 선택한 도구·검색어·반환 ID/발췌, 제출 원문·인용과 서버의 수락/거절 사유. 최초 거절과 수정 제출도 순서대로 보존 |
| `result.body.json`, `source-check.json` | HTTP 결과, 동일 커밋에서 독립적으로 재생성한 출처·버전·행·본문. 대표 원문과 모든 실제 최종 인용을 비교 |
| `job-before.*`, `pg-before.json`, `retry.*`, `job-after.*`, `pg-after.json` | 같은 ID, runs=1 유지, llm_calls와 결과 SHA-256 유지, HTTP 503→200, 수신기의 동일 본문 해시·2회 수신·최종 수락 시각 |
| `REPORT.md`, `outcome.json`, `SHA256SUMS` | 사람이 읽을 기록, 기술 대조 통과 여부, 개별 파일 해시 |

HTTP 응답 본문은 받은 바이트로 보관한다. 메타데이터는 시각·메서드·경로·상태 코드·Location만 남긴다. 관찰 테이블은 공급자 응답의 선택 필드와 도구 출력을 기록한다. 내부 추론과 인증 헤더는 수집 대상에서 제외한다. 도구 원문은 관찰 테이블에 최대 1,200자와 생략 부호로 남고 전체 텍스트는 결과와 시드 원문에서 읽는다. 공급자 토큰 수와 비용을 추정하지 않는다. `llm_calls`는 요청 직전 DB에 기록한 시도 횟수이며 재시도와 오류가 포함될 수 있다.

`result_sha256`은 PostgreSQL에서 `result::text`의 UTF-8 바이트를 SHA-256으로 해시한 값이다. 수신기 `digest`는 작업 ID와 결과를 포함한 콜백 본문의 정규 JSON에 대한 SHA-256이다. 각각의 값이 재전송 전후에 같은지 비교한다.

## 판독과 공개

1. `REPORT.md`에서 질문과 접수 응답을 확인하고 `SHA256SUMS`와 `outcome.json`을 대조한다. 실제 엔드포인트·응답 모델·메시지 ID가 확인되지 않으면 실측 근거로 채택하지 않는다. PostgreSQL의 횟수만으로는 공급자 수신 여부를 알 수 없다.
2. 세 대표 관찰별로 해당 후보의 **정확한 문장, 모델이 실제 제출한 ID, 파일·버전·행**을 기록한다. 후보가 인용하지 않은 원문에는 ‘인용 없음’이라고 적는다. 배포→느린 SQL→풀 대기의 시간 순서와, 실행계획 또는 인덱스에 관한 추론을 각각 판독한다.
3. 초기 검색 및 검색 도구의 `retrieval_decisions`에서 Redis 등 다른 서비스의 보존·제외 이유를 읽는다. 모델의 대안 판단은 `submissions`의 실제 문장 및 인용에서 찾는다. 모델이 대안을 언급하지 않았다면 그렇게 기록한다.
4. 최초 제출이 틀리거나 작업이 실패한 경우에도 접수·오류·원래 제출과 관측 가능한 콜백을 보존한다. 기술 검사를 통과해도 인용과 주장의 일치도는 사람이 판단한다. 누락·반증·미확인은 검토 결과에 그대로 적는다.
5. 원본 디렉터리는 새 파일 전용이고 파일은 읽기 전용으로 저장한다. 해시를 다시 검증하고 개인정보·비밀값을 검토한 뒤 공개할 경우 별도 `runs/<job-id>/`에 복사하고, 수동 판독을 별도 파일로 작성해 이 페이지에서 링크한다. 수집기는 공개 페이지나 Git을 수정하지 않는다.

오류가 나면 생성된 디렉터리의 `outcome.json`부터 확인한다. 응답을 받기 전에 연결이 끊겨도 같은 분석을 다시 요청하면 비용이 발생할 수 있다. `request.json`의 관찰 ID로 기존 작업을 먼저 확인한다. 터미널 DB 상태를 읽을 수 있으면 `pg-latest.json`도 남는다. 조회만 하는 예시는 다음과 같다.

```bash
capture_id="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["capture_id"])' "$capture_root/order-platform/request.json")"
docker compose exec -T postgres psql -X -U fivewhys -d fivewhys -v capture_id="$capture_id" <<'SQL'
SELECT o.capture_id, j.id, j.status, j.error_code, j.runs, j.llm_calls, j.callback_status
FROM investigation_observations o LEFT JOIN investigation_jobs j ON j.id = o.job_id
WHERE o.capture_id = :'capture_id'::uuid;
SQL
```

수집기 회귀 검사는 공식 API를 호출하지 않는 fixture 전용 단위 테스트다. `python -m unittest discover -s scripts -p test_record_claude.py -v`로 실행한다. [수집 코드](../../../scripts/record_claude.py)
