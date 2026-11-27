"""평가 하니스.

정답(GroundTruth)이 있는 시나리오에서 에이전트를 실행하고 규칙 기반으로
채점한다. LLM 출력 평가를 전부 LLM-judge에 맡기지 않고, 검증 가능한 것은
결정적 규칙으로 채점하는 것이 원칙이다:

- root_service_correct : 근본 원인 서비스 정확히 일치 (0/1)
- root_cause_keyword_hit: 근본 원인 서술에 정답 키워드 포함 (0/1)
- culprit_identified   : 원인 변경(배포/설정) 식별 (해당 시나리오만, 0/1)
- evidence_precision   : 인용한 로그 id 중 실제 존재하며 장애 구간에
                          속하는 비율 (환각 인용 탐지, 0~1). 인용이 하나도
                          없으면 None — "인용 안 함"과 "전부 환각"을 구분한다
- whys_depth           : 왜? 체인 길이

주관적 품질(체인의 논리적 일관성)만 선택적으로 LLM-judge(judge.py)에 맡긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Callable

from fivewhys.agent.five_whys import run_five_whys_agent
from fivewhys.agent.llm import LLMClient
from fivewhys.agent.scripted import ScriptedLLM
from fivewhys.agent.tools import ToolExecutor
from fivewhys.models import RCAReport
from fivewhys.pipeline import build_retriever, build_store
from fivewhys.scenarios.catalog import SCENARIOS, get_scenario
from fivewhys.scenarios.generator import ScenarioBundle
from fivewhys.store.db import LogStore


@dataclass
class EvalResult:
    scenario: str
    seed: int
    root_service_correct: bool
    root_cause_keyword_hit: bool
    culprit_applicable: bool
    culprit_identified: bool
    evidence_precision: float | None  # None == 인용 자체가 없음
    evidence_count: int
    whys_depth: int
    n_turns: int
    predicted_service: str
    error: str | None = None


@dataclass
class EvalSummary:
    engine: str
    results: list[EvalResult] = field(default_factory=list)

    @property
    def root_service_accuracy(self) -> float:
        ok = [r for r in self.results if r.error is None]
        return mean(r.root_service_correct for r in ok) if ok else 0.0

    @property
    def keyword_accuracy(self) -> float:
        ok = [r for r in self.results if r.error is None]
        return mean(r.root_cause_keyword_hit for r in ok) if ok else 0.0

    @property
    def culprit_accuracy(self) -> float:
        ok = [r for r in self.results if r.error is None and r.culprit_applicable]
        return mean(r.culprit_identified for r in ok) if ok else 0.0

    @property
    def mean_evidence_precision(self) -> float:
        ok = [
            r.evidence_precision
            for r in self.results
            if r.error is None and r.evidence_precision is not None
        ]
        return mean(ok) if ok else 0.0

    @property
    def no_citation_rate(self) -> float:
        ok = [r for r in self.results if r.error is None]
        return mean(r.evidence_precision is None for r in ok) if ok else 0.0


def score_report(
    report: RCAReport, bundle: ScenarioBundle, store: LogStore
) -> dict:
    gt = bundle.ground_truth

    root_service_correct = report.root_cause_service.strip() == gt.root_cause_service

    text = f"{report.root_cause} {report.summary}".lower()
    keyword_hit = any(k.lower() in text for k in gt.root_cause_keywords)

    culprit_applicable = gt.culprit_change_match is not None
    culprit_identified = False
    if culprit_applicable and report.culprit_change:
        culprit_identified = gt.culprit_change_match.lower() in report.culprit_change.lower()

    cited: list[int] = []
    for step in report.five_whys:
        cited.extend(step.evidence_log_ids)
    valid = 0
    i_start, i_end = bundle.incident
    for log_id in cited:
        row = store.get_log(log_id)
        if row and i_start.timestamp() <= row["epoch"] <= i_end.timestamp():
            valid += 1
    evidence_precision = round(valid / len(cited), 3) if cited else None

    return {
        "root_service_correct": root_service_correct,
        "root_cause_keyword_hit": keyword_hit,
        "culprit_applicable": culprit_applicable,
        "culprit_identified": culprit_identified,
        "evidence_precision": evidence_precision,
        "evidence_count": len(cited),
        "whys_depth": len(report.five_whys),
        "predicted_service": report.root_cause_service,
    }


def _make_llm(engine: str) -> LLMClient:
    if engine == "scripted":
        return ScriptedLLM()
    if engine == "claude":
        from fivewhys.agent.llm import AnthropicLLM

        return AnthropicLLM()
    raise ValueError(f"알 수 없는 엔진: {engine}")


def evaluate(
    engine: str = "scripted",
    scenario_names: list[str] | None = None,
    seeds: tuple[int, ...] = (42,),
    llm_factory: Callable[[], LLMClient] | None = None,
) -> EvalSummary:
    """llm_factory: 실행(run)마다 새 LLMClient를 만들어야 상태가 섞이지 않는다."""
    summary = EvalSummary(engine=engine)
    for name in scenario_names or list(SCENARIOS):
        for seed in seeds:
            bundle = get_scenario(name, seed)
            store, _ = build_store(bundle)
            retriever = build_retriever(store)
            executor = ToolExecutor.for_bundle(bundle, store, retriever)
            agent_llm = llm_factory() if llm_factory is not None else _make_llm(engine)
            try:
                result = run_five_whys_agent(bundle, executor, agent_llm)
                scores = score_report(result.report, bundle, store)
                summary.results.append(
                    EvalResult(
                        scenario=name, seed=seed, n_turns=result.n_turns, **scores
                    )
                )
            except Exception as e:
                summary.results.append(
                    EvalResult(
                        scenario=name,
                        seed=seed,
                        root_service_correct=False,
                        root_cause_keyword_hit=False,
                        culprit_applicable=False,
                        culprit_identified=False,
                        evidence_precision=None,
                        evidence_count=0,
                        whys_depth=0,
                        n_turns=0,
                        predicted_service="",
                        error=f"{type(e).__name__}: {e}",
                    )
                )
            finally:
                store.close()
    return summary


def render_markdown(summary: EvalSummary) -> str:
    lines = [
        f"### 평가 결과 — engine: `{summary.engine}`",
        "",
        "| 시나리오 | seed | 근본원인 서비스 | 키워드 | 원인 변경 | 증거 정밀도 | 인용 수 | 체인 깊이 | 턴 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in summary.results:
        if r.error:
            lines.append(f"| {r.scenario} | {r.seed} | 오류: {r.error} | | | | | | |")
            continue
        culprit = ("✅" if r.culprit_identified else "❌") if r.culprit_applicable else "—"
        precision = "—" if r.evidence_precision is None else f"{r.evidence_precision:.2f}"
        lines.append(
            f"| {r.scenario} | {r.seed} "
            f"| {'✅' if r.root_service_correct else '❌ (' + r.predicted_service + ')'} "
            f"| {'✅' if r.root_cause_keyword_hit else '❌'} "
            f"| {culprit} "
            f"| {precision} | {r.evidence_count} | {r.whys_depth} | {r.n_turns} |"
        )
    lines += [
        "",
        f"- 근본원인 서비스 정확도: **{summary.root_service_accuracy:.0%}**",
        f"- 근본원인 키워드 정확도: **{summary.keyword_accuracy:.0%}**",
        f"- 원인 변경 식별 정확도: **{summary.culprit_accuracy:.0%}** (해당 시나리오 한정)",
        f"- 평균 증거 정밀도: **{summary.mean_evidence_precision:.2f}**"
        f" (인용 없는 보고서 비율: {summary.no_citation_rate:.0%})",
    ]
    return "\n".join(lines)
