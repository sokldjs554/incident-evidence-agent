"""MCP(Model Context Protocol) 서버.

로그 분석 도구를 MCP 표준으로 노출한다. Claude Desktop / Claude Code 등
MCP 호스트가 이 서버에 연결하면, 어떤 LLM 앱에서든 동일한 로그 분석 도구를
쓸 수 있다 — 에이전트 내부 도구(agent/tools.py)와 같은 저장소 계층을
공유하되, 노출 표면만 MCP 표준으로 바꾼 것이다.

Claude Desktop 설정 예시(claude_desktop_config.json):
    {
      "mcpServers": {
        "fivewhys-logs": {
          "command": "/path/to/.venv/bin/fivewhys",
          "args": ["mcp-serve", "--scenario", "db_pool_exhaustion"]
        }
      }
    }
"""

from __future__ import annotations

import json
import threading

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from fivewhys.agent.tools import ToolExecutor
from fivewhys.pipeline import build_retriever, build_store
from fivewhys.scenarios.catalog import get_scenario


def create_server(scenario: str = "db_pool_exhaustion", seed: int = 42) -> FastMCP:
    bundle = get_scenario(scenario, seed)
    # mcp 2.x는 동기 도구 함수를 워커 스레드에서 실행하므로
    # (1) SQLite 스레드 검사를 끄고 (2) 락으로 접근을 직렬화한다
    store, _ = build_store(bundle, check_same_thread=False)
    retriever = build_retriever(store)
    executor = ToolExecutor.for_bundle(bundle, store, retriever)
    lock = threading.Lock()

    mcp = FastMCP(
        "fivewhys-logs",
        instructions=(
            f"장애 로그 분석 도구 모음. 현재 적재된 장애: {bundle.title} "
            f"(장애 구간 {bundle.incident[0].isoformat()} ~ {bundle.incident[1].isoformat()})"
        ),
    )

    def _run(name: str, **kwargs) -> str:
        with lock:
            content, is_error = executor.execute(name, kwargs)
        if is_error:
            raise RuntimeError(content)
        return content

    @mcp.tool()
    def get_error_timeline(bucket_minutes: int = 2) -> str:
        """장애 구간의 서비스별 WARN/ERROR 발생량을 시간 버킷 단위로 집계한다."""
        return _run("get_error_timeline", bucket_minutes=bucket_minutes)

    @mcp.tool()
    def get_anomalous_templates(top_k: int = 10) -> str:
        """베이스라인 대비 장애 구간에서 급증한 로그 템플릿을 점수순으로 반환한다."""
        return _run("get_anomalous_templates", top_k=top_k)

    @mcp.tool()
    def search_logs(
        query: str, service: str | None = None, level: str | None = None, limit: int = 15
    ) -> str:
        """로그 본문 BM25 전문 검색. service/level 필터 지원."""
        return _run("search_logs", query=query, service=service, level=level, limit=limit)

    @mcp.tool()
    def get_log_context(log_id: int, before: int = 5, after: int = 5) -> str:
        """특정 로그 id 전후의 주변 로그를 시간순으로 반환한다."""
        return _run("get_log_context", log_id=log_id, before=before, after=after)

    @mcp.tool()
    def list_change_events() -> str:
        """최근 배포/설정 변경 이벤트 목록을 반환한다."""
        return _run("list_change_events")

    @mcp.tool()
    def search_past_incidents(query: str, k: int = 4) -> str:
        """과거 장애 보고서(포스트모템)를 하이브리드 검색(BM25+벡터+RRF)한다."""
        return _run("search_past_incidents", query=query, k=k)

    @mcp.tool()
    def get_incident_info() -> str:
        """현재 적재된 장애의 개요(서비스 목록, 시간 창)를 반환한다."""
        return json.dumps(
            {
                "title": bundle.title,
                "description": bundle.description,
                "services": bundle.services,
                "baseline": [bundle.baseline[0].isoformat(), bundle.baseline[1].isoformat()],
                "incident": [bundle.incident[0].isoformat(), bundle.incident[1].isoformat()],
            },
            ensure_ascii=False,
        )

    return mcp


def run_server(scenario: str = "db_pool_exhaustion", seed: int = 42) -> None:
    create_server(scenario, seed).run()
