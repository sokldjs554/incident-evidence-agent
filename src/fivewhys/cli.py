"""CLI 진입점.

fivewhys demo      : 시나리오 하나를 분석하고 보고서를 출력
fivewhys evaluate  : 전체 시나리오 평가 후 마크다운 결과 출력
fivewhys serve     : REST API 서버 기동
fivewhys mcp-serve : MCP 서버(stdio) 기동
"""

from __future__ import annotations

import argparse
import sys

from fivewhys.models import RCAReport


def _print_report(report: RCAReport, n_turns: int, scores: dict | None = None) -> None:
    bar = "=" * 72
    print(bar)
    print("근본원인분석(RCA) 보고서")
    print(bar)
    print(f"\n[요약]\n{report.summary}")
    print("\n[5 Whys 인과 사슬]")
    for i, step in enumerate(report.five_whys, 1):
        print(f"  Why {i}. {step.question}")
        print(f"     → {step.answer}")
        if step.evidence_log_ids:
            print(f"       증거 로그: {step.evidence_log_ids}")
    print(f"\n[근본 원인] ({report.root_cause_service})\n{report.root_cause}")
    if report.culprit_change:
        print(f"\n[원인 변경] {report.culprit_change}")
    print("\n[재발 방지 대책]")
    for r in report.remediation:
        print(f"  - {r}")
    if report.similar_incident:
        print(f"\n[유사 과거 장애] {report.similar_incident}")
    print(f"\n신뢰도: {report.confidence:.0%} · 분석 턴 수: {n_turns}")
    if scores:
        print("\n[자동 채점]")
        for k, v in scores.items():
            print(f"  {k}: {v}")
    print(bar)


def cmd_demo(args: argparse.Namespace) -> int:
    from fivewhys.agent.five_whys import run_five_whys_agent
    from fivewhys.agent.scripted import ScriptedLLM
    from fivewhys.agent.tools import ToolExecutor
    from fivewhys.evaluation.harness import score_report
    from fivewhys.pipeline import build_retriever, build_store
    from fivewhys.scenarios.catalog import get_scenario

    bundle = get_scenario(args.scenario, args.seed)
    print(f"시나리오: {bundle.title} — {bundle.description}")
    print(f"로그 {len(bundle.logs)}건, 변경 이벤트 {len(bundle.deploys)}건 적재 중...\n")

    store, _ = build_store(bundle)
    retriever = build_retriever(store)
    executor = ToolExecutor.for_bundle(bundle, store, retriever)

    if args.engine == "claude":
        from fivewhys.agent.llm import AnthropicLLM

        llm = AnthropicLLM()
        print(f"엔진: Claude API ({llm.model})\n")
    else:
        llm = ScriptedLLM()
        print("엔진: scripted (오프라인 휴리스틱 베이스라인)\n")

    try:
        result = run_five_whys_agent(bundle, executor, llm)
    except TypeError as e:
        if "authentication" in str(e).lower():
            print("Claude API 인증 실패 — ANTHROPIC_API_KEY 환경변수를 설정하세요 (.env.example 참고).", file=sys.stderr)
            store.close()
            return 1
        raise
    scores = score_report(result.report, bundle, store)
    _print_report(result.report, result.n_turns, scores)
    store.close()
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from fivewhys.evaluation.harness import evaluate, render_markdown

    seeds = tuple(range(args.base_seed, args.base_seed + args.runs))
    summary = evaluate(engine=args.engine, seeds=seeds)
    md = render_markdown(summary)
    print(md)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print(f"\n결과 저장: {args.output}", file=sys.stderr)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("fivewhys.api:app", host=args.host, port=args.port)
    return 0


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    try:
        from fivewhys.mcp_server.server import run_server
    except ImportError:
        print(
            "MCP 서버에는 mcp 패키지가 필요합니다: pip install 'fivewhys-agent[mcp]'",
            file=sys.stderr,
        )
        return 1
    run_server(scenario=args.scenario, seed=args.seed)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fivewhys", description="Five Whys Agent — 장애 로그 근본원인분석"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="시나리오 하나를 분석")
    p_demo.add_argument("--scenario", default="db_pool_exhaustion")
    p_demo.add_argument("--seed", type=int, default=42)
    p_demo.add_argument("--engine", choices=["scripted", "claude"], default="scripted")
    p_demo.set_defaults(func=cmd_demo)

    p_eval = sub.add_parser("evaluate", help="전체 시나리오 평가")
    p_eval.add_argument("--engine", choices=["scripted", "claude"], default="scripted")
    p_eval.add_argument("--runs", type=int, default=1, help="시나리오당 실행 횟수(시드 수)")
    p_eval.add_argument("--base-seed", type=int, default=42)
    p_eval.add_argument("--output", help="마크다운 결과 저장 경로")
    p_eval.set_defaults(func=cmd_evaluate)

    p_serve = sub.add_parser("serve", help="REST API 서버 기동")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    p_mcp = sub.add_parser("mcp-serve", help="MCP 서버(stdio) 기동")
    p_mcp.add_argument("--scenario", default="db_pool_exhaustion")
    p_mcp.add_argument("--seed", type=int, default=42)
    p_mcp.set_defaults(func=cmd_mcp_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
