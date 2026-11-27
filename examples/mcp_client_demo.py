"""MCP 서버 실습 — 호스트 앱 없이 MCP 인터페이스를 직접 호출한다.

Claude Desktop/Code 같은 MCP 호스트에 연결하면 이 서버의 도구들이 그대로
노출된다. 다만 호스트 앱은 OS 버전 등 별도 요구사항이 있으므로, 이 스크립트는
서버를 in-process로 띄워 MCP 표준 API(list_tools / call_tool)를 직접 호출한다.

즉, 호스트가 하는 일을 코드로 대신 수행해 서버가 MCP 규약대로 동작함을 보여준다.

실행:
    pip install -e ".[mcp]"
    python examples/mcp_client_demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys


def unwrap(result) -> str:
    """mcp 버전별 call_tool 반환 형태를 흡수한다."""
    if hasattr(result, "content"):  # mcp 2.x: CallToolResult
        blocks = result.content
    elif isinstance(result, tuple):  # mcp 1.x FastMCP: (blocks, raw)
        blocks = result[0]
    else:
        blocks = result
    return blocks[0].text


async def run() -> int:
    try:
        from fivewhys.mcp_server.server import create_server
    except ImportError as e:
        print(f"❌ MCP 패키지가 필요합니다: {e}")
        print('   → pip install -e ".[mcp]"')
        return 1

    server = create_server("db_pool_exhaustion", seed=42)

    # 1) 호스트가 연결 직후 하는 일: 사용 가능한 도구 목록 조회
    tools = await server.list_tools()
    print(f"MCP 서버가 노출하는 도구 {len(tools)}종:")
    for t in tools:
        summary = (t.description or "").strip().split("\n")[0]
        print(f"  · {t.name:26s} {summary[:60]}")

    # 2) 호스트(LLM)가 분석할 때 부르는 순서대로 호출해 본다
    print("\n" + "=" * 72)
    print("호스트가 분석 시 호출하는 순서 재현")
    print("=" * 72)

    info = json.loads(unwrap(await server.call_tool("get_incident_info", {})))
    print(f"\n[1] get_incident_info → 적재된 장애: {info['title']}")
    print(f"    장애 구간: {info['incident'][0]} ~ {info['incident'][1]}")

    anomalies = json.loads(
        unwrap(await server.call_tool("get_anomalous_templates", {"top_k": 3}))
    )
    print("\n[2] get_anomalous_templates → 급증한 로그 패턴 상위 3개")
    for a in anomalies:
        print(
            f"    {a['service']:16s} {a['level']:5s} "
            f"장애 {a['count_incident']:3d}건 / 평시 {a['count_baseline']:3d}건  "
            f"{a['template'][:45]}"
        )

    changes = json.loads(unwrap(await server.call_tool("list_change_events", {})))
    print("\n[3] list_change_events → 장애 전 변경 이력")
    for c in changes:
        print(f"    {c['ts']}  {c['description'][:60]}")

    logs = json.loads(
        unwrap(
            await server.call_tool(
                "search_logs", {"query": "connection pool timeout", "limit": 3}
            )
        )
    )
    print("\n[4] search_logs('connection pool timeout') → 증거 로그")
    for row in logs:
        print(f"    [{row['id']}] {row['service']} {row['level']}  {row['message'][:55]}")

    past = json.loads(
        unwrap(
            await server.call_tool(
                "search_past_incidents", {"query": "커넥션 풀 고갈 slow query", "k": 2}
            )
        )
    )
    print("\n[5] search_past_incidents → 유사 과거 장애 (하이브리드 RAG)")
    for p in past:
        print(f"    {p['doc']} · {p['section']}")

    print("\n" + "=" * 72)
    print("이 도구들이 Claude Desktop/Code 같은 MCP 호스트에 그대로 노출된다.")
    print("호스트 연동 설정은 README의 mcp-serve 항목 참고.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
