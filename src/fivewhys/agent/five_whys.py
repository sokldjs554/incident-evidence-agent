"""Five-Whys 에이전트 루프.

수동 tool-use 루프를 직접 구현했다. SDK의 tool runner 헬퍼 대신 수동 루프를
쓴 이유:
- LLMClient 프로토콜 뒤에 ScriptedLLM(오프라인 엔진)을 끼워 API 키 없이
  전체 루프를 테스트/평가할 수 있어야 한다.
- 제출 도구(submit_root_cause_report)를 가로채 pydantic으로 검증하고,
  실패 시 오류를 tool_result로 되돌려 모델이 스스로 고치게 하는 제어가 필요하다.

루프 규칙 (Claude Messages API 규약):
- assistant 응답의 content 전체를 그대로 히스토리에 추가한다
  (thinking 블록 포함 — 변형하면 안 된다).
- 한 턴의 모든 tool_use 블록에 대한 tool_result를 하나의 user 메시지로
  묶어 보낸다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from fivewhys.agent.llm import LLMClient
from fivewhys.agent.prompts import SYSTEM_PROMPT, build_incident_brief
from fivewhys.agent.tools import SUBMIT_TOOL, ToolExecutor, build_tool_definitions
from fivewhys.models import RCAReport
from fivewhys.scenarios.generator import ScenarioBundle


Report = TypeVar("Report")


@dataclass
class AgentResult(Generic[Report]):
    report: Report
    n_turns: int
    tool_calls: list[dict] = field(default_factory=list)


class AgentError(RuntimeError):
    pass


class MixedSubmission(ValueError):
    pass


def run_five_whys_agent(
    bundle: ScenarioBundle,
    executor: ToolExecutor,
    llm: LLMClient,
    max_turns: int = 24,
) -> AgentResult[RCAReport]:
    return run_tool_loop(
        llm=llm, system=SYSTEM_PROMPT, brief=build_incident_brief(bundle),
        tools=build_tool_definitions(), execute=executor.execute,
        submit_tool=SUBMIT_TOOL, validate=RCAReport.model_validate, max_turns=max_turns,
    )


def run_tool_loop(
    *, llm: LLMClient, system: str, brief: str, tools: list[dict],
    execute: Callable[[str, dict], tuple[str, bool]], submit_tool: str,
    validate: Callable[[dict], Report], max_turns: int, max_repairs: int | None = None,
    submit_separately: bool = False,
    observe_result: Callable[[int, int, str, str | Exception | None], None] | None = None,
) -> AgentResult[Report]:
    """Run the shared Messages API dialogue, retaining opaque assistant blocks."""
    messages: list[dict] = [{"role": "user", "content": brief}]
    invalid_submissions = 0
    tool_calls: list[dict] = []
    nudges = 0

    for turn in range(1, max_turns + 1):
        response = llm.create(system=system, messages=messages, tools=tools)

        tool_blocks = [b for b in response.content if getattr(b, "type", "") == "tool_use"]

        if not tool_blocks:
            # 도구 없이 턴을 끝냄 → 보고서 제출을 촉구 (최대 2회)
            if response.stop_reason == "max_tokens":
                raise AgentError("응답이 max_tokens로 잘렸습니다. max_tokens를 늘리세요.")
            nudges += 1
            if nudges > 2:
                raise AgentError("에이전트가 보고서를 제출하지 않고 종료했습니다.")
            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {
                    "role": "user",
                    "content": f"분석을 마쳤다면 {submit_tool} 도구로 보고서를 제출하라.",
                }
            )
            continue

        messages.append({"role": "assistant", "content": response.content})
        nudges = 0  # 촉구는 '연속' 무도구 턴에만 누적한다

        tool_results: list[dict] = []
        submitted: Report | None = None

        for index, block in enumerate(tool_blocks):
            tool_calls.append({"turn": turn, "name": block.name, "input": block.input})

            if block.name == submit_tool:
                try:
                    if submit_separately and len(tool_blocks) != 1:
                        raise MixedSubmission("검색 결과를 읽은 다음 별도 턴에서 제출하세요.")
                    submitted = validate(block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "보고서가 접수되었습니다.",
                        }
                    )
                except Exception as e:
                    if observe_result:
                        observe_result(turn, index, "rejected", e)
                    if not isinstance(e, ValueError):
                        raise
                    invalid_submissions += 1
                    if max_repairs is not None and invalid_submissions > max_repairs:
                        raise AgentError("보고서 검증 오류가 반복되었습니다.") from e
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"보고서 스키마 오류를 수정해 다시 제출하세요: {e}",
                            "is_error": True,
                        }
                    )
                else:
                    if observe_result:
                        observe_result(turn, index, "accepted", None)
                continue

            try:
                content, is_error = execute(block.name, block.input)
            except Exception as exc:
                if observe_result:
                    observe_result(turn, index, "error", exc)
                raise
            if observe_result:
                observe_result(turn, index, "error" if is_error else "returned", content)
            result: dict = {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
            }
            if is_error:
                result["is_error"] = True
            tool_results.append(result)

        # 규약: 한 턴의 모든 tool_result는 하나의 user 메시지로
        messages.append({"role": "user", "content": tool_results})

        if submitted is not None:
            return AgentResult(report=submitted, n_turns=turn, tool_calls=tool_calls)

    raise AgentError(f"max_turns({max_turns}) 안에 보고서가 제출되지 않았습니다.")
