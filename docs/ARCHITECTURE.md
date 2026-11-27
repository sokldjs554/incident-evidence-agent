# 아키텍처 상세

## 전체 흐름

```
[시나리오 생성기]                       [포스트모템 코퍼스 (md 9건)]
      │ LogRecord / DeployEvent               │
      ▼                                       ▼
[파서 parse_line] ──► [Drain 템플릿 마이닝] [섹션 인지 청킹]
      │                     │                 │
      ▼                     ▼                 ▼
[SQLite FTS5 저장소] ◄── template_id     [해싱 임베딩] ─► [벡터 스토어]
  logs / templates / deploys / chunks
      │
      ▼
[도구 실행기 ToolExecutor]  ◄───────── [하이브리드 검색기 (BM25+벡터+RRF)]
      ▲ tool_result
      │ tool_use
[Five-Whys 에이전트 루프] ◄── LLMClient ── AnthropicLLM │ ScriptedLLM
      │ submit_root_cause_report (pydantic 검증)
      ▼
[RCA 보고서] ─► [평가 하니스] ─► 정확도/증거 정밀도/채점 리포트
```

## 계층별 설계

### 1. 로그 계층 (`logs/`)

**parser.py** — 표준 포맷(`ISO8601 service LEVEL [component] message`)을 정규식으로
파싱하되, 포맷 불일치 라인도 버리지 않고 `unknown` 서비스로 보존한다. 실무 로그는
항상 지저분하다는 전제를 코드에 반영했다.

**drain.py** — Drain(ICWS 2017)의 단순화 구현. 3단계로 동작한다:

1. **마스킹**: 숫자·IP·UUID·hex id 등 가변 파라미터를 `<NUM>` `<IP>` 등으로 치환.
   토큰 앞뒤 문장부호를 벗겨낸 뒤 전체 일치로만 마스킹해 오탐을 줄인다.
2. **버킷팅**: `(토큰 수, 선행 depth개 토큰)`을 키로 하는 prefix tree로 비교 대상
   클러스터를 좁힌다. 선행 토큰이 마스크(`<...>`)면 키에서 `<*>`로 일반화한다.
3. **병합**: 후보와 위치별 토큰 일치율을 계산해 임계치(0.5) 이상이면 병합하고,
   불일치 위치를 `<*>`로 치환한다.

알려진 한계(테스트로 명시): 마스킹되지 않는 파라미터가 선행 depth개 토큰 안에
오면 템플릿이 분화된다. 원 논문도 같은 한계를 가지며, depth를 줄이면 완화되지만
버킷 변별력이 떨어지는 트레이드오프가 있다.

**anomaly.py** — 템플릿별 발생률을 베이스라인 구간과 장애 구간에서 비교한다.

```
score = level_weight × count_incident × log2(rate_incident / rate_baseline)
```

- 반개구간 `[start, end)` 집계로 두 구간 경계의 이중 집계를 방지
- 레벨 가중(FATAL 4 > ERROR 3 > WARN 2 > 1)으로 "평소 없던 에러"를 최상위로

### 2. 저장 계층 (`store/`)

**db.py** — SQLite 하나로 로그/템플릿/변경 이벤트/문서 청크를 모두 담는다.
전문 검색은 FTS5 가상 테이블 + `bm25()` 랭킹. 사용자 질의는 토큰화 후
`"tok" OR "tok"` 형태로 재조립해 FTS5 문법 주입(`AND`, `NEAR`, `*` 등)으로
인한 쿼리 오류를 차단한다.

**es.py** — 동일한 `search_logs` 시그니처의 Elasticsearch 어댑터.
`match` + `term`/`range` 필터로 질의를 변환한다. 어댑터 계층 덕분에 저장소를
바꿔도 에이전트 도구 코드는 변하지 않는다.

### 3. RAG 계층 (`rag/`)

- **chunker**: 포스트모템은 `## 섹션` 구조가 강하므로 섹션 경계 우선 분할.
  긴 섹션만 문단 단위로 나누고 앞 청크 꼬리를 겹친다(overlap). 섹션 제목을
  청크 본문에 포함시켜 검색 신호를 살린다.
- **embedder**: `Embedder` 프로토콜(임베딩 추상화). 기본 구현은 feature hashing
  기반 단어+문자 3-gram 벡터라이저 — 외부 모델/네트워크 의존이 없고 결정적이라
  테스트 재현성이 보장된다. built-in `hash()`는 프로세스마다 salt가 달라지므로
  md5 기반 안정 해시를 쓴다.
- **retriever**: BM25 랭킹과 벡터 랭킹을 RRF(`Σ 1/(60+rank)`)로 융합.
  점수 스케일이 다른 두 랭커를 순위만으로 결합하므로 보정이 필요 없다.

### 4. 에이전트 계층 (`agent/`)

**LLMClient 프로토콜** — 루프는 `create(system, messages, tools) → LLMResponse`
인터페이스에만 의존한다. 구현체는 둘:

- `AnthropicLLM`: Claude Messages API. 기본 모델 `claude-opus-5`
  (adaptive thinking 기본 활성 — thinking 파라미터를 보내지 않음, 샘플링 파라미터
  미사용). `stop_reason == "refusal"`(안전 분류기 거절)을 명시적으로 처리한다.
- `ScriptedLLM`: SRE의 표준 분석 절차를 상태 기계로 코드화한 결정적 엔진.
  ① 타임라인 → ② 이상 템플릿 → ③ 변경 이력 → ④ 과거 장애 검색 → ⑤ 보고서.
  근본 원인 선택 휴리스틱: "가장 먼저 ERROR가 시작된 서비스"를 기본으로 하되,
  장애 전 2시간 내 변경이 있고 이상 로그가 있는 서비스를 우선한다.

**수동 tool-use 루프** (`five_whys.py`) — Messages API 규약을 정확히 따른다:

- assistant 응답의 `content` 전체를 그대로 히스토리에 추가 (thinking 블록 보존)
- 한 턴의 모든 `tool_use`에 대한 `tool_result`를 **하나의** user 메시지로 반환
- `submit_root_cause_report`는 실행기 대신 루프가 가로채 pydantic으로 검증하고,
  실패 시 `is_error` tool_result로 오류를 돌려줘 모델이 자가 수정하게 한다
- 도구 없이 턴이 끝나면 제출을 촉구(nudge, 최대 2회), 초과 시 명시적 실패

**도구 7종** — description에 "언제 호출해야 하는가"까지 명시한다 (모델의 도구
선택 품질은 description이 좌우한다):

| 도구 | 역할 |
|---|---|
| `get_error_timeline` | 서비스×레벨×시간버킷 에러 집계 — 전파 순서 파악 |
| `get_anomalous_templates` | 급증 템플릿 목록 — "평소 없던 패턴" 발견 |
| `search_logs` | BM25 전문 검색 — 증거 로그 id 확보 |
| `get_log_context` | 특정 로그 전후 문맥 — 선행 사건 확인 |
| `list_change_events` | 배포/설정 변경 이력 — 트리거 특정 |
| `search_past_incidents` | 포스트모템 하이브리드 RAG — 대책 참고 |
| `submit_root_cause_report` | 최종 보고서 제출 (종결 도구) |

### 5. MCP 서버 (`mcp_server/`)

에이전트 내부 도구와 같은 저장소 계층을 공유하되, 노출 표면만 MCP 표준으로
바꾼 서버다. Claude Desktop/Code 등 MCP 호스트에 붙이면 어떤 LLM 앱에서든
같은 로그 분석 도구를 쓸 수 있다. mcp 1.x(FastMCP)/2.x(MCPServer)를 모두 지원한다.

### 6. 평가 계층 (`evaluation/`)

원칙: **검증 가능한 것은 규칙으로, 주관적인 것만 LLM-judge로.**

- 규칙 채점: 근본원인 서비스 일치, 키워드 포함, 원인 변경 식별,
  증거 인용 정밀도(인용 log id의 실존 + 장애 구간 포함 검증)
- LLM-judge(선택): 인과 사슬의 논리적 일관성을 구조화 출력(json_schema)으로
  1~5점 채점

자세한 방법론은 [EVALUATION.md](EVALUATION.md) 참고.
