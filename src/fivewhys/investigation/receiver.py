"""Demo webhook sink with a durable per-job first-failure and idempotent receipt."""

import hashlib
import json
from functools import lru_cache

from fastapi import FastAPI, Header, HTTPException, Response

from . import jobs
from .contracts import Receipt, TerminalJob

app = FastAPI(title="Five Whys demo callback receiver")


@lru_cache(maxsize=4)
def _initialize(url):
    with jobs.connect() as db, db.transaction():
        db.execute("SELECT pg_advisory_xact_lock(5230193042)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS investigation_receipts (
                job_id uuid PRIMARY KEY,
                digest text NOT NULL,
                attempts integer NOT NULL DEFAULT 1,
                accepted_at timestamptz
            )
        """)


@app.get("/health")
def health():
    _initialize(jobs.database_url())
    with jobs.connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/callback", response_model=Receipt, responses={400: {"description": "키와 작업 ID 불일치"}, 409: {"description": "동일한 키의 본문 불일치"}, 503: {"model": Receipt}})
def receive(payload: TerminalJob, response: Response, idempotency_key: str = Header()) -> Receipt:
    if idempotency_key != str(payload.id):
        raise HTTPException(status_code=400, detail="Idempotency key must match the job ID")
    _initialize(jobs.database_url())
    serialized = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    with jobs.connect() as db:
        row = db.execute("""
            INSERT INTO investigation_receipts(job_id, digest) VALUES (%s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
                attempts = investigation_receipts.attempts + 1,
                accepted_at = coalesce(investigation_receipts.accepted_at, now())
            WHERE investigation_receipts.digest = excluded.digest
            RETURNING attempts
        """, (payload.id, digest)).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="Payload differs for this idempotency key")
    first = row["attempts"] == 1
    response.status_code = 503 if first else 200
    return Receipt(id=payload.id, attempt=row["attempts"], status="retry" if first else "accepted")
