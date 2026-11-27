"""적대적 코드 리뷰에서 확인된 결함들의 회귀 테스트."""

from datetime import datetime, timedelta

from fivewhys.evaluation.harness import EvalSummary, EvalResult, evaluate
from fivewhys.logs.drain import DrainMiner
from fivewhys.models import KST, LogRecord
from fivewhys.rag.chunker import chunk_markdown
from fivewhys.store.es import ElasticsearchLogSearch


# --- drain: 와일드카드를 일치로 세면 생기는 폭주 병합 (seqDist 수정 검증) ----


def test_drain_wildcard_heavy_template_does_not_absorb_everything():
    miner = DrainMiner()
    messages = [
        "payment gateway timeout while calling upstream",
        "payment gateway timeout retries exhausted abort",
        "payment gateway circuit breaker opened now",
        "payment gateway warmup finished loading rules",
    ]
    ids = [miner.add(m) for m in messages]
    # 논문의 seqDist 의미론에서는 서로 다른 의미의 메시지가
    # 한 클러스터로 전부 흡수되면 안 된다
    assert len(set(ids)) >= 3


# --- chunker: max_chars보다 긴 단일 문단 강제 분할 --------------------------


def test_chunker_splits_single_long_paragraph():
    long_para = "가나다 " * 800  # ~3200자, 빈 줄 없음
    chunks = chunk_markdown("doc", f"# t\n\n## 본문\n\n{long_para}", max_chars=900)
    body = [c for c in chunks if c.section == "본문"]
    assert len(body) >= 3
    assert all(len(c.text) <= 1100 for c in body)  # 제목 프리픽스 여유 포함


# --- chunker: 코드 펜스 안의 '# 주석'을 헤딩으로 오인하지 않기 --------------


def test_chunker_ignores_headings_inside_code_fence():
    doc = """# 제목

## 대응 절차

```bash
# restart the pool
systemctl restart app
```

절차 끝.
"""
    chunks = chunk_markdown("doc", doc)
    sections = {c.section for c in chunks}
    assert "restart the pool" not in sections
    joined = " ".join(c.text for c in chunks if c.section == "대응 절차")
    assert "systemctl restart app" in joined


# --- es: id 없는 레코드 색인 + bulk 부분 실패 감지 --------------------------


class _StubES:
    def __init__(self, errors=False):
        self.operations = None
        self._errors = errors

    def bulk(self, operations, refresh):
        self.operations = operations
        if self._errors:
            return {
                "errors": True,
                "items": [{"index": {"error": {"type": "mapper_parsing_exception"}}}],
            }
        return {"errors": False, "items": []}


def _rec():
    return LogRecord(
        ts=datetime(2026, 7, 3, 14, 0, tzinfo=KST),
        service="svc",
        level="INFO",
        component="",
        message="hello",
    )


def test_es_bulk_index_without_ids_omits_id_field():
    stub = _StubES()
    es = ElasticsearchLogSearch(client=stub)
    es.bulk_index([_rec()])  # rec.id is None
    meta = stub.operations[0]["index"]
    assert "_id" not in meta


def test_es_bulk_index_raises_on_partial_failure():
    es = ElasticsearchLogSearch(client=_StubES(errors=True))
    rec = _rec()
    rec.id = 1
    try:
        es.bulk_index([rec])
        raise AssertionError("부분 실패가 예외로 드러나야 한다")
    except RuntimeError as e:
        assert "부분 실패" in str(e)


# --- harness: 인용 0건은 0.0이 아니라 None (환각과 구분) --------------------


def test_zero_citation_precision_excluded_from_mean():
    ok = dict(
        scenario="s", seed=1, root_service_correct=True, root_cause_keyword_hit=True,
        culprit_applicable=False, culprit_identified=False, evidence_count=3,
        whys_depth=3, n_turns=5, predicted_service="a",
    )
    summary = EvalSummary(engine="x")
    summary.results.append(EvalResult(evidence_precision=1.0, **ok))
    summary.results.append(EvalResult(evidence_precision=None, **{**ok, "evidence_count": 0}))
    assert summary.mean_evidence_precision == 1.0  # None은 평균에서 제외
    assert summary.no_citation_rate == 0.5


# --- harness: llm_factory로 실행마다 새 엔진 (상태 오염 방지) ---------------


def test_evaluate_with_llm_factory_multiple_runs():
    from fivewhys.agent.scripted import ScriptedLLM

    summary = evaluate(
        scenario_names=["disk_full"], seeds=(42, 43), llm_factory=ScriptedLLM
    )
    assert all(r.error is None for r in summary.results)
    assert summary.root_service_accuracy == 1.0


# --- scripted: 인스턴스 재사용 시 새 대화에서 상태 리셋 ---------------------


def test_scripted_llm_instance_reusable_across_conversations():
    from fivewhys.agent.five_whys import run_five_whys_agent
    from fivewhys.agent.scripted import ScriptedLLM
    from fivewhys.agent.tools import ToolExecutor
    from fivewhys.pipeline import build_retriever, build_store
    from fivewhys.scenarios.catalog import get_scenario

    llm = ScriptedLLM()
    for name in ("disk_full", "cert_expiry"):
        bundle = get_scenario(name)
        store, _ = build_store(bundle)
        retriever = build_retriever(store)
        executor = ToolExecutor.for_bundle(bundle, store, retriever)
        result = run_five_whys_agent(bundle, executor, llm)
        assert result.report.root_cause_service == bundle.ground_truth.root_cause_service
        store.close()


# --- tools: bucket_minutes=0 방어 -------------------------------------------


def test_error_timeline_bucket_zero_guarded():
    from fivewhys.agent.tools import ToolExecutor
    from fivewhys.pipeline import build_retriever, build_store
    from fivewhys.scenarios.catalog import get_scenario

    bundle = get_scenario("disk_full")
    store, _ = build_store(bundle)
    retriever = build_retriever(store)
    executor = ToolExecutor.for_bundle(bundle, store, retriever)
    content, is_error = executor.execute("get_error_timeline", {"bucket_minutes": 0})
    assert not is_error
    store.close()
