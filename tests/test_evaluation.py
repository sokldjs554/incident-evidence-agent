"""평가 하니스: scripted 베이스라인이 전 시나리오에서 기준선을 넘는지 검증."""

from fivewhys.evaluation.harness import evaluate, render_markdown


def test_scripted_engine_full_evaluation():
    summary = evaluate(engine="scripted", seeds=(42,))
    assert len(summary.results) == 5
    for r in summary.results:
        assert r.error is None, f"{r.scenario}: {r.error}"

    # scripted 베이스라인의 최소 품질 기준
    assert summary.root_service_accuracy == 1.0
    assert summary.keyword_accuracy == 1.0
    assert summary.culprit_accuracy == 1.0
    assert summary.mean_evidence_precision >= 0.9


def test_render_markdown_contains_table():
    summary = evaluate(engine="scripted", scenario_names=["cert_expiry"], seeds=(42,))
    md = render_markdown(summary)
    assert "| 시나리오 |" in md
    assert "cert_expiry" in md
    assert "정확도" in md
