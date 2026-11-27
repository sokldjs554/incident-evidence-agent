"""LLM-as-judge (선택 사항, ANTHROPIC_API_KEY 필요).

규칙 기반 채점이 못 보는 '인과 사슬의 논리적 일관성'을 구조화 출력
(output_config.format = json_schema)으로 1~5점 채점한다.
"""

from __future__ import annotations

import json
import os

from fivewhys.models import RCAReport
from fivewhys.scenarios.generator import ScenarioBundle

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "coherence_score": {
            "type": "integer",
            "description": "인과 사슬의 논리적 일관성 1(비약 심함)~5(완벽)",
        },
        "matches_ground_truth": {
            "type": "boolean",
            "description": "보고서의 인과 사슬이 정답 사슬과 본질적으로 일치하는가",
        },
        "rationale": {"type": "string", "description": "채점 근거 (2~3문장, 한국어)"},
    },
    "required": ["coherence_score", "matches_ground_truth", "rationale"],
    "additionalProperties": False,
}


def judge_report(report: RCAReport, bundle: ScenarioBundle, model: str | None = None) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    model = model or os.environ.get("FIVEWHYS_MODEL", "claude-opus-5")

    gt_chain = "\n".join(f"- {s}" for s in bundle.ground_truth.causal_chain)
    prompt = f"""다음은 장애 근본원인분석 보고서와, 시나리오 설계자가 정의한 정답 인과 사슬이다.

[정답 인과 사슬]
{gt_chain}

[제출된 보고서]
{report.model_dump_json(indent=2)}

보고서의 5 Whys 체인이 논리적으로 일관된지, 정답 사슬과 본질적으로 일치하는지 채점하라."""

    # claude-opus-5는 adaptive thinking이 기본 활성이고 thinking 토큰도
    # max_tokens에 포함되므로 넉넉히 잡는다
    resp = client.messages.create(
        model=model,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": _JUDGE_SCHEMA}},
    )
    # 구조화 출력은 응답이 정상 종료됐을 때만 스키마가 보장된다
    if resp.stop_reason == "refusal":
        raise RuntimeError("판정 요청이 안전 분류기에 의해 거절되었습니다 (refusal).")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("판정 응답이 max_tokens로 잘렸습니다. max_tokens를 늘리세요.")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError(f"응답에 text 블록이 없습니다 (stop_reason={resp.stop_reason}).")
    return json.loads(text)
