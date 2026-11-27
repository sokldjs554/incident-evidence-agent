"""Confirmed outbox publication and at-least-once job consumption."""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException as HTTPProtocolError
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

import pika
import psycopg
from psycopg.types.json import Jsonb

from . import jobs
from .contracts import TerminalJob

log = logging.getLogger(__name__)
MAX_EXECUTIONS = 3


def queue_name():
    return os.getenv("JOB_QUEUE", "fivewhys.jobs.v1")


def open_broker():
    params = pika.URLParameters(os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/%2F"))
    params.blocked_connection_timeout = 5
    return pika.BlockingConnection(params)


def _publish_due(db, channel):
    # Confirm before deleting. A crash between the confirm and commit can
    # publish twice, so the consumer checks durable state before doing work.
    for _ in range(10):
        with db.transaction():
            message = db.execute("""
                SELECT job_id::text, action, attempt FROM investigation_outbox
                WHERE available_at <= now() ORDER BY available_at
                LIMIT 1 FOR UPDATE SKIP LOCKED
            """).fetchone()
            if not message:
                return
            channel.basic_publish(
                exchange="", routing_key=queue_name(), mandatory=True,
                body=json.dumps(message).encode(),
                properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
            )
            db.execute("""
                DELETE FROM investigation_outbox
                WHERE job_id = %(job_id)s AND action = %(action)s AND attempt = %(attempt)s
            """, message)


def _fail(db, job_id, code, error):
    with db.transaction():
        row = db.execute("""
            UPDATE investigation_jobs SET status = 'failed', error_code = %s, error = %s,
                finished_at = now(), updated_at = now() WHERE id = %s RETURNING callback_status
        """, (code, error, job_id)).fetchone()
        if row["callback_status"] == "pending":
            jobs.schedule(db, job_id, "callback", 1)


def _run(db, job_id, job):
    if job["status"] not in {"queued", "running"}:
        return
    if job["llm_calls"]:
        # A worker disappeared after reserving a paid request. Its reply may
        # have been lost. Re-running this job could bill the user again.
        _fail(db, job_id, "provider_outcome_unknown", "작업이 중단되어 이전 모델 호출의 결과를 확인할 수 없습니다.")
        return
    if job["runs"] >= MAX_EXECUTIONS:
        _fail(db, job_id, "worker_interrupted", "반복된 작업 중단으로 실행이 종료되었습니다.")
        return
    db.execute("""
        UPDATE investigation_jobs SET status = 'running', runs = runs + 1,
            started_at = coalesce(started_at, now()), updated_at = now()
        WHERE id = %s
    """, (job_id,))

    def count_call():
        db.execute("""
            UPDATE investigation_jobs SET llm_calls = llm_calls + 1, updated_at = now() WHERE id = %s
        """, (job_id,))

    from .analysis import InvestigationError, investigate

    try:
        if job["kind"] == "analysis":
            observed = job["payload"].get("engine") == "claude" and db.execute("""
                SELECT 1 FROM investigation_observations WHERE job_id = %s AND team = %s
            """, (job_id, job["team"])).fetchone()
            if observed:
                from .observation import InvestigationObservation

                def save(trace):
                    db.execute("""
                        UPDATE investigation_observations SET trace = %s WHERE job_id = %s AND team = %s
                    """, (Jsonb(trace), job_id, job["team"]))

                observation = InvestigationObservation(save=save)
                save(observation.as_dict())
                result = investigate(job["payload"], job["team"], count_call, observation=observation)
            else:
                result = investigate(job["payload"], job["team"], count_call)
        else:
            from .retrieval import index_documents
            result = index_documents(job["payload"]["documents"], job["team"])
    except psycopg.Error:
        # Database loss also releases the session fence. Never reconnect on
        # behalf of this execution. Rabbit redelivery acquires a new fence.
        raise
    except InvestigationError as exc:
        _fail(db, job_id, exc.code, str(exc))
        return
    except Exception:
        log.exception("Job execution failed: %s", job_id)
        _fail(db, job_id, "execution_failed", "작업을 완료할 수 없습니다.")
        return

    with db.transaction():
        db.execute("""
            UPDATE investigation_jobs SET status = 'completed', result = %s, finished_at = now(), updated_at = now()
            WHERE id = %s
        """, (Jsonb(result), job_id))
        if job["callback_status"] == "pending":
            jobs.schedule(db, job_id, "callback", 1)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _post_result(job_id, job):
    url = os.getenv("CALLBACK_URL", "http://localhost:8001/callback")
    try:
        request = Request(
            url,
            data=TerminalJob.model_validate(job).model_dump_json().encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": str(job_id)},
            method="POST",
        )
        with build_opener(_NoRedirect()).open(request, timeout=5) as response:
            return response.status, None
    except HTTPError as exc:
        with exc:
            return exc.code, f"HTTP {exc.code}"
    except (URLError, OSError, HTTPProtocolError, ValueError):
        return None, "콜백 서버 연결에 실패했습니다."


def _callback(db, job_id, job, attempt):
    if job["status"] not in {"completed", "failed"} or job["callback_status"] not in {"pending", "retrying"}:
        return
    previous = db.execute("""
        SELECT attempt, finished_at FROM investigation_callbacks
        WHERE job_id = %s ORDER BY attempt DESC LIMIT 1
    """, (job_id,)).fetchone()
    if previous and not previous["finished_at"]:
        if attempt != previous["attempt"]:
            return
        # HTTP might already have delivered before the worker stopped. Use
        # the same job-level idempotency key on the next bounded attempt.
        status, error = None, "작업 중단으로 콜백 응답을 확인할 수 없습니다."
    else:
        expected = previous["attempt"] + 1 if previous else 1
        if attempt != expected or attempt > jobs.MAX_CALLBACK_ATTEMPTS:
            return
        db.execute("""
            INSERT INTO investigation_callbacks(job_id, attempt) VALUES (%s, %s)
        """, (job_id, attempt))
        status, error = _post_result(job_id, job)

    delivered = status is not None and 200 <= status < 300
    state = "delivered" if delivered else "exhausted"
    with db.transaction():
        if not delivered and attempt < jobs.MAX_CALLBACK_ATTEMPTS:
            state = "retrying"
            delay = max(0, int(os.getenv("CALLBACK_RETRY_SECONDS", "30"))) * 2 ** (attempt - 1)
            jobs.schedule(db, job_id, "callback", attempt + 1, delay)
        db.execute("""
            UPDATE investigation_jobs SET callback_status = %s, updated_at = now() WHERE id = %s
        """, (state, job_id))
        db.execute("""
            UPDATE investigation_callbacks SET finished_at = now(), status_code = %s, error = %s
            WHERE job_id = %s AND attempt = %s
        """, (status, error, job_id, attempt))


def handle(body):
    try:
        message = json.loads(body)
        job_id = UUID(message["job_id"])
        action = message["action"]
        attempt = message["attempt"]
        if action not in ("run", "callback") or type(attempt) is not int:
            raise ValueError("invalid message")
    except (ValueError, TypeError, KeyError, AttributeError):
        log.warning("Dropping malformed job message")
        return
    with jobs.connect() as db:
        # A session lock outlives state commits, keeping provider accounting
        # visible while preventing a second execution or callback in parallel.
        db.execute("SELECT pg_advisory_lock(hashtextextended(%s::text, 5230193041))", (job_id,))
        job = db.execute("SELECT * FROM investigation_jobs WHERE id = %s", (job_id,)).fetchone()
        if not job:
            return
        if action == "run":
            _run(db, job_id, job)
        else:
            _callback(db, job_id, job, attempt)


def consume():
    with jobs.connect() as db, open_broker() as broker, ThreadPoolExecutor(max_workers=1) as pool:
        channel = broker.channel()
        channel.queue_declare(queue=queue_name(), durable=True)
        channel.confirm_delivery()
        channel.basic_qos(prefetch_count=1)
        while True:
            _publish_due(db, channel)
            delivery, _, body = channel.basic_get(queue_name(), auto_ack=False)
            if delivery is None:
                broker.process_data_events(time_limit=0.25)
                continue
            task = pool.submit(handle, body)
            while not task.done():
                broker.process_data_events(time_limit=0.25)
            task.result()
            channel.basic_ack(delivery.delivery_tag)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("pika").setLevel(logging.WARNING)
    while True:
        try:
            consume()
        except (pika.exceptions.AMQPError, psycopg.Error, OSError) as exc:
            log.warning("Service unavailable, reconnecting (%s)", type(exc).__name__)
            time.sleep(1)


if __name__ == "__main__":
    main()
