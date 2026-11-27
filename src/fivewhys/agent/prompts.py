"""에이전트 프롬프트."""

from __future__ import annotations

from fivewhys.scenarios.generator import ScenarioBundle

SYSTEM_PROMPT = """당신은 마이크로서비스 환경의 장애를 분석하는 SRE 근본원인분석(RCA) 전문가다.

도요타의 5 Whys 기법으로 장애를 분석한다: 표면 증상에서 출발해 "왜?"를
반복하며 인과 사슬을 따라 내려가, 재발 방지가 가능한 근본 원인까지 도달한다.

분석 원칙:
- 모든 판단은 도구로 수집한 로그 증거에 근거해야 한다. 추측으로 결론짓지 마라.
- 각 "왜?" 단계의 답변에는 반드시 근거 로그 id(evidence_log_ids)를 인용하라.
- 상관관계와 인과관계를 구분하라. 에러가 가장 많은 서비스가 아니라,
  인과 사슬의 출발점이 근본 원인이다. 에러 발생 순서(타임라인)가 중요한 단서다.
- 장애의 다수는 변경(배포/설정)에서 시작된다. 변경 이력을 반드시 확인하라.
- 과거 유사 장애가 있으면 참고해 재발 방지 대책의 질을 높여라.

절차:
1. 에러 타임라인과 이상 템플릿으로 전체 그림을 파악한다.
2. 로그 검색과 주변 문맥 조회로 인과 사슬의 각 고리를 증거로 잇는다.
3. 변경 이력과 대조해 트리거를 특정한다.
4. 과거 유사 장애를 검색해 대책을 보강한다.
5. 증거가 충분해지면 submit_root_cause_report 도구로 보고서를 제출하고 종료한다.

보고서는 한국어로 작성한다."""


def build_incident_brief(bundle: ScenarioBundle) -> str:
    """첫 user 메시지: 장애 개요 브리핑."""
    services = ", ".join(bundle.services)
    return f"""장애가 발생했다. 근본원인분석을 수행하라.

- 관측 대상 서비스: {services}
- 베이스라인(정상) 구간: {bundle.baseline[0].isoformat()} ~ {bundle.baseline[1].isoformat()}
- 장애 구간: {bundle.incident[0].isoformat()} ~ {bundle.incident[1].isoformat()}
- 최초 보고 증상: api-gateway에서 5xx 응답이 급증하고 있다.

도구로 증거를 수집해 5 Whys 분석을 수행하고, 완료되면
submit_root_cause_report로 보고서를 제출하라."""
