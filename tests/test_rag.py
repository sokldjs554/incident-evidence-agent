from fivewhys.rag.chunker import chunk_markdown
from fivewhys.rag.embedder import HashingEmbedder
from fivewhys.rag.retriever import HybridRetriever, rrf_fuse
from fivewhys.rag.vectorstore import VectorStore
from fivewhys.store.db import LogStore

DOC = """# 장애 보고서: 커넥션 풀 고갈

## 요약
slow query로 인해 connection pool이 고갈되었다.

## 재발 방지 대책
- 인덱스 추가
- 커넥션 풀 알람 추가
"""


def test_chunk_by_sections():
    chunks = chunk_markdown("doc1", DOC)
    sections = {c.section for c in chunks}
    assert "요약" in sections
    assert "재발 방지 대책" in sections
    # 섹션 제목이 청크 본문에 포함되어 검색 신호가 된다
    summary = next(c for c in chunks if c.section == "요약")
    assert "요약" in summary.text


def test_long_section_split_with_overlap():
    long_doc = "# t\n\n## 본문\n\n" + "\n\n".join(f"문단 {i} " + "x" * 200 for i in range(10))
    chunks = chunk_markdown("doc2", long_doc, max_chars=500, overlap_chars=50)
    body_chunks = [c for c in chunks if c.section == "본문"]
    assert len(body_chunks) >= 3
    assert all(len(c.text) <= 700 for c in body_chunks)


def test_embedder_deterministic_and_normalized():
    emb = HashingEmbedder(dim=256)
    v1 = emb.embed("HikariPool connection timeout")
    v2 = emb.embed("HikariPool connection timeout")
    assert v1 == v2
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_embedder_similarity_ordering():
    emb = HashingEmbedder()
    q = emb.embed("connection pool exhausted timeout")
    similar = emb.embed("database connection pool timeout errors")
    unrelated = emb.embed("certificate expired tls handshake failure")
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    assert dot(q, similar) > dot(q, unrelated)


def test_rrf_prefers_doc_in_both_rankings():
    fused = rrf_fuse([[1, 2, 3], [3, 1, 4]], top_k=4)
    ids = [i for i, _ in fused]
    assert ids[0] == 1  # 두 랭킹 모두 상위 → 최상위


def test_hybrid_retriever_end_to_end():
    store = LogStore()
    emb = HashingEmbedder(dim=256)
    retriever = HybridRetriever(store, emb, VectorStore(dim=256))
    retriever.index_chunk("pool-doc", "요약", "slow query로 connection pool 고갈, HikariPool timeout")
    retriever.index_chunk("cert-doc", "요약", "TLS certificate expired, SSL handshake 실패")
    retriever.index_chunk("disk-doc", "요약", "No space left on device, 디스크 용량 고갈")

    results = retriever.search("HikariPool connection pool 고갈", k=2)
    assert results
    assert results[0].doc == "pool-doc"
    store.close()
