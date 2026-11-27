"""ScriptedLLM — 결정적 휴리스틱 RCA 엔진 (오프라인).

LLMClient 프로토콜을 구현하지만 실제 LLM이 아니라, SRE가 손으로 하던
분석 절차를 그대로 코드로 옮긴 상태 기계다:

  ① 에러 타임라인 → ② 이상 템플릿 → ③ 변경 이력 → ④ 과거 유사 장애 → ⑤ 보고서

용도:
- API 키 없는 오프라인 데모
- 에이전트 루프의 결정적 단위/통합 테스트
- 평가(Evaluation)에서 LLM 에이전트와 비교할 베이스라인

휴리스틱 요지: "장애 구간에서 가장 먼저 ERROR가 시작된 서비스"를 후보로
하되, 장애 전 2시간 내 변경(배포/설정)이 있고 그 서비스에 이상 로그가
있으면 그 변경 서비스를 우선한다 (장애의 다수는 변경에서 시작된다는 사전
지식의 코드화).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fivewhys.agent.llm import LLMResponse, TextBlock, ToolUseBlock
from fivewhys.agent.tools import SUBMIT_TOOL

_CHANGE_LOOKBACK = timedelta(hours=2)

_REMEDIATION_RULES: list[tuple[tuple[str, ...], list[str]]] = [
    (
        ("connection", "pool", "slow query", "hikari"),
        ["원인 배포 롤백 및 신규 쿼리에 인덱스 추가", "커넥션 풀 사용률 임계 알람 추가", "슬로우 쿼리 자동 감지 및 배포 게이트 연동"],
    ),
    (
        ("outofmemory", "heap", "gc", "oomkiller"),
        ["원인 배포 롤백 후 메모리 프로파일링 수행", "힙/컨테이너 메모리 사용률 알람 추가", "부하 테스트에 메모리 시나리오 포함"],
    ),
    (
        ("space", "disk"),
        ["로그 로테이션 설정 원복 및 디스크 긴급 정리", "디스크 용량 임계 알람 추가", "로그 보존 정책 변경 시 용량 영향 리뷰 절차 도입"],
    ),
    (
        ("certificate", "ssl", "tls"),
        ["만료 인증서 즉시 갱신", "인증서 만료 D-30/D-7 모니터링 알람 도입", "인증서 자동 갱신(ACME) 전환 검토"],
    ),
    (
        ("redis", "cache", "stampede"),
        ["캐시 워밍 후 점진적 재시작 절차 수립", "캐시 TTL 지터 적용으로 스탬피드 방지", "DB 커넥션 상한 보호(서킷브레이커) 적용"],
    ),
]

_DEFAULT_REMEDIATION = ["원인 변경 롤백", "관련 핵심 지표 알람 추가", "장애 대응 런북 갱신"]

_ERROR_LEVELS = {"ERROR", "FATAL"}


def _words(text: str, limit: int = 6) -> list[str]:
    return re.findall(r"[A-Za-z가-힣]{4,}", text)[:limit]


@dataclass
class ScriptedLLM:
    """결정적 스크립트 엔진. 상태 기계 단계별로 도구 호출을 생성한다."""

    confidence: float = 0.6
    _step: int = 0
    _timeline: list[dict] = field(default_factory=list)
    _anomalies: list[dict] = field(default_factory=list)
    _changes: list[dict] = field(default_factory=list)
    _past: list[dict] = field(default_factory=list)
    _incident_start: str = ""

    # ------------------------------------------------------------ 결과 흡수

    def _absorb(self, content: str) -> None:
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(data, dict) and "timeline" in data:
            self._timeline = data["timeline"]
            self._incident_start = data.get("incident_window", ["", ""])[0]
        elif isinstance(data, list) and data and "template" in data[0]:
            self._anomalies = data
        elif isinstance(data, list) and data and "kind" in data[0]:
            self._changes = data
        elif isinstance(data, list) and data and "doc" in data[0]:
            self._past = data

    # ------------------------------------------------------------ 휴리스틱

    def _error_onsets(self) -> dict[str, float]:
        onsets: dict[str, float] = {}
        for row in self._timeline:
            if row["level"] in _ERROR_LEVELS:
                cur = onsets.get(row["service"])
                if cur is None or row["window_start_min"] < cur:
                    onsets[row["service"]] = row["window_start_min"]
        return onsets

    def _top_template(self, service: str) -> dict | None:
        for a in self._anomalies:  # 점수 내림차순 정렬 상태
            if a["service"] == service:
                return a
        return None

    def _recent_changes(self) -> list[dict]:
        if not self._incident_start:
            return []
        try:
            incident_start = datetime.fromisoformat(self._incident_start)
        except ValueError:
            return []
        out = []
        for ch in self._changes:
            try:
                ts = datetime.fromisoformat(ch["ts"])
            except (KeyError, ValueError):
                continue
            if timedelta(0) <= incident_start - ts <= _CHANGE_LOOKBACK:
                out.append(ch)
        return out

    def _pick_root_cause(self) -> tuple[str, dict | None]:
        """(root_cause_service, culprit_change|None)를 결정한다."""
        onsets = self._error_onsets()
        anomalous_services = {a["service"] for a in self._anomalies}

        candidates = [
            ch
            for ch in self._recent_changes()
            if ch["service"] in onsets or ch["service"] in anomalous_services
        ]
        if candidates:
            candidates.sort(key=lambda ch: onsets.get(ch["service"], 1e9))
            return candidates[0]["service"], candidates[0]

        if onsets:
            service = min(onsets, key=lambda s: (onsets[s], s))
            return service, None
        if self._anomalies:
            return self._anomalies[0]["service"], None
        return "unknown", None

    def _build_report(self) -> dict:
        root_service, culprit = self._pick_root_cause()
        onsets = self._error_onsets()

        # 증상(하류) → 근본 원인(상류) 순서로 왜? 체인 구성
        ordered = sorted(onsets, key=lambda s: -onsets[s])  # 늦게 시작된 서비스부터
        five_whys: list[dict] = []
        prev = "사용자 요청"
        for service in ordered:
            top = self._top_template(service)
            answer = (
                f"{service}에서 '{top['template']}' 패턴이 급증했다 "
                f"(장애 구간 {top['count_incident']}건, 베이스라인 {top['count_baseline']}건)."
                if top
                else f"{service}에서 에러가 급증했다."
            )
            five_whys.append(
                {
                    "question": f"왜 {prev}에 문제가 생겼는가?",
                    "answer": answer,
                    "evidence_log_ids": top["example_log_ids"] if top else [],
                }
            )
            prev = f"{service}의 처리"
        if culprit:
            five_whys.append(
                {
                    "question": f"왜 {root_service}에서 이 문제가 시작됐는가?",
                    "answer": f"장애 직전 변경이 있었다: {culprit['description']} ({culprit['ts']})",
                    "evidence_log_ids": [],
                }
            )
        while len(five_whys) < 3:
            five_whys.append(
                {
                    "question": "왜 이 문제를 사전에 탐지하지 못했는가?",
                    "answer": "해당 패턴에 대한 사전 알람/모니터링이 없었다.",
                    "evidence_log_ids": [],
                }
            )

        root_top = self._top_template(root_service)
        root_desc = f"{root_service}에서 '{root_top['template']}' 패턴이 장애의 출발점이다." if root_top else f"{root_service}가 장애의 출발점이다."
        if culprit:
            root_desc += f" 트리거는 변경 '{culprit['description']}'로 판단된다."

        blob = " ".join(
            [root_desc] + [a["template"] for a in self._anomalies if a["service"] == root_service]
        ).lower()
        remediation = _DEFAULT_REMEDIATION
        for keywords, actions in _REMEDIATION_RULES:
            if any(k in blob for k in keywords):
                remediation = actions
                break

        symptom = self._top_template("api-gateway")
        summary = (
            f"장애 구간에 api-gateway에서 '{symptom['template']}' 등 오류 응답이 급증했다. "
            if symptom
            else "장애 구간에 게이트웨이 오류 응답이 급증했다. "
        ) + f"인과 추적 결과 출발점은 {root_service}로 판단된다."

        return {
            "summary": summary,
            "five_whys": five_whys,
            "root_cause": root_desc,
            "root_cause_service": root_service,
            "culprit_change": culprit["description"] if culprit else None,
            "remediation": remediation,
            "similar_incident": self._past[0]["doc"] if self._past else None,
            "confidence": self.confidence,
        }

    # ------------------------------------------------------------ LLMClient

    def _reset(self) -> None:
        self._step = 0
        self._timeline = []
        self._anomalies = []
        self._changes = []
        self._past = []
        self._incident_start = ""

    def create(
        self, *, system: str, messages: list[dict], tools: list[dict]
    ) -> LLMResponse:
        # 새 대화(첫 user 메시지 하나뿐)면 상태를 초기화해 인스턴스 재사용을 허용
        if len(messages) == 1:
            self._reset()
        last = messages[-1] if messages else {}
        content = last.get("content")
        if isinstance(content, list):
            for tr in content:
                if isinstance(tr, dict) and tr.get("type") == "tool_result" and not tr.get("is_error"):
                    self._absorb(tr.get("content", ""))

        self._step += 1
        step = self._step

        def call(name: str, tool_input: dict, note: str) -> LLMResponse:
            return LLMResponse(
                content=[
                    TextBlock(text=note),
                    ToolUseBlock(id=f"scripted-{step}", name=name, input=tool_input),
                ],
                stop_reason="tool_use",
                model="scripted-heuristic",
            )

        if step == 1:
            return call("get_error_timeline", {"bucket_minutes": 2}, "에러 전파 순서를 파악한다.")
        if step == 2:
            return call("get_anomalous_templates", {"top_k": 10}, "평소에 없던 로그 패턴을 찾는다.")
        if step == 3:
            return call("list_change_events", {}, "장애 직전 변경이 있었는지 확인한다.")
        if step == 4:
            root_service, _ = self._pick_root_cause()
            top = self._top_template(root_service)
            query_words = _words(top["template"]) if top else []
            query = " ".join([root_service, *query_words]) or "incident"
            return call(
                "search_past_incidents",
                {"query": query, "k": 4},
                "과거 유사 장애를 참고한다.",
            )
        if step == 5:
            return call(SUBMIT_TOOL, self._build_report(), "분석을 종합해 보고서를 제출한다.")

        # 제출 이후 재호출되면 종료 신호
        return LLMResponse(content=[TextBlock(text="분석 완료.")], stop_reason="end_turn")
