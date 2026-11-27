"""적재 파이프라인: 시나리오/로그 → 저장소, 포스트모템 → 하이브리드 인덱스."""

from __future__ import annotations

from pathlib import Path

from fivewhys.logs.drain import DrainMiner
from fivewhys.models import LogRecord
from fivewhys.rag.chunker import chunk_markdown
from fivewhys.rag.embedder import HashingEmbedder
from fivewhys.rag.retriever import HybridRetriever
from fivewhys.rag.vectorstore import VectorStore
from fivewhys.scenarios.generator import ScenarioBundle
from fivewhys.store.db import LogStore

DEFAULT_POSTMORTEM_DIR = Path(__file__).parent / "data" / "postmortems"


def ingest_records(store: LogStore, records: list[LogRecord]) -> DrainMiner:
    """레코드에 Drain 템플릿 id를 배정하고 저장소에 적재한다."""
    miner = DrainMiner()
    for rec in records:
        rec.template_id = miner.add(rec.message)
    store.add_logs(records)
    store.set_templates(miner.templates())
    return miner


def build_store(
    bundle: ScenarioBundle, db_path: str = ":memory:", check_same_thread: bool = True
) -> tuple[LogStore, DrainMiner]:
    store = LogStore(db_path, check_same_thread=check_same_thread)
    miner = ingest_records(store, bundle.logs)
    store.add_deploys(bundle.deploys)
    return store, miner


def build_retriever(
    store: LogStore, postmortem_dir: Path | None = None, dim: int = 512
) -> HybridRetriever:
    """포스트모템 문서를 청킹→임베딩→색인해 하이브리드 검색기를 만든다."""
    postmortem_dir = postmortem_dir or DEFAULT_POSTMORTEM_DIR
    embedder = HashingEmbedder(dim=dim)
    retriever = HybridRetriever(store, embedder, VectorStore(dim=dim))
    for path in sorted(postmortem_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for chunk in chunk_markdown(path.stem, text):
            retriever.index_chunk(chunk.doc, chunk.section, chunk.text)
    return retriever
