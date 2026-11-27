"""하이브리드 검색 (BM25 + 벡터) + RRF 융합.

Elasticsearch의 hybrid search가 쓰는 것과 같은 Reciprocal Rank Fusion을
사용한다: score(d) = Σ 1 / (rrf_k + rank_i(d))

- BM25(키워드)는 정확한 용어 매칭(예: "HikariPool")에 강하고
- 벡터 검색은 표현이 달라도 의미가 겹치는 문서를 찾아준다
두 랭킹을 점수 스케일 보정 없이 순위만으로 합치는 것이 RRF의 장점이다.
"""

from __future__ import annotations

from dataclasses import dataclass

from fivewhys.rag.embedder import Embedder
from fivewhys.rag.vectorstore import VectorStore
from fivewhys.store.db import LogStore


@dataclass
class RetrievedChunk:
    chunk_id: int
    doc: str
    section: str
    text: str
    score: float


def rrf_fuse(
    rankings: list[list[int]], k: int = 60, top_k: int = 5
) -> list[tuple[int, float]]:
    """여러 랭킹(문서 id 순서 목록)을 RRF로 융합한다."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    ordered = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return ordered[:top_k]


class HybridRetriever:
    def __init__(
        self, store: LogStore, embedder: Embedder, vector_store: VectorStore
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._vectors = vector_store

    def index_chunk(self, doc: str, section: str, text: str) -> int:
        chunk_id = self._store.add_chunk(doc, section, text)
        self._vectors.add(chunk_id, self._embedder.embed(text))
        return chunk_id

    def search(self, query: str, k: int = 5, candidates: int = 20) -> list[RetrievedChunk]:
        bm25_ranking = [
            r["id"] for r in self._store.search_chunks(query, limit=candidates)
        ]
        vec_ranking = [
            cid
            for cid, _ in self._vectors.search(self._embedder.embed(query), k=candidates)
        ]
        fused = rrf_fuse([bm25_ranking, vec_ranking], top_k=k)

        results: list[RetrievedChunk] = []
        chunk_by_id = {c["id"]: c for c in self._store.all_chunks()}
        for chunk_id, score in fused:
            c = chunk_by_id.get(chunk_id)
            if c:
                results.append(
                    RetrievedChunk(
                        chunk_id=chunk_id,
                        doc=c["doc"],
                        section=c["section"],
                        text=c["text"],
                        score=round(score, 5),
                    )
                )
        return results
