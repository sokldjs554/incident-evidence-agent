"""Real PostgreSQL, RabbitMQ, worker processes and HTTP. Only the model is a test double."""

import json
import multiprocessing
import os
import socket
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest

if os.getenv("FIVEWHYS_INTEGRATION") != "1":
    pytest.skip("Set FIVEWHYS_INTEGRATION=1 and start PostgreSQL/RabbitMQ", allow_module_level=True)

import pika
import psycopg
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo

from fivewhys.investigation import jobs, receiver, worker


def wait_for(read, condition, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = read()
        if condition(value):
            return value
        time.sleep(0.1)
    pytest.fail(f"Timed out waiting for durable state. Last value: {value!r}")


@pytest.fixture()
def services(monkeypatch):
    # A private SQL schema and queue prevent another running Compose worker
    # from consuming this test's outbox or test messages.
    name = "jobtest_" + uuid4().hex
    base = jobs.database_url()
    with psycopg.connect(base, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    monkeypatch.setenv("DATABASE_URL", make_conninfo(base, options=f"-c search_path={name}"))
    monkeypatch.setenv("JOB_QUEUE", name)
    monkeypatch.setenv("CALLBACK_RETRY_SECONDS", "120")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(receiver.app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    monkeypatch.setenv("CALLBACK_URL", f"http://127.0.0.1:{port}/callback")
    try:
        wait_for(lambda: server.started, bool)
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        with worker.open_broker() as broker:
            broker.channel().queue_delete(queue=name)
        with psycopg.connect(base, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def model_double(payload, team, count_call):
    from fivewhys.investigation.analysis import InvestigationError

    count_call()
    if payload.get("hang"):
        # The parent kills this process after seeing the durable reservation.
        threading.Event().wait(60)
    if payload.get("fail"):
        raise InvestigationError("insufficient_evidence", "근거가 부족합니다.")
    return {"team": team, "question": payload["question"]}


def start_worker(pause_after_delivery):
    from fivewhys.investigation import analysis
    analysis.investigate = model_double
    if pause_after_delivery:
        post = worker._post_result

        def pause_after_receipt(job_id, job):
            status, error = post(job_id, job)
            if status == 200:
                threading.Event().wait(60)
            return status, error

        worker._post_result = pause_after_receipt
    worker.main()


@contextmanager
def running_worker(pause_after_delivery=False):
    process = multiprocessing.get_context("spawn").Process(target=start_worker, args=(pause_after_delivery,))
    process.start()
    try:
        yield
    finally:
        process.kill()
        process.join(timeout=5)
        assert not process.is_alive()


def test_lost_publication_confirmation_restart_and_retry(services):
    key = jobs.submit("analysis", {"question": "why?", "callback": True}, "orders")
    read = lambda: jobs.get_job(key, "orders")
    assert read()["status"] == "queued"
    assert jobs.get_job(key, "platform") is None
    assert jobs.retry_callback(key, "platform") is None

    with worker.open_broker() as broker, jobs.connect() as db:
        channel = broker.channel()
        channel.queue_declare(queue=worker.queue_name(), durable=True)
        channel.confirm_delivery()

        class LostConfirmation:
            def basic_publish(self, **message):
                channel.basic_publish(**message)
                raise pika.exceptions.AMQPConnectionError("simulated lost publish confirmation")

        with pytest.raises(pika.exceptions.AMQPConnectionError):
            worker._publish_due(db, LostConfirmation())
        assert db.execute("SELECT count(*) AS n FROM investigation_outbox").fetchone()["n"] == 1

    # Duplicate "run" messages now reach a real broker consumer.
    with running_worker():
        before = wait_for(read, lambda j: j["callback_status"] == "retrying")
    assert before["status"] == "completed"
    assert before["llm_calls"] == 1
    assert [c["status_code"] for c in before["callbacks"]] == [503]
    assert before["created_at"] <= before["started_at"] <= before["finished_at"]
    assert before["result"] == {"team": "orders", "question": "why?"}

    # Re-submit both a stale callback and the old execution after the worker died.
    with worker.open_broker() as broker:
        channel = broker.channel()
        for action, attempt in (("callback", 1), ("run", 0)):
            channel.basic_publish(
                exchange="", routing_key=worker.queue_name(),
                body=json.dumps({"job_id": key, "action": action, "attempt": attempt}),
            )
    jobs.retry_callback(key, "orders")
    with running_worker():
        after = wait_for(read, lambda j: j["callback_status"] == "delivered")
    assert after["llm_calls"] == before["llm_calls"]
    assert after["result"] == before["result"]
    assert [c["status_code"] for c in after["callbacks"]] == [503, 200]
    with jobs.connect() as db:
        receipt = db.execute("SELECT attempts, accepted_at FROM investigation_receipts WHERE job_id = %s", (key,)).fetchone()
        assert receipt["attempts"] == 2
        assert receipt["accepted_at"] is not None
    with pytest.raises(ValueError):
        jobs.retry_callback(key, "orders")


def test_worker_loss_after_provider_reservation_uses_failure_callback(services, monkeypatch):
    monkeypatch.setenv("CALLBACK_RETRY_SECONDS", "0")
    key = jobs.submit("analysis", {"question": "why?", "callback": True, "hang": True}, "orders")
    read = lambda: jobs.get_job(key, "orders")
    with running_worker():
        wait_for(read, lambda j: j["status"] == "running" and j["llm_calls"] == 1)
    with running_worker():
        job = wait_for(read, lambda j: j["callback_status"] == "delivered")
    assert job["status"] == "failed"
    assert job["error_code"] == "provider_outcome_unknown"
    assert job["llm_calls"] == 1
    assert [c["status_code"] for c in job["callbacks"]] == [503, 200]


def test_permanently_unavailable_callback_is_bounded(services, monkeypatch):
    monkeypatch.setenv("CALLBACK_URL", os.environ["CALLBACK_URL"] + "/missing")
    monkeypatch.setenv("CALLBACK_RETRY_SECONDS", "0")
    key = jobs.submit("analysis", {"question": "why?", "callback": True, "fail": True}, "orders")
    with running_worker():
        job = wait_for(lambda: jobs.get_job(key, "orders"), lambda j: j["callback_status"] == "exhausted")
    assert job["status"] == "failed"
    assert job["error_code"] == "insufficient_evidence"
    assert job["llm_calls"] == 1
    assert [c["status_code"] for c in job["callbacks"]] == [404] * jobs.MAX_CALLBACK_ATTEMPTS
    with pytest.raises(ValueError):
        jobs.retry_callback(key, "orders")


def test_crash_after_remote_acceptance_reuses_delivery_key(services, monkeypatch):
    monkeypatch.setenv("CALLBACK_RETRY_SECONDS", "0")
    receiver.health()
    key = jobs.submit("analysis", {"question": "why?", "callback": True}, "orders")

    def receipt():
        with jobs.connect() as db:
            return db.execute(
                "SELECT attempts, accepted_at FROM investigation_receipts WHERE job_id = %s", (key,)
            ).fetchone()

    with running_worker(pause_after_delivery=True):
        accepted = wait_for(receipt, lambda r: r and r["accepted_at"])
        assert accepted["attempts"] == 2
        recorded = jobs.get_job(key, "orders")["callbacks"]
        assert [row["status_code"] for row in recorded] == [503, None]
        assert recorded[-1]["finished_at"] is None
    with running_worker():
        job = wait_for(lambda: jobs.get_job(key, "orders"), lambda j: j["callback_status"] == "delivered")
    assert job["llm_calls"] == 1
    assert [c["status_code"] for c in job["callbacks"]] == [503, None, 200]
    assert job["callbacks"][1]["error"]
    assert receipt() == {"attempts": 3, "accepted_at": accepted["accepted_at"]}


def test_one_use_capture_bound_to_team_preserves_private_failed_trace(services, monkeypatch):
    from contextlib import nullcontext
    from fastapi.testclient import TestClient
    from fivewhys.api import create_app
    from fivewhys.agent.llm import LLMResponse, ToolUseBlock
    from fivewhys.investigation import analysis

    capture = uuid4()
    with jobs.connect() as db:
        db.execute("INSERT INTO investigation_observations(capture_id, team) VALUES (%s, %s)", (capture, "orders"))
    # A provider double drives the ordinary SDK-independent loop; no request leaves the test.
    class EmptySubmission:
        def create(self, **kwargs):
            return LLMResponse(content=[ToolUseBlock("submit", "submit_investigation", {"hypotheses": []})],
                               model="unit-double", stop_reason="tool_use")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-test")
    monkeypatch.setenv("FIVEWHYS_DEMO_ACCOUNTS", "1")
    monkeypatch.setattr(analysis, "_claude_client", lambda: nullcontext(EmptySubmission()))
    monkeypatch.setattr(analysis, "research", lambda *a, **kw: dict(vector=[], graph=[], paths=[]))
    client = TestClient(create_app())
    body = {"engine": "claude", "question": "why?"}
    headers = {"X-Investigation-Observation": str(capture)}
    client.post("/v1/demo/session", json={"team": "platform"})
    assert client.post("/v1/analyses", headers=headers, json=body).status_code == 409
    client.post("/v1/demo/session", json={"team": "orders"})
    receipt = client.post("/v1/analyses", headers=headers, json=body)
    assert receipt.status_code == 202
    assert client.post("/v1/analyses", headers=headers, json=body).status_code == 409
    key = receipt.json()["id"]

    worker.handle(json.dumps({"job_id": key, "attempt": 0, "action": "run"}))
    with jobs.connect() as db:
        observation = db.execute("SELECT job_id::text, trace FROM investigation_observations WHERE capture_id = %s",
                                 (capture,)).fetchone()
        assert db.execute("SELECT count(*) AS n FROM investigation_jobs").fetchone()["n"] == 1
    assert observation["job_id"] == key
    trace = observation["trace"]
    assert [s["reason"] for s in trace["submissions"]] == ["missing_search", "missing_search"]
    job = client.get(receipt.headers["Location"]).json()
    assert job["status"] == "failed" and job["error_code"] == "invalid_model_output"
    assert len(trace["provider_attempts"]) == job["llm_calls"] == 2
    assert "submissions" not in repr(job) and "unit-double" not in repr(job)
    assert client.get(receipt.headers["Location"] + "/result").status_code == 409
