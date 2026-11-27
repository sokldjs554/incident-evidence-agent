"""ScriptedLLM으로 에이전트 루프 전체를 오프라인 검증한다."""

import pytest

from fivewhys.agent.five_whys import AgentError, run_five_whys_agent
from fivewhys.agent.llm import LLMResponse, TextBlock
from fivewhys.agent.scripted import ScriptedLLM
from fivewhys.agent.tools import ToolExecutor, build_tool_definitions
from fivewhys.pipeline import build_retriever, build_store
from fivewhys.scenarios.catalog import get_scenario


def _run(name, seed=42):
    bundle = get_scenario(name, seed)
    store, _ = build_store(bundle)
    retriever = build_retriever(store)
    executor = ToolExecutor.for_bundle(bundle, store, retriever)
    result = run_five_whys_agent(bundle, executor, ScriptedLLM())
    return bundle, store, result


def test_agent_completes_and_report_is_valid():
    bundle, store, result = _run("db_pool_exhaustion")
    report = result.report
    assert len(report.five_whys) >= 3
    assert report.remediation
    assert 0.0 <= report.confidence <= 1.0
    assert result.n_turns >= 4  # 증거 수집 턴들을 실제로 거쳤는지
    store.close()


def test_agent_identifies_deploy_caused_root_service():
    bundle, store, result = _run("db_pool_exhaustion")
    assert result.report.root_cause_service == "order-service"
    assert result.report.culprit_change and "v2.4.0" in result.report.culprit_change
    store.close()


def test_agent_handles_no_deploy_scenario():
    bundle, store, result = _run("cert_expiry")
    assert result.report.root_cause_service == "user-service"
    assert result.report.culprit_change is None
    store.close()


def test_evidence_ids_point_to_real_incident_logs():
    bundle, store, result = _run("memory_leak")
    i_start, i_end = bundle.incident
    cited = [i for step in result.report.five_whys for i in step.evidence_log_ids]
    assert cited, "증거 인용이 있어야 한다"
    for log_id in cited:
        row = store.get_log(log_id)
        assert row is not None
        assert i_start.timestamp() <= row["epoch"] <= i_end.timestamp()
    store.close()


def test_tool_definitions_schema_sanity():
    tools = build_tool_definitions()
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))
    for t in tools:
        assert t["description"]
        assert t["input_schema"]["type"] == "object"


def test_loop_raises_when_llm_never_submits():
    class SilentLLM:
        def create(self, *, system, messages, tools):
            return LLMResponse(content=[TextBlock(text="...")], stop_reason="end_turn")

    bundle = get_scenario("db_pool_exhaustion")
    store, _ = build_store(bundle)
    retriever = build_retriever(store)
    executor = ToolExecutor.for_bundle(bundle, store, retriever)
    with pytest.raises(AgentError):
        run_five_whys_agent(bundle, executor, SilentLLM())
    store.close()
