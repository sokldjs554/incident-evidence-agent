# 합성 사건의 검색 및 복구 검증

[DB 풀 장애와 인증서 만료의 후속 비교](TWO_CASE_RETRIEVAL.md) · [실제 Claude 조사 상태 및 실행 절차](claude/README.md) · [최신 서비스 통합 CI 실행](https://github.com/sokldjs554/five-whys-agent/actions/workflows/test.yml?query=branch%3Ademo%2Fincident-backend)

## 실행 기준 4d01473의 결과와 데모에서 확인할 두 장면

Compose의 PostgreSQL, RabbitMQ, Milvus, Neo4j 및 HTTP 콜백 수신기를 연결한 pytest 전체 실행에서 **112개 통과, 실패 0, 건너뜀 0**을 기록했다. 36.94초가 걸렸고 Pika 사용 중단 예정 경고가 8건 발생했다. 아래 원본은 데모 실행 기준 `4d01473`에서 수집했다. 공개 백엔드 브랜치와 해당 실행본의 `src/`, `tests/`, Compose 및 Dockerfile 내용이 일치한다. [pytest 콘솔 출력](pytest.log) · [테스트별 JUnit 결과](junit.xml)

| 공개 데모 장면 | 통합 테스트가 확인한 값 |
|---|---|
| 팀을 바꾸고 같은 질문의 근거 열기 | 플랫폼팀 전용 슬로우 쿼리는 주문팀의 후보·그래프 경로·최종 근거에서 제외되고 직접 조회하면 404다. 모델 대역에 전달한 요청을 캡처해 전용 로그 경로와 ID가 주문팀 입력에서 빠지고 플랫폼팀 입력에 포함되는지 확인한다. [검색·근거·원문 접근](../../tests/test_investigation_flow.py#L94-L127) · [모델 입력 캡처 비교](../../tests/test_investigation_flow.py#L256-L264) |
| 첫 콜백 실패 후 재전송 기록 열기 | 동일 작업 ID에서 503 → 200, 저장한 결과 동일, PostgreSQL의 분석 실행 횟수 1회. 통합 테스트에서 모델 대역 호출은 재전송 전후 3회로 같았다. 공개 재생본은 규칙 엔진으로 실행해 외부 LLM 호출이 0회다. [콜백 전후 검증](../../tests/test_investigation_flow.py#L225-L254) · [DB 실행 횟수 검증](../../tests/test_investigation_flow.py#L266-L279) |

검증 당시 Docker 저장 공간 제약으로 Python 서비스는 같은 아카이브 소스와 의존성을 읽기 전용으로 공유했다. 공개 브랜치에서 전체 스택으로 재현하려면 다음 명령을 실행한다. [서비스 구성](../../docker-compose.yml) · [CI 정의](../../.github/workflows/test.yml)

```sh
docker compose up -d --build --wait
docker compose run --build --rm test
```


원본 `db_pool_exhaustion`의 시드 42를 고정했다. 기본 512차원 어휘 해싱 임베딩으로 Milvus에서 권한 필터를 먼저 적용하고, 동일 후보를 Neo4j의 서비스·배포·장애 관계로 검증·확장했다. 실행마다 별도 컬렉션에 권한과 버전이 고정된 청크를 올려 운영 색인·테스트 업로드를 배제했다.

## 검색 측정

| 방법 | 평균 Recall@4 | MRR@4 | 정답 배포 Hit@4 | 검색 p50 | 검색 p95 |
|---|---:|---:|---:|---:|---:|
| 벡터 | 0.857 | 0.929 | 3/4 | 2.8 ms | 4.4 ms |
| Vector + Graph | 1.000 | 1.000 | 4/4 | 14.8 ms | 27.8 ms |

총 7문항, 시드 청크 70개다. 배포를 묻는 문항의 정답은 원래 시나리오의 변경 라벨에서 얻었다. 나머지 문항은 지정한 문서 버전과 원문 위치를 정답으로 채점했다. 두 조건의 상위 결과 56개에서 원문·버전·권한 메타데이터가 시드 매니페스트와 일치했다. 별도 테스트는 기존 보고서와 생성 로그·배포 기록에서 실제 인용 행을 다시 읽는다. 지연은 웜 상태에서 조건별 35회 호출의 벽시계 시간이고, 임베딩과 검색을 포함한다. 큐와 LLM 시간은 제외했다.

| 질문 | 팀 | Recall@4 벡터 → 그래프 | 정답 배포 순위 | 후보 순서 |
|---|---|---|---|---|
| 배포 이후 주문 서비스의 응답이 느려졌다. 어떤 변경이 관련돼 있나? | orders | 0.50 → 1.00 | 2 → 1 | 변경 |
| api-gateway Upstream timeout 504 exceeded 10000ms와 관련된 이번 변경은? | orders | 0.50 → 1.00 | 상위 4개 밖 → 2 | 변경 |
| HikariPool-1 Connection is not available request timed out after 30000ms 원문 | orders | 1.00 → 1.00 | 원문 검색 | 동일 |
| Slow query SELECT orders WHERE customer_ref execution time의 실제 실행 로그 | platform | 1.00 → 1.00 | 원문 검색 | 동일 |
| 왜 api-gateway Circuit breaker OPEN for order-service가 발생했는지 장애 로그와 선행 변경을 찾아줘 | platform | 1.00 → 1.00 | 4 → 2 | 변경 |
| 2025 주문 API 커넥션 풀 고갈 재발 방지 대책 EXPLAIN 자동 검토 | orders | 1.00 → 1.00 | 원문 검색 | 동일 |
| order-service v2.4.0 배포 주문 조회 쿼리 리팩터링 customer_ref 기록 | orders | 1.00 → 1.00 | 1 → 1 | 변경 |

같은 질문에 대해 정답 원문이 상위 후보에 남았는지와 결과 순서가 바뀌었는지는 별도로 표시했다. 그래프에서 연결이 확인되지 않은 과거 사례는 유사 증상으로만 취급한다. 위 측정은 단일 합성 장애에서의 결과다. [정답표](../../src/fivewhys/evaluation/retrieval_gold.json) · [문항별 원시 결과·경로·지연 샘플](retrieval-benchmark.json)

## 대표 질문에서 바뀐 근거

주문팀이 “배포 이후 주문 서비스의 응답이 느려졌다. 어떤 변경이 관련돼 있나?”라고 물었을 때 첫 벡터 후보는 Redis 재시작 뒤 지연을 기록한 과거 보고서였다. 그래프에서 이 보고서의 서비스와 현재 사건을 잇는 허용된 영향 경로를 찾지 못했다. 대신 장애 전 60분 안에 주문 서비스를 실제 변경한 13:20 KST의 `v2.4.0` 배포와 장애 구간의 DB 풀 로그가 상위 후보에 들어갔다. 이 근거는 쿼리 변경과 연결 풀 대기의 연관성을 뒷받침한다. 과거 Redis 장애 보고서는 비교 화면에서 원문을 열어 제외 이유를 확인할 수 있다.

풀 오류·슬로우 쿼리·과거 조치 원문을 직접 지정한 세 문항은 순서가 같았다. 상위 4개 재현율이 하락한 문항은 이번 일곱 문항에서 관측되지 않았다. 그래프 검증은 중앙 검색 지연을 늘렸다.

## 기능 및 실패 복구

- 기존 CLI의 5턴, 로그 2,328건, 원인 배포와 인용 6건을 원본 커밋에서 재현했다. 같은 커맨드의 개편 후 출력과 원본 텍스트를 비교했다. [기준 실행](baseline/README.md)
- 양 팀의 후보·그래프 경로·모델 입력과 결과를 조회했다. 플랫폼 전용 DB 슬로우 쿼리를 모델 입력 캡처에서도 비교했다. [HTTP 통합 테스트](../../tests/test_investigation_flow.py)
- 실제 워커에 허위 인용을 반복 제출한 작업은 수정 기회 1회, 모델 대역 호출 4회 뒤에 실패했다. 저장 결과는 NULL, 결과 조회는 HTTP 409였다. [작업 통합 테스트](../../tests/test_investigation_flow.py)
- 깨진 보고서와 허위 인용의 검증 실패, 허용된 수정 횟수, 근거 부족과 일시적 모델 장애를 검증했다. [엔진 테스트](../../tests/test_investigation_analysis.py)
- 브로커 재전달, 워커 재시작, 콜백 HTTP 503 후 200 복구, 수신 성공 직후 워커 유실을 재현했다. [워커 복구 테스트](../../tests/test_jobs_integration.py)
