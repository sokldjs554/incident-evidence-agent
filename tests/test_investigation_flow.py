"""HTTP investigation flow with real database, broker, search and callback services."""

import hashlib
import json
import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("FIVEWHYS_INTEGRATION") != "1",
    reason="requires the Docker Compose investigation stack",
)

SQL_QUESTION = (
    "Slow query: SELECT * FROM orders WHERE customer_ref execution time. "
    "order-service HikariPool connection pool, v2.4.0 배포"
)
PLATFORM_LOG = "scenario/db_pool_exhaustion/seed-42/logs/mysql/slowlog-WARN.log"


def _json(response, expected=200):
    assert response.status_code == expected, (response.request.url, response.text)
    return response.json()


def _sign_in(client, team):
    response = client.post("/v1/demo/session", json={"team": team})
    assert _json(response) == {"team": team}
    assert "HttpOnly" in response.headers["set-cookie"]


def _submit(client, collection, **payload):
    response = client.post(f"/v1/{collection}", json=payload)
    body = _json(response, 202)
    assert body["status"] == "queued"
    assert response.headers["location"] == f"/v1/jobs/{body['id']}"
    return response.headers["location"]


def _wait_job(client, path, callback_status=None, *, status="completed"):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = _json(client.get(path))
        assert "result" not in job
        if job["status"] in {"completed", "failed"}:
            assert job["status"] == status, job
            if callback_status is None or job["callback_status"] == callback_status:
                return job
        time.sleep(0.25)
    pytest.fail(f"Timed out waiting for {path}: {job}")


def _result(client, path):
    result = _json(client.get(f"{path}/result"))
    visible = {row["id"] for row in result["evidence"]}
    for name in ("vector", "graph"):
        assert result[name]
        assert {row["id"] for row in result[name]} <= visible
    assert result["paths"]
    assert {edge_path["evidence_id"] for edge_path in result["paths"]} <= visible
    assert result["hypotheses"]
    for candidate in result["hypotheses"]:
        assert candidate["evidence_ids"]
        assert set(candidate["evidence_ids"]) <= visible
    return result


@pytest.fixture
def api_clients():
    import httpx

    base_url = os.getenv("FIVEWHYS_API_URL", "http://127.0.0.1:8750")
    with httpx.Client(base_url=base_url) as orders, httpx.Client(base_url=base_url) as platform:
        yield orders, platform


@pytest.fixture
def indexed_test_ids():
    ids = []
    yield ids
    if ids:
        from fivewhys.investigation.retrieval import _stores

        milvus, collection, neo4j = _stores()
        milvus.delete(collection, ids=ids)
        neo4j.execute_query("MATCH (e:Evidence) WHERE e.id IN $ids DETACH DELETE e", ids=ids)


def test_team_scoped_jobs_rankings_evidence_and_indexing(api_clients, indexed_test_ids):
    orders, platform = api_clients
    assert orders.post("/v1/analyses", json={"question": SQL_QUESTION}).status_code == 401
    _sign_in(orders, "orders")
    _sign_in(platform, "platform")
    orders_job = _submit(orders, "analyses", question=SQL_QUESTION)
    platform_job = _submit(platform, "analyses", question=SQL_QUESTION)
    assert _wait_job(orders, orders_job)["llm_calls"] == 0
    assert _wait_job(platform, platform_job)["llm_calls"] == 0

    orders_result = _result(orders, orders_job)
    platform_result = _result(platform, platform_job)
    assert [row["id"] for row in orders_result["vector"]] != [
        row["id"] for row in platform_result["vector"]
    ]
    for result in (orders_result, platform_result):
        assert [row["id"] for row in result["vector"]] != [
            row["id"] for row in result["graph"]
        ]
        assert any(
            edge_path.get("incident_edge", {}).get("target") == "order-service"
            for edge_path in result["paths"]
        )
    restricted = next(row for row in platform_result["evidence"] if row["source"] == PLATFORM_LOG)
    assert "Slow query:" in restricted["text"]
    assert restricted["id"] not in {row["id"] for row in orders_result["evidence"]}
    assert PLATFORM_LOG not in repr(orders_result)
    private_path = f"/evidence/{restricted['id']}"
    assert _json(platform.get(platform_job + private_path)) == restricted
    assert orders.get(orders_job + private_path).status_code == 404
    for suffix in ("", "/result", private_path):
        assert orders.get(platform_job + suffix).status_code == 404
    assert platform.get(orders_job + "/result").status_code == 404
    assert orders.post(platform_job + "/callback/retry").status_code == 404

    token = f"poolcheck{uuid4().hex}"
    upload = {
        "source": f"integration/{token}.md",
        "kind": "runbook",
        "title": f"{token} HikariPool order-service",
        "service": "order-service",
        "version": "r1",
        "text": f"1. {token} HikariPool connection pool order-service v2.4.0 상태를 확인한다.",
    }
    index_job = _submit(orders, "indexes", documents=[upload])
    index_status = _wait_job(orders, index_job)
    assert index_status["kind"] == "index"
    indexed_result = _json(orders.get(index_job + "/result"))
    indexed_test_ids.extend(indexed_result["evidence_ids"])
    assert indexed_result["documents"] == indexed_result["chunks"] == 1
    assert platform.get(index_job + "/result").status_code == 404

    upload_question = f"{token} HikariPool connection pool order-service v2.4.0"
    owner_job = _submit(orders, "analyses", question=upload_question)
    outsider_job = _submit(platform, "analyses", question=upload_question)
    _wait_job(orders, owner_job)
    _wait_job(platform, outsider_job)
    owner = _result(orders, owner_job)
    outsider = _result(platform, outsider_job)
    indexed = next(row for row in owner["vector"] if row["source"] == upload["source"])
    assert indexed_result["evidence_ids"] == [indexed["id"]]
    assert indexed["version"].startswith("r1@sha256:")
    assert token in indexed["text"]
    assert _json(orders.get(f"{owner_job}/evidence/{indexed['id']}")) == indexed
    assert token not in repr(outsider)
    assert platform.get(f"{outsider_job}/evidence/{indexed['id']}").status_code == 404


@pytest.fixture
def provider_boundary_worker(monkeypatch, tmp_path, request):
    """Start the actual worker with an isolated outbox and deterministic model transport."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from fivewhys.investigation import jobs, worker

    base_dsn = jobs.database_url()
    namespace = f"regression_{uuid4().hex}"
    monkeypatch.setenv("DATABASE_URL", make_conninfo(base_dsn, options=f"-c search_path={namespace}"))
    monkeypatch.setenv("JOB_QUEUE", namespace)
    monkeypatch.setenv("CALLBACK_RETRY_SECONDS", "120")
    monkeypatch.setenv("FIVEWHYS_DEMO_ACCOUNTS", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-transport")
    capture_path = tmp_path / "provider-attempts.jsonl"
    start_worker = """
import json
import sys
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

from fivewhys.investigation import analysis, worker


class CapturingModel(analysis.ScriptedInvestigator):
    def create(self, **request):
        with Path(sys.argv[2]).open("a", encoding="utf-8") as transcript:
            transcript.write(json.dumps(request, ensure_ascii=False, default=asdict) + "\\n")
        response = super().create(**request)
        if sys.argv[3] == "invalid":
            for block in response.content:
                if block.name == "submit_investigation":
                    block.input["hypotheses"][0]["evidence_ids"] = ["ev-forged"]
        return response


analysis._claude_client = lambda: nullcontext(CapturingModel(sys.argv[1]))
worker.main()
"""

    with psycopg.connect(base_dsn, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
        mode = getattr(request, "param", "normal")
        process = subprocess.Popen([
            sys.executable, "-c", start_worker, SQL_QUESTION, str(capture_path), mode,
        ])
        try:
            yield db, capture_path
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            with worker.open_broker() as broker:
                broker.channel().queue_delete(namespace)
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


def test_callback_retry_reuses_same_result_and_positive_model_count(
    provider_boundary_worker, record_property
):
    from fastapi.testclient import TestClient

    from fivewhys.api import create_app
    from fivewhys.investigation import jobs

    receipt_db, capture_path = provider_boundary_worker
    with TestClient(create_app()) as client:
        _sign_in(client, "orders")
        capture = uuid4()
        with jobs.connect() as db:
            db.execute("INSERT INTO investigation_observations(capture_id, team) VALUES (%s, 'orders')", (capture,))
        accepted = client.post("/v1/analyses", json={"question": SQL_QUESTION, "engine": "claude", "callback": True},
                               headers={"X-Investigation-Observation": str(capture)})
        job_id = _json(accepted, 202)["id"]
        path = accepted.headers["location"]
        assert path == f"/v1/jobs/{job_id}"

        def read_trace():
            with jobs.connect() as db:
                return db.execute("SELECT trace FROM investigation_observations WHERE capture_id=%s AND job_id=%s",
                                  (capture, job_id)).fetchone()["trace"]

        before = _wait_job(client, path, "retrying")
        assert [callback["status_code"] for callback in before["callbacks"]] == [503]
        result = _result(client, path)
        assert result["engine"] == "claude"
        assert before["llm_calls"] == result["engine_turns"] >= 2
        orders_messages = capture_path.read_text(encoding="utf-8").splitlines()
        assert len(orders_messages) == before["llm_calls"]
        assert all(PLATFORM_LOG not in message for message in orders_messages)
        trace = read_trace()
        assert len(trace["provider_attempts"]) == before["llm_calls"]
        assert all(attempt["provider_endpoint"] == "other_or_unknown" for attempt in trace["provider_attempts"])
        assert {c["name"] for c in trace["tool_calls"]} == {
            "search_logs", "search_deployments", "search_reports", "submit_investigation",
        }
        assert trace["submissions"][-1]["status"] == "accepted"
        assert PLATFORM_LOG not in repr(trace) and "provider_attempts" not in repr(result)

        assert _json(client.post(path + "/callback/retry"), 202)["id"] == before["id"]
        after = _wait_job(client, path, "delivered")
        assert after["id"] == before["id"]
        assert [callback["attempt"] for callback in after["callbacks"]] == [1, 2]
        assert [callback["status_code"] for callback in after["callbacks"]] == [503, 200]
        assert after["llm_calls"] == before["llm_calls"]
        assert _result(client, path) == result
        assert read_trace() == trace
        assert capture_path.read_text(encoding="utf-8").splitlines() == orders_messages
        assert client.post(path + "/callback/retry").status_code == 409

        _sign_in(client, "platform")
        platform_path = _submit(client, "analyses", question=SQL_QUESTION, engine="claude")
        platform_job = _wait_job(client, platform_path)
        platform_result = _result(client, platform_path)
        restricted = next(row for row in platform_result["evidence"] if row["source"] == PLATFORM_LOG)
        platform_messages = capture_path.read_text(encoding="utf-8").splitlines()[len(orders_messages):]
        assert len(platform_messages) == platform_job["llm_calls"]
        assert any(restricted["id"] in message and PLATFORM_LOG in message for message in platform_messages)
        assert all(restricted["id"] not in message for message in orders_messages)

    job_id = after["id"]
    with jobs.connect() as db:
        assert db.execute("SELECT runs FROM investigation_jobs WHERE id=%s", (job_id,)).fetchone() == {
            "runs": 1
        }
    assert receipt_db.execute("""
        SELECT attempts, accepted_at IS NOT NULL FROM investigation_receipts WHERE job_id=%s
    """, (job_id,)).fetchone() == (2, True)
    report_bytes = json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
    record_property("callback_job_id", job_id)
    record_property("callback_status_codes", "503,200")
    record_property("analysis_executions", 1)
    record_property("model_calls_before_and_after", f"{before['llm_calls']},{after['llm_calls']}")
    record_property("engine_turns", result["engine_turns"])
    record_property("stored_report_sha256", hashlib.sha256(report_bytes).hexdigest())


@pytest.mark.parametrize("provider_boundary_worker", ["invalid"], indirect=True)
def test_forged_model_citations_fail_after_one_repair(provider_boundary_worker):
    from fastapi.testclient import TestClient

    from fivewhys.api import create_app
    from fivewhys.investigation import jobs

    _, capture_path = provider_boundary_worker
    with TestClient(create_app()) as client:
        _sign_in(client, "orders")
        path = _submit(client, "analyses", question=SQL_QUESTION, engine="claude")
        job = _wait_job(client, path, status="failed")
        assert job["error_code"] == "invalid_model_output"
        assert client.get(path + "/result").status_code == 409
        assert client.get(path + "/evidence/ev-forged").status_code == 409

    messages = [json.loads(row) for row in capture_path.read_text(encoding="utf-8").splitlines()]
    assert len(messages) == job["llm_calls"] == 4
    repair = messages[-1]["messages"][-1]["content"]
    assert len(repair) == 1 and repair[0]["is_error"]
    with jobs.connect() as db:
        assert db.execute(
            "SELECT runs, result FROM investigation_jobs WHERE id=%s", (job["id"],)
        ).fetchone() == {"runs": 1, "result": None}
