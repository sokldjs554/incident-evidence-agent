"""Job investigators use only evidence returned by their team's tools."""

import json
from contextlib import nullcontext

import pytest

from fivewhys.agent.llm import LLMResponse, ToolUseBlock


def row(id_, kind, text, teams=("orders", "platform"), service="cart"):
    return dict(id=id_, kind=kind, text=text, title=text, teams=list(teams),
                origin="scenario" if kind != "report" else "archive", incident="db_pool_exhaustion/seed-42",
                version="test", source="test-fixture", locator=id_, service=service)


ROWS = [
    row("log-9", "log", "HikariPool connection timed out"),
    row("dep-7", "deployment", "cart v7 changed lookup query"),
    row("rep-3", "report", "과거 cart 커넥션 풀 고갈 사례"),
    row("restricted-5", "log", "DB-PRIVATE-CANARY", teams=("platform",)),
]


def result_with_paths(rows):
    relations = {"log": "OBSERVED_ON", "deployment": "CHANGED", "report": "DESCRIBES"}
    paths = []
    for r in rows:
        relation = relations[r["kind"]]
        paths.append(dict(evidence_id=r["id"], nodes=[r["id"], r["service"]], relations=[relation],
                          edges=[dict(source=r["id"], target=r["service"], type=relation)]))
    return dict(vector=rows, graph=rows, paths=paths)


@pytest.fixture
def analysis(monkeypatch):
    from fivewhys.investigation import analysis

    def search(question, team, kind=None, **kwargs):
        # Deliberately include a disallowed candidate to test the engine's ACL boundary.
        rows = [r for r in ROWS if kind is None or r["kind"] == kind]
        return result_with_paths(rows)

    monkeypatch.setattr(analysis, "research", search)
    return analysis


def test_scripted_cites_visible_tool_results_and_never_counts_paid_calls(analysis):
    payload = {"question": "cart timeout", "engine": "scripted"}
    result = analysis.investigate(payload, "orders", lambda: pytest.fail("paid request"))
    assert result == analysis.investigate(payload, "orders", lambda: pytest.fail("paid request"))
    assert result["engine_turns"] >= 2
    cited = {cid for h in result["hypotheses"] for cid in h["evidence_ids"]}
    assert cited == {"log-9", "dep-7", "rep-3"}
    assert "cart" in result["hypotheses"][0]["title"]
    serialized = json.dumps(result)
    assert "restricted-5" not in serialized
    assert "DB-PRIVATE-CANARY" not in serialized


class Transcript:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.calls = 0

    def create(self, *, system, messages, tools):
        assert "DB-PRIVATE-CANARY" not in repr(messages)
        self.calls += 1
        turn = next(self.turns)
        if isinstance(turn, Exception):
            raise turn
        return LLMResponse(content=[ToolUseBlock(str(i), name, args)
                                    for i, (name, args) in enumerate(turn)], stop_reason="tool_use")


def search_all():
    return [(name, {"query": "cart"}) for name in ("search_logs", "search_deployments", "search_reports")]


def submit(ids):
    return [("submit_investigation", {"hypotheses": [
        {"title": "cart 문제", "description": "배포 이후 풀 대기 가능성", "evidence_ids": ids}]})]


def use_claude(analysis, monkeypatch, turns):
    client = Transcript(turns)
    monkeypatch.setattr(analysis, "_claude_client", lambda: nullcontext(client))
    counted = []

    def run(**kwargs):
        return analysis.investigate({"question": "cart timeout", "engine": "claude"},
                                    "orders", lambda: counted.append(1), **kwargs)
    return run, counted, client


def test_invalid_citations_can_be_repaired(analysis, monkeypatch):
    run, attempts, client = use_claude(analysis, monkeypatch, [
        search_all(), submit(["restricted-5"]), submit(["log-9"]),
    ])
    result = run()
    assert result["hypotheses"][0]["evidence_ids"] == ["log-9"]
    assert result["engine_turns"] == len(attempts) == client.calls == 3


@pytest.mark.parametrize("bad", [submit(["restricted-5"]), submit(["missing"]), submit([]),
                                 [("submit_investigation", {"hypotheses": "not a list"})]])
def test_bad_model_submissions_stop_after_repair_budget(analysis, monkeypatch, bad):
    run, attempts, client = use_claude(analysis, monkeypatch, [search_all(), bad, bad])
    with pytest.raises(analysis.InvestigationError) as error:
        run()
    assert error.value.code == "invalid_model_output"
    assert len(attempts) == client.calls == 3


def test_comparison_rows_are_not_seen_before_tool_results(analysis, monkeypatch):
    forged = submit(["log-9"])
    run, _, _ = use_claude(analysis, monkeypatch, [search_all() + forged, forged])
    # First guess is sent together with the search, before the model could read results.
    result = run()
    assert result["engine_turns"] == 2


def test_provider_timeouts_get_one_retry_and_own_failure_code(analysis, monkeypatch):
    run, attempts, client = use_claude(analysis, monkeypatch, [TimeoutError(), TimeoutError()])
    with pytest.raises(analysis.InvestigationError) as error:
        run()
    assert error.value.code == "provider_failure"
    assert len(attempts) == client.calls == 2


def test_provider_retry_is_counted_separately_from_logical_turns(analysis, monkeypatch):
    run, attempts, client = use_claude(analysis, monkeypatch, [
        TimeoutError(), search_all(), submit(["log-9"]),
    ])
    assert run()["engine_turns"] == 2
    assert len(attempts) == client.calls == 3


def test_missing_logs_do_not_generate_hypotheses(analysis, monkeypatch):
    monkeypatch.setattr(analysis, "research", lambda *a, **kw: dict(graph=[], vector=[], paths=[]))
    with pytest.raises(analysis.InvestigationError) as error:
        analysis.investigate({"question": "cart timeout", "engine": "scripted"}, "orders", lambda: None)
    assert error.value.code == "insufficient_evidence"


@pytest.mark.parametrize("payload,team", [({"question": "   "}, "orders"),
                                           ({"question": "q", "engine": "other"}, "orders"),
                                           ({"question": "q"}, "bogus")])
def test_bad_inputs_never_call_provider(analysis, payload, team):
    with pytest.raises(analysis.InvestigationError) as error:
        analysis.investigate(payload, team, lambda: pytest.fail("paid request"))
    assert error.value.code == "invalid_input"


def test_platform_hypothesis_cites_current_slow_query_without_borrowing_other_team_rows(analysis, monkeypatch):
    slow = row("db-02", "log", "Slow query: SELECT customer_id", teams=("platform",), service="mysql")
    change = row("dep-7", "deployment", "cart v7 changed lookup query")
    def search(question, team, kind=None, **kwargs):
        rows = [r for r in (slow, change, ROWS[0], ROWS[2]) if kind is None or r["kind"] == kind]
        return result_with_paths(rows)

    monkeypatch.setattr(analysis, "research", search)
    platform = analysis.investigate({"question": "cart lookup slowdown"}, "platform", lambda: None)
    orders = analysis.investigate({"question": "cart lookup slowdown"}, "orders", lambda: None)
    assert any({"dep-7", "db-02", "log-9"} <= set(h["evidence_ids"])
               for h in platform["hypotheses"])
    assert "db-02" not in repr(orders)
    assert not any("SELECT customer_id" in h["description"] for h in platform["hypotheses"])
    assert any("SELECT customer_id" in e["text"] for e in platform["evidence"])

def test_candidate_carries_paths_seen_during_investigation_not_comparison(analysis, monkeypatch):
    def search(question, team, kind=None, **kwargs):
        rows = [r for r in ROWS if r["kind"] == kind] if kind else []
        return result_with_paths(rows)

    monkeypatch.setattr(analysis, "research", search)
    result = analysis.investigate({"question": "cart timeout"}, "orders", lambda: None)
    assert result["paths"] == []
    for h in result["hypotheses"]:
        assert {p["evidence_id"] for p in h["paths"]} == set(h["evidence_ids"])


@pytest.mark.parametrize("problem", ["wrong_edge", "wrong_incident", "upload"])
def test_deployment_claim_requires_real_current_changed_edge(analysis, monkeypatch, problem):
    deployment = dict(ROWS[1])
    if problem == "wrong_incident":
        deployment["incident"] = "unrelated incident"
    if problem == "upload":
        deployment["origin"] = "upload"
    rows = [ROWS[0], deployment, ROWS[2]]
    def search(question, team, kind=None, **kwargs):
        result = result_with_paths([r for r in rows if kind is None or r["kind"] == kind])
        if problem == "wrong_edge":
            for path in result["paths"]:
                if path["evidence_id"] == "dep-7":
                    path["edges"][0]["target"] = "other-service"
        return result

    monkeypatch.setattr(analysis, "research", search)
    result = analysis.investigate({"question": "cart timeout"}, "orders", lambda: None)
    assert result["hypotheses"]
    assert all("dep-7" not in h["evidence_ids"] for h in result["hypotheses"])


def test_full_length_api_question_is_accepted(analysis):
    result = analysis.investigate({"question": "주" * 2000}, "orders", lambda: None)
    assert result["hypotheses"]


def test_claude_messages_request_serializes_only_scoped_tool_results(analysis, monkeypatch):
    import anthropic
    import httpx
    if not issubclass(anthropic.DefaultHttpxClient, httpx.Client):
        import httpx2 as httpx  # newer SDK transport
    from fivewhys.agent.llm import AnthropicLLM

    requests = []
    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            turn = search_all()
        else:
            assert body["messages"][-1]["role"] == "user"
            results = body["messages"][-1]["content"]
            assert {json.loads(r["content"])["kind"] for r in results} == {"log", "deployment", "report"}
            assert "DB-PRIVATE-CANARY" not in str(body)
            assert "restricted-5" not in str(body)
            turn = submit(["log-9", "dep-7", "rep-3"])
        blocks = [dict(type="tool_use", id=f"tool-{index}", name=name, input=args)
                  for index, (name, args) in enumerate(turn)]
        return httpx.Response(200, headers={"request-id": f"req-{len(requests)}"}, json=dict(
            id=f"message-{len(requests)}", type="message", role="assistant", content=blocks,
            stop_reason="tool_use", stop_sequence=None, model="actual-fixture-model",
            usage=dict(input_tokens=50, output_tokens=20),
        ))

    with anthropic.Anthropic(api_key="fixture", max_retries=0,
                             http_client=httpx.Client(transport=httpx.MockTransport(provider))) as sdk:
        monkeypatch.setattr(analysis, "_claude_client", lambda: nullcontext(AnthropicLLM(_client=sdk)))
        counted = []
        trace = analysis.InvestigationObservation()
        result = analysis.investigate({"question": "cart timeout", "engine": "claude"},
                                      "orders", lambda: counted.append(1), observation=trace)
    assert len(requests) == len(counted) == result["engine_turns"] == 2
    assert len(result["hypotheses"][0]["paths"]) == 3
    assert [r["message_id"] for r in trace.provider_attempts] == ["message-1", "message-2"]
    assert [r["request_id"] for r in trace.provider_attempts] == ["req-1", "req-2"]
    assert {r["model"] for r in trace.provider_attempts} == {"actual-fixture-model"}
    assert all(r["usage"] == {"input_tokens": 50, "output_tokens": 20} for r in trace.provider_attempts)
    assert "provider_attempts" not in result


def test_scripted_uses_verified_graph_evidence_for_hypotheses(analysis, monkeypatch):
    def search(question, team, kind=None, **kwargs):
        result = result_with_paths([r for r in ROWS if kind is None or r["kind"] == kind])
        if kind == "report":
            result["graph"] = []
            result["paths"] = []
            result["decisions"] = [{"evidence_id": "rep-3", "action": "excluded", "reason": "관련 경로 없음"}]
        return result

    monkeypatch.setattr(analysis, "research", search)
    result = analysis.investigate({"question": "cart timeout"}, "orders", lambda: None)
    assert all("rep-3" not in h["evidence_ids"] for h in result["hypotheses"])


def test_private_trace_keeps_failed_first_submission_and_retry(analysis, monkeypatch):
    from copy import deepcopy

    def search(question, team, kind=None, **kwargs):
        matches = result_with_paths([r for r in ROWS if kind is None or r["kind"] == kind])
        matches["decisions"] = [{"evidence_id": r["id"], "action": "retained", "reason": "graph verified"}
                                for r in matches["graph"]]
        return matches

    monkeypatch.setattr(analysis, "research", search)
    snapshots = []
    trace = analysis.InvestigationObservation(save=lambda data: snapshots.append(deepcopy(data)))
    run, paid, _ = use_claude(analysis, monkeypatch, [
        TimeoutError("provider diagnostics"), search_all(), submit(["unseen-100"]), submit(["log-9"])])
    result = run(observation=trace)
    assert len(paid) == 4
    assert [(r["turn"], r["attempt"], r["status"]) for r in trace.provider_attempts] == [
        (1, 1, "error"), (1, 2, "returned"), (2, 1, "returned"), (3, 1, "returned")]
    first, repaired = trace.submissions
    assert first["hypotheses"] == submit(["unseen-100"])[0][1]["hypotheses"]
    assert first["status"] == "rejected" and first["reason"] == "unknown_citation"
    assert repaired["status"] == "accepted"
    assert repaired["hypotheses"][0]["evidence_ids"] == result["hypotheses"][0]["evidence_ids"] == ["log-9"]
    assert any(s["submissions"] and s["submissions"][0]["status"] == "selected" for s in snapshots)
    logs = trace.tool_calls[0]
    assert logs["query"] == "cart"
    assert logs["returned"] == [dict(id="log-9", source="test-fixture", locator="log-9", version="test",
                                     kind="log", service="cart", excerpt="HikariPool connection timed out")]
    assert logs["retrieval_decisions"] == [dict(evidence_id="log-9", action="retained", reason="graph verified")]
    assert all(s not in repr(trace.as_dict()) for s in ("restricted-5", "DB-PRIVATE-CANARY", "provider diagnostics"))


def test_terminal_failure_preserves_submissions_without_unexpected_fields_or_credentials(analysis, monkeypatch):
    secret = "opaque-unit-test-credential"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    rejected = submit(["unknown"])
    rejected[0][1]["hypotheses"][0]["description"] = "failed: " + secret
    rejected[0][1]["extra"] = "opaque-do-not-record"
    run, _, _ = use_claude(analysis, monkeypatch, [search_all(), rejected, rejected])
    trace = analysis.InvestigationObservation()
    with pytest.raises(analysis.InvestigationError, match="검증") as error:
        run(observation=trace)
    assert error.value.code == "invalid_model_output"
    assert [s["reason"] for s in trace.submissions] == ["invalid_schema", "invalid_schema"]
    assert all(s["hypotheses"][0]["description"] == "failed: [redacted]" for s in trace.submissions)
    assert all(s not in repr(trace.as_dict()) for s in (secret, "opaque-do-not-record"))


def test_mixed_turn_and_insufficient_evidence_remain_distinct(analysis, monkeypatch):
    empty = [("submit_investigation", {"hypotheses": []})]
    run, _, _ = use_claude(analysis, monkeypatch, [search_all() + empty, empty])
    trace = analysis.InvestigationObservation()
    with pytest.raises(analysis.InvestigationError) as error:
        run(observation=trace)
    assert error.value.code == "insufficient_evidence"
    assert [(s["hypotheses"], s["status"], s["reason"]) for s in trace.submissions] == [
        ([], "rejected", "mixed_turn"), ([], "rejected", "insufficient_evidence")]
