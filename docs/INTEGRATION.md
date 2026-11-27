# 사건 조사 API 연동

Compose의 API는 `http://127.0.0.1:8750`, 대화형 명세는 `/docs`, JSON은 `/openapi.json`에 있다. 이 문서의 작업 ID는 예시다. [API 명세 스냅샷](openapi.json) · [콜백 수신기 명세](callback-openapi.json)

## 인증과 조사 범위

로컬 합성 데모에서는 `POST /v1/demo/session`이 `orders` 또는 `platform` 계정의 HttpOnly 쿠키를 발급한다. 두 계정 모두 주문 서비스와 게이트웨이의 관측치를 볼 수 있다. 플랫폼팀은 MySQL 슬로우 쿼리 원문까지 볼 수 있다. 팀을 바꾸면 같은 사건을 새 팀의 범위에서 다시 요청한다. 다른 팀의 작업 ID에 접근하면 HTTP 404를 반환한다.

서버 간 연동은 `Authorization: Bearer <팀 토큰>`을 사용한다. `FIVEWHYS_ORDERS_TOKEN`과 `FIVEWHYS_PLATFORM_TOKEN`에 서로 다른 임의 토큰을 설정하고, 데모 계정 발급을 끄려면 `FIVEWHYS_DEMO_ACCOUNTS=0`을 설정한다. 여러 API 프로세스에서 데모 세션을 공유하려면 동일한 `FIVEWHYS_SESSION_KEY`를 쓴다. 기존 동기 API는 종전의 합성 시나리오 저장소를 사용한다. 팀 업로드 문서는 비동기 API에서만 다룬다.

## 요청과 응답

| 호출 | 정상 응답 | 오류 |
|---|---|---|
| `POST /v1/analyses` | 202, `{id,status:"queued"}`, `Location`에 조회 경로 | 401 인증, 422 필드, 503 모델 설정 또는 DB |
| `POST /v1/indexes` | 202, 같은 작업 계약 | 401 인증, 422 문서 규격, 503 DB |
| `GET /v1/jobs/{id}` | 200, 상태·오류·시작/완료 시각·모델 호출 수·콜백 시도 이력 | 401, 404, 422, 503 |
| `GET /v1/jobs/{id}/result` | 200, 분석 결과 또는 색인 결과 | 409 완료 결과 없음, 404 미존재/권한 부족 |
| `GET /v1/jobs/{id}/evidence/{evidence_id}` | 200, 해당 실행에서 검색한 근거 원문·버전·위치 | 404 미조회 근거 또는 접근 불가, 409 미완료 |
| `POST /v1/jobs/{id}/callback/retry` | 202, 같은 작업의 상태. 예약된 콜백 시각을 앞당긴다 | 409 재시도 대기 건이 없음, 404 접근 불가 |

```bash
base=http://127.0.0.1:8750
curl -sS -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"team":"orders"}' "$base/v1/demo/session"
curl -i -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"question":"배포 이후 주문 서비스의 응답이 느려졌다. 어떤 변경이 관련돼 있나?","engine":"scripted","callback":true}' \
  "$base/v1/analyses"
```

`engine`은 `scripted`와 `claude` 중 선택한다. 전자는 실제 검색 도구를 실행하는 결정적 규칙 엔진이다. 후자는 Anthropic Messages API를 호출한다. 서버에 `ANTHROPIC_API_KEY`를 제공하고 계정에서 사용할 수 있는 모델 ID를 `FIVEWHYS_MODEL`로 지정한다. `llm_calls`는 유료 요청 직전 DB에 기록한 호출 시도 수, `engine_turns`는 보고서 제출까지의 논리적 턴 수다. 네트워크 단절 시 공급자가 실제 처리·과금한 횟수는 로컬 카운터만으로 확정할 수 없다.

```json
{
  "documents": [{
    "kind": "runbook",
    "source": "runbooks/orders/db-pool.md",
    "version": "rev-3",
    "title": "주문 DB 풀 점검",
    "service": "order-service",
    "text": "# DB 풀 점검\n1. 풀 대기 시간을 확인한다.\n2. 배포 전후의 실행계획을 비교한다."
  }],
  "callback": false
}
```

이를 `POST /v1/indexes`로 보낸다. 문서 1~32건, 본문당 UTF-8 32,768바이트까지 허용한다. `kind`는 `report`, `runbook`, `deployment`, `log`다. 배포는 한 문서에 변경 한 건을 담는다. 보고서는 제목으로 구분한 섹션, 절차서는 번호 단계별로 인덱싱한다. 팀 권한은 인증에서 정한다. 완료된 색인 결과의 `documents`, `chunks`, `evidence_ids`로 적재량을 확인한다. 같은 본문 버전·위치·권한의 근거 ID는 재시도해도 동일하다. 기존 실행은 당시 조회한 버전과 원문을 PostgreSQL에 보관한다.

## 실패와 복구

작업은 `queued → running → completed | failed`로 진행한다. HTTP 202는 접수를 뜻한다. 이후 실패는 작업 조회의 `error_code`, `error`로 전달한다. 근거 부족은 `insufficient_evidence`, 제출 형식이나 인용 오류는 `invalid_model_output`, 공급자 오류는 `provider_failure`다. 모델 출력은 한 번 수정할 수 있고 일시적 호출 실패는 호출당 한 번 재시도한다. 전체 도구 루프 한도는 8턴이다.

수신기는 서버에 등록한 `CALLBACK_URL`에서 JSON POST를 받는다. 요청 본문과 완료 이벤트 정의는 OpenAPI의 `webhooks`에 있다. `Idempotency-Key`는 작업 ID다. 실서비스 연동에서는 같은 키와 같은 본문을 여러 번 받아도 적용은 한 번만 하도록 처리한다. 샘플은 PostgreSQL에 본문 해시와 처리 여부를 저장한다.

```http
POST /callback HTTP/1.1
Content-Type: application/json
Idempotency-Key: e41cb057-6a94-4170-b62a-54243ec0654d

{"id":"e41cb057-6a94-4170-b62a-54243ec0654d","kind":"index","team":"orders","status":"completed","result":{"documents":1,"chunks":2,"evidence_ids":["ev-demo1","ev-demo2"]},"error":null,"error_code":null}
```

| 전송 | 예제 수신기 응답 | 워커 동작 |
|---|---|---|
| 1차 | HTTP 503, `{"id":"…","attempt":1,"status":"retry"}` | 결과는 완료 상태로 보존, 다음 콜백 예약 |
| 2차 | HTTP 200, `{"id":"…","attempt":2,"status":"accepted"}` | `callback_status=delivered`, 동일 키·동일 본문 |
| 내용 불일치 | HTTP 409 | 이력에 상태 기록, 한도 내 재시도 |
| 키/필드 오류 | HTTP 400 또는 422 | 이력에 기록 |

Compose 시연의 재시도 대기는 180초, 360초(워커 단독 기본값 30초, 60초)이며 최초 전송을 포함해 최대 세 번 시도한다. 수동 버튼은 예약을 앞당긴다. 자체 수신기를 연결하려면 Compose 워커의 `CALLBACK_URL`을 변경한다. 워커가 재시작하면 미확인 메시지가 다시 전달되고, PostgreSQL의 잠금과 종결 상태를 확인한 뒤 작업한다. 유료 요청을 이미 시도한 뒤 응답 저장 전에 워커가 종료되면 중복 과금 위험을 줄이기 위해 `provider_outcome_unknown`으로 종료한다. 해당 상황의 공급자 처리 여부는 별도 확인 대상이다.
