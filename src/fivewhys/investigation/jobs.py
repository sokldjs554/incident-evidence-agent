"""PostgreSQL job state and transactional RabbitMQ outbox."""

import os
from functools import lru_cache
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


MAX_CALLBACK_ATTEMPTS = 3


def database_url():
    return os.getenv("DATABASE_URL", "postgresql://fivewhys:fivewhys@localhost:5432/fivewhys")


def connect():
    url = database_url()
    _initialize(url)
    return psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=5)


@lru_cache(maxsize=4)
def _initialize(url):
    # Multiple API and worker processes can start against an empty database.
    with psycopg.connect(url, connect_timeout=5) as db:
        db.execute("SELECT pg_advisory_xact_lock(5230193041)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS investigation_jobs (
                id uuid PRIMARY KEY,
                team text NOT NULL,
                kind text NOT NULL,
                payload jsonb NOT NULL,
                status text NOT NULL DEFAULT 'queued',
                runs integer NOT NULL DEFAULT 0,
                llm_calls integer NOT NULL DEFAULT 0,
                callback_status text NOT NULL,
                result jsonb,
                error text,
                error_code text,
                created_at timestamptz NOT NULL DEFAULT now(),
                started_at timestamptz,
                finished_at timestamptz,
                updated_at timestamptz NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS investigation_observations (
                capture_id uuid PRIMARY KEY,
                team text NOT NULL,
                job_id uuid UNIQUE REFERENCES investigation_jobs(id) ON DELETE CASCADE,
                trace jsonb
            );
            CREATE TABLE IF NOT EXISTS investigation_outbox (
                job_id uuid NOT NULL REFERENCES investigation_jobs(id) ON DELETE CASCADE,
                action text NOT NULL,
                attempt integer NOT NULL,
                available_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY(job_id, action, attempt)
            );
            CREATE INDEX IF NOT EXISTS investigation_outbox_due ON investigation_outbox(available_at);
            CREATE TABLE IF NOT EXISTS investigation_callbacks (
                job_id uuid NOT NULL REFERENCES investigation_jobs(id) ON DELETE CASCADE,
                attempt integer NOT NULL,
                status_code integer,
                error text,
                created_at timestamptz NOT NULL DEFAULT now(),
                finished_at timestamptz,
                PRIMARY KEY(job_id, attempt)
            );
        """)


def schedule(db, job_id, action, attempt=0, delay=0):
    db.execute("""
        INSERT INTO investigation_outbox(job_id, action, attempt, available_at)
        VALUES (%s, %s, %s, now() + %s * interval '1 second')
        ON CONFLICT (job_id, action, attempt) DO NOTHING
    """, (job_id, action, attempt, delay))


def submit(kind, payload, team, capture_id=None):
    if kind not in {"analysis", "index"}:
        raise ValueError("Unknown job kind")
    if not team or not isinstance(payload, dict):
        raise ValueError("A team and payload are required")
    job_id = uuid4()
    callback_status = "pending" if payload.get("callback") is True else "disabled"
    with connect() as db, db.transaction():
        db.execute("""
            INSERT INTO investigation_jobs(id, team, kind, payload, callback_status)
            VALUES (%s, %s, %s, %s, %s)
        """, (job_id, team, kind, Jsonb(payload), callback_status))
        if capture_id is not None:
            if kind != "analysis" or payload.get("engine") != "claude":
                raise ValueError("capture requires Claude analysis")
            bound = db.execute("""
                UPDATE investigation_observations SET job_id = %s
                WHERE capture_id = %s AND team = %s AND job_id IS NULL RETURNING job_id
            """, (job_id, capture_id, team)).fetchone()
            if not bound:
                raise ValueError("capture unavailable")
        schedule(db, job_id, "run")
    return str(job_id)


def _uuid(value):
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _public_job(db, job_id, team):
    job = db.execute("""
        SELECT id::text, team, kind, status, result, error, error_code, llm_calls,
               callback_status, created_at, started_at, finished_at, updated_at
        FROM investigation_jobs WHERE id = %s AND team = %s
        FOR SHARE
    """, (job_id, team)).fetchone()
    if job:
        job["callbacks"] = db.execute("""
            SELECT attempt, status_code, error, created_at, finished_at
            FROM investigation_callbacks WHERE job_id = %s ORDER BY attempt
        """, (job_id,)).fetchall()
    return job


def get_job(job_id, team):
    with connect() as db, db.transaction():
        return _public_job(db, _uuid(job_id), team)


def retry_callback(job_id, team):
    """Bring forward the next bounded retry. Dispatched retries are already due."""
    with connect() as db, db.transaction():
        job_id = _uuid(job_id)
        row = db.execute("""
            SELECT callback_status FROM investigation_jobs WHERE id = %s AND team = %s FOR UPDATE
        """, (job_id, team)).fetchone()
        if row is None:
            return None
        if row["callback_status"] != "retrying":
            raise ValueError("실패한 콜백 중 재시도 가능한 건만 요청할 수 있습니다.")
        db.execute("""
            UPDATE investigation_outbox SET available_at = now()
            WHERE job_id = %s AND action = 'callback' AND available_at > now()
        """, (job_id,))
        return _public_job(db, job_id, team)
