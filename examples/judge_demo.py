"""LLM-as-judge 실습 — 규칙 채점이 못 보는 '논리적 일관성'을 채점한다.

평가 하니스(evaluation/harness.py)는 검증 가능한 항목만 규칙으로 채점한다:
근본원인 서비스가 정답과 같은가, 인용한 로그 id가 실제로 존재하는가 등.
하지만 "왜? 체인이 논리적으로 말이 되는가"는 규칙으로 판정하기 어렵다.
그 부분만 LLM에게 구조화 출력(json_schema)으로 1~5점 채점을 맡긴다.

사전 준비:
    export ANTHROPIC_API_KEY=sk-ant-...

실행:
    python examples/judge_demo.py                 # 오프라인 엔진 보고서를 채점
    python examples/judge_demo.py --engine claude # Claude 에이전트 보고서를 채점
"""

from __future__ import annotations

import argparse
import os
import sys

from fivewhys.agent.five_whys import run_five_whys_agent
from fivewhys.agent.scripted import ScriptedLLM
from fivewhys.agent.tools import ToolExecutor
from fivewhys.evaluation.harness import score_report
from fivewhys.evaluation.judge import judge_report
from fivewhys.pipeline import build_retriever, build_store
from fivewhys.scenarios.catalog import get_scenario


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM-as-judge 실습")
    parser.add_argument("--scenario", default="db_pool_exhaustion")
    parser.add_argument("--engine", choices=["scripted", "claude"], default="scripted")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 환경변수가 필요합니다 (판정에 Claude를 호출합니다).")
        return 1

    # 1) 보고서 생성
    bundle = get_scenario(args.scenario)
    store, _ = build_store(bundle)
    retriever = build_retriever(store)
    executor = ToolExecutor.for_bundle(bundle, store, retriever)

    if args.engine == "claude":
        from fivewhys.agent.llm import AnthropicLLM

        llm = AnthropicLLM()
    else:
        llm = ScriptedLLM()

    print(f"시나리오: {bundle.title} · 엔진: {args.engine}\n분석 중...")
    result = run_five_whys_agent(bundle, executor, llm)

    # 2) 규칙 기반 채점 (결정적, 비용 0)
    print("\n── 규칙 기반 채점")
    for k, v in score_report(result.report, bundle, store).items():
        print(f"   {k}: {v}")

    # 3) LLM 판정 (주관적 항목)
    print("\n── LLM 판정 (인과 사슬 일관성)")
    try:
        verdict = judge_report(result.report, bundle)
    except Exception as e:
        print(f"   판정 실패: {type(e).__name__}: {e}")
        store.close()
        return 1

    print(f"   일관성 점수: {verdict['coherence_score']} / 5")
    print(f"   정답 사슬과 일치: {verdict['matches_ground_truth']}")
    print(f"   근거: {verdict['rationale']}")

    print(
        "\n핵심: 검증 가능한 것은 규칙으로, 주관적인 것만 LLM으로 —\n"
        "      평가 전체를 LLM에 맡기지 않는 것이 이 하니스의 설계 원칙이다."
    )
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
