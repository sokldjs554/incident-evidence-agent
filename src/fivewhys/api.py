"""REST API (FastAPI).

POST /analyze     : 시나리오 선택 → 에이전트 실행 → RCA 보고서 반환
GET  /reports/{id}: 저장된 보고서 조회
GET  /scenarios   : 사용 가능한 시나리오 목록
"""

from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from fivewhys.agent.five_whys import run_five_whys_agent
from fivewhys.agent.scripted import ScriptedLLM
from fivewhys.agent.tools import ToolExecutor
from fivewhys.evaluation.harness import score_report
from fivewhys.investigation.routes import install
from fivewhys.models import RCAReport
from fivewhys.pipeline import build_retriever, build_store
from fivewhys.scenarios.catalog import SCENARIOS, get_scenario


class AnalyzeRequest(BaseModel):
    scenario: str = Field(description="시나리오 이름 (GET /scenarios 참고)")
    seed: int = 42
    engine: str = Field(default="scripted", description="'scripted' | 'claude'")


class AnalyzeResponse(BaseModel):
    report_id: str
    scenario: str
    engine: str
    n_turns: int
    report: RCAReport
    scores: dict


def create_app() -> FastAPI:
    app = FastAPI(
        title="Five Whys Agent",
        description="LLM 에이전트 기반 장애 로그 근본원인분석(RCA) API",
        version="0.2.0",
    )
    install(app)
    reports: dict[str, AnalyzeResponse] = {}

    @app.get("/scenarios")
    def list_scenarios() -> dict:
        out = {}
        for name in SCENARIOS:
            bundle = get_scenario(name)
            out[name] = {"title": bundle.title, "description": bundle.description}
        return out

    @app.post("/analyze", response_model=AnalyzeResponse)
    def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
        if req.scenario not in SCENARIOS:
            raise HTTPException(404, f"알 수 없는 시나리오: {req.scenario}")
        if req.engine == "scripted":
            llm = ScriptedLLM()
        elif req.engine == "claude":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise HTTPException(400, "claude 엔진은 ANTHROPIC_API_KEY가 필요합니다.")
            from fivewhys.agent.llm import AnthropicLLM

            llm = AnthropicLLM()
        else:
            raise HTTPException(400, f"알 수 없는 엔진: {req.engine}")

        bundle = get_scenario(req.scenario, req.seed)
        store, _ = build_store(bundle)
        try:
            retriever = build_retriever(store)
            executor = ToolExecutor.for_bundle(bundle, store, retriever)
            result = run_five_whys_agent(bundle, executor, llm)
            scores = score_report(result.report, bundle, store)
        finally:
            store.close()

        resp = AnalyzeResponse(
            report_id=str(uuid.uuid4())[:8],
            scenario=req.scenario,
            engine=req.engine,
            n_turns=result.n_turns,
            report=result.report,
            scores=scores,
        )
        reports[resp.report_id] = resp
        return resp

    @app.get("/reports/{report_id}", response_model=AnalyzeResponse)
    def get_report(report_id: str) -> AnalyzeResponse:
        if report_id not in reports:
            raise HTTPException(404, "보고서를 찾을 수 없습니다.")
        return reports[report_id]

    return app


app = create_app()
