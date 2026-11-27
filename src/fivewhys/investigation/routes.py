"""HTTP boundary for the asynchronous investigation. Legacy callers need no broker."""
from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import Receipt, TerminalJob
from .retrieval_corpus import DEFAULT_SCENARIO, DEFAULT_SEED, incident_key

Team = Literal["orders", "platform"]
Engine = Literal["scripted", "claude"]
Status = Literal["queued", "running", "completed", "failed"]
EvidenceKind = Literal["report", "runbook", "log", "deployment"]
WEB = Path(__file__).resolve().parents[1] / "web"
bearer = HTTPBearer(auto_error=False)
demo_cookie = APIKeyCookie(name="fw_session", scheme_name="DemoCookie", auto_error=False)
# The single API process in Compose can issue restart-scoped demo sessions.
# Set a shared random signing key for multiple API processes.
_PROCESS_KEY = secrets.token_hex(32)
router = APIRouter(prefix="/v1", tags=["사건 조사"])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*")
    @classmethod
    def valid_string(cls, value):
        if isinstance(value, str):
            if "\0" in value:
                raise ValueError("NUL is not supported in PostgreSQL text")
            value.encode("utf-8")
        return value


class DemoConfig(BaseModel):
    demo_accounts: bool
    claude_available: bool
    source_revision: str


class DemoSession(Input):
    team: Team


class InvestigationRequest(Input):
    question: str = Field(min_length=1, max_length=2000)
    engine: Engine = "scripted"
    callback: bool = False
    scenario: str = DEFAULT_SCENARIO
    seed: int = Field(default=DEFAULT_SEED, strict=True)

    @field_validator("scenario")
    @classmethod
    def known_scenario(cls, value):
        incident_key(value, DEFAULT_SEED)
        return value


class SourceDocument(Input):
    model_config = ConfigDict(str_strip_whitespace=False)

    kind: EvidenceKind
    title: str = Field(min_length=1, max_length=180)
    service: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=240, description="원문 식별자 또는 경로")
    text: str = Field(min_length=1, max_length=32768)
    version: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("source", "title", "service", "text", "version")
    @classmethod
    def check_bytes(cls, value, info):
        if value is not None:
            limit = 32768 if info.field_name == "text" else 512
            if not value.strip() or len(value.encode("utf-8")) > limit:
                raise ValueError(f"nonempty UTF-8 field of at most {limit} bytes required")
        return value


class IndexRequest(Input):
    documents: list[SourceDocument] = Field(min_length=1, max_length=32)
    callback: bool = False


class Accepted(BaseModel):
    id: UUID
    status: Literal["queued"] = "queued"


class FlexibleOutput(BaseModel):
    model_config = ConfigDict(extra="allow")


class Evidence(FlexibleOutput):
    id: str
    kind: EvidenceKind
    title: str
    text: str
    source: str
    service: str
    version: str
    locator: str


class Hypothesis(FlexibleOutput):
    title: str
    description: str
    evidence_ids: list[str]


class GraphPath(FlexibleOutput):
    evidence_id: str
    nodes: list[str]
    relations: list[str]


class AnalysisResult(FlexibleOutput):
    engine: Engine
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    vector: list[Evidence]
    graph: list[Evidence]
    paths: list[GraphPath]


class Delivery(FlexibleOutput):
    attempt: int
    status_code: int | None = None


class Job(FlexibleOutput):
    id: UUID
    kind: Literal["analysis", "index"]
    status: Status
    team: Team
    llm_calls: int
    callbacks: list[Delivery]
    error: str | None = None
    error_code: str | None = None
    callback_status: Literal["disabled", "pending", "retrying", "delivered", "exhausted"] = "disabled"
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


class IndexResult(BaseModel):
    documents: int
    chunks: int
    evidence_ids: list[str]


class Problem(BaseModel):
    detail: str


ERRORS = {
    401: {"model": Problem, "description": "팀 인증 필요"},
    404: {"model": Problem, "description": "작업 또는 근거를 조회할 수 없음"},
    409: {"model": Problem, "description": "작업 상태가 요청과 맞지 않음"},
    503: {"model": Problem, "description": "작업 저장소 또는 선택한 엔진 사용 불가"},
}


ACCEPT_RESPONSES = {**ERRORS, 202: {"description": "작업 접수",
    "headers": {"Location": {"description": "작업 조회 경로", "schema": {"type": "string"}}}}}


def demo_enabled() -> bool:
    return os.getenv("FIVEWHYS_DEMO_ACCOUNTS") == "1"


def session_token(team: str) -> str:
    key = os.getenv("FIVEWHYS_SESSION_KEY") or _PROCESS_KEY
    signature = hmac.digest(key.encode(), team.encode(), "sha256").hex()
    return f"{team}.{signature}"


def caller(
    auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    cookie: Annotated[str | None, Depends(demo_cookie)],
    expected_team: Annotated[Team | None, Header(alias="X-Expected-Team")] = None,
) -> Team:
    teams: tuple[Team, Team] = ("orders", "platform")
    for team in teams:
        if auth:
            token = os.getenv(f"FIVEWHYS_{team.upper()}_TOKEN")
            matches = token and secrets.compare_digest(auth.credentials.encode(), token.encode())
        else:
            matches = demo_enabled() and cookie and secrets.compare_digest(cookie.encode(), session_token(team).encode())
        if matches:
            if expected_team and expected_team != team:
                raise HTTPException(409, "데모 계정이 변경됐습니다. 팀을 다시 선택하세요.",
                                    headers={"X-Session-Changed": "1"})
            return team
    raise HTTPException(401, "팀 인증이 필요합니다.", headers={"WWW-Authenticate": "Bearer"})


Caller = Annotated[Team, Depends(caller)]


def _job_service(operation: str, *args):
    try:
        import psycopg
        from fivewhys.investigation import jobs
    except ImportError as exc:
        raise HTTPException(503, "조사 서비스 의존성이 필요합니다. Docker Compose로 실행하세요.") from exc
    try:
        return getattr(jobs, operation)(*args)
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "작업 저장소에 연결할 수 없습니다.") from exc


def submit_job(kind, payload, team):
    return _job_service("submit", kind, payload, team)


def lookup_job(key, team):
    return _job_service("get_job", key, team)


def accepted(kind: str, payload: dict, team: Team, response: Response) -> Accepted:
    key = submit_job(kind, payload, team)
    response.headers["Location"] = f"/v1/jobs/{key}"
    return Accepted(id=key)


@router.get("/demo/config")
def config() -> DemoConfig:
    return DemoConfig(demo_accounts=demo_enabled(), claude_available=bool(os.getenv("ANTHROPIC_API_KEY")),
                      source_revision=os.getenv("FIVEWHYS_REVISION", "local"))


@router.post("/demo/session", response_model=DemoSession, responses={404: ERRORS[404]})
def set_session(body: DemoSession, request: Request, response: Response) -> DemoSession:
    if not demo_enabled():
        raise HTTPException(404, "시연 계정이 비활성화되어 있습니다.")
    response.set_cookie("fw_session", session_token(body.team), httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/v1")
    response.headers["Cache-Control"] = "no-store"
    return body


@router.post("/analyses", status_code=202, response_model=Accepted, responses=ACCEPT_RESPONSES)
def analyze(
    body: InvestigationRequest, team: Caller, response: Response,
    capture_id: Annotated[UUID | None, Header(alias="X-Investigation-Observation",
        description="운영자가 같은 팀에 사전 등록한 일회용 관찰 ID")] = None,
):
    if body.engine == "claude" and not config().claude_available:
        raise HTTPException(503, "Claude 엔진의 자격 증명이 설정되지 않았습니다.")
    if capture_id is not None:
        if body.engine != "claude":
            raise HTTPException(409, "관찰 요청에는 Claude 엔진이 필요합니다.")
        try:
            key = _job_service("submit", "analysis", body.model_dump(), team, capture_id)
        except ValueError as exc:
            raise HTTPException(409, "사용 가능한 관찰 요청을 확인하세요.") from exc
        response.headers["Location"] = f"/v1/jobs/{key}"
        return Accepted(id=key)
    return accepted("analysis", body.model_dump(), team, response)


@router.post("/indexes", status_code=202, response_model=Accepted, responses=ACCEPT_RESPONSES)
def index(body: IndexRequest, team: Caller, response: Response):
    return accepted("index", body.model_dump(exclude_none=True), team, response)


def find_job(job_id: UUID, team: Team) -> dict:
    job = lookup_job(str(job_id), team)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return job


def ready_result(job: dict) -> dict:
    if job["status"] != "completed":
        raise HTTPException(409, "완료된 결과가 없습니다. 작업 상태와 오류를 확인하세요.")
    return job["result"]


def job_summary(job: dict) -> dict:
    # Request bodies (including uploaded documents) and results have separate access paths.
    return {key: value for key, value in job.items() if key not in {"result", "payload", "request"}}


@router.get("/jobs/{job_id}", response_model=Job, responses=ERRORS)
def status(job_id: UUID, team: Caller):
    return job_summary(find_job(job_id, team))


@router.get("/jobs/{job_id}/result", response_model=AnalysisResult | IndexResult, responses=ERRORS)
def result(job_id: UUID, team: Caller):
    return ready_result(find_job(job_id, team))


@router.get("/jobs/{job_id}/evidence/{evidence_id}", response_model=Evidence, responses=ERRORS)
def evidence(job_id: UUID, evidence_id: str, team: Caller):
    report = ready_result(find_job(job_id, team))
    for row in report.get("evidence", []):
        if row["id"] == evidence_id:
            return row
    raise HTTPException(404, "근거를 찾을 수 없습니다.")


@router.post("/jobs/{job_id}/callback/retry", response_model=Job, responses=ERRORS, status_code=202)
def retry(job_id: UUID, team: Caller):
    try:
        job = _job_service("retry_callback", str(job_id), team)
    except ValueError as exc:
        raise HTTPException(409, "이 작업은 현재 콜백을 재전송할 수 없습니다.") from exc
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return job_summary(job)


def install(app: FastAPI) -> None:
    @app.webhooks.post("job.completed", response_model=Receipt, responses={
        400: {"description": "Idempotency-Key와 본문 작업 ID 불일치"},
        409: {"description": "같은 ID로 다른 내용 전송"},
        503: {"model": Receipt, "description": "테스트 수신기의 첫 전송 실패. 저장된 결과로 재시도"},
    }, description="워커가 CALLBACK_URL에 터미널 상태와 저장된 결과를 POST합니다. 동일 작업은 같은 키를 사용합니다.")
    def completed_event(event: TerminalJob, idempotency_key: Annotated[str, Header()]):
        """외부 수신기 구현용 계약. 실제 데모 수신기는 receiver:8080/callback에 있습니다."""

    app.include_router(router)
    app.mount("/assets", StaticFiles(directory=WEB, check_dir=False), name="investigation-assets")

    @app.get("/", include_in_schema=False)
    def desk():
        return FileResponse(WEB / "index.html")

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Cookie, Authorization"
        return response
