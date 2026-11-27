"""Elasticsearch 검색 백엔드 어댑터 (선택 사항).

기본 저장소는 SQLite FTS5(bm25)지만, 운영 규모의 로그 플랫폼에서는
Elasticsearch를 쓰는 것이 일반적이다. 이 어댑터는 LogStore.search_logs와
동일한 시그니처를 제공하므로 ToolExecutor에 그대로 끼울 수 있다.

사용법:
    pip install "fivewhys-agent[es]"
    docker compose up -d elasticsearch
    es = ElasticsearchLogSearch(hosts=["http://localhost:9200"])
    es.bulk_index(records)
    es.search_logs("connection pool timeout", service="order-service")
"""

from __future__ import annotations

from datetime import datetime

from fivewhys.models import LogRecord

def _coerce_id(raw) -> int | str:
    """ES 문서 id를 정수로 되돌린다.

    LogStore를 거쳐 색인하면 id가 정수 문자열이지만, id 없이 색인하면
    ES가 임의 문자열 id를 생성한다. 후자를 int()로 변환하려다 터지지 않도록
    변환 실패 시 원본 문자열을 그대로 쓴다.
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "ts": {"type": "date"},
            "service": {"type": "keyword"},
            "level": {"type": "keyword"},
            "component": {"type": "keyword"},
            "message": {"type": "text"},
            "template_id": {"type": "integer"},
        }
    }
}


class ElasticsearchLogSearch:
    def __init__(
        self,
        hosts: list[str] | None = None,
        index: str = "fivewhys-logs",
        client=None,
    ) -> None:
        if client is None:
            try:
                from elasticsearch import Elasticsearch
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "elasticsearch 패키지가 필요합니다: pip install 'fivewhys-agent[es]'"
                ) from e
            client = Elasticsearch(hosts=hosts or ["http://localhost:9200"])
        self._es = client
        self._index = index

    def ensure_index(self) -> None:
        if not self._es.indices.exists(index=self._index):
            self._es.indices.create(index=self._index, **INDEX_MAPPING)

    def bulk_index(self, records: list[LogRecord]) -> None:
        operations: list[dict] = []
        for rec in records:
            meta: dict = {"index": {"_index": self._index}}
            if rec.id is not None:  # id가 없으면 ES가 자동 생성
                meta["index"]["_id"] = rec.id
            operations.append(meta)
            operations.append(
                {
                    "ts": rec.ts.isoformat(),
                    "service": rec.service,
                    "level": rec.level,
                    "component": rec.component,
                    "message": rec.message,
                    "template_id": rec.template_id,
                }
            )
        if not operations:
            return
        resp = self._es.bulk(operations=operations, refresh=True)
        # _bulk는 부분 실패도 HTTP 200으로 돌려준다 — errors 플래그를 확인해야 한다
        if resp.get("errors"):
            failed = [
                item["index"].get("error")
                for item in resp.get("items", [])
                if item.get("index", {}).get("error")
            ]
            raise RuntimeError(f"bulk 색인 부분 실패 {len(failed)}건: {failed[:3]}")

    def build_query(
        self,
        query: str,
        service: str | None = None,
        level: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict:
        must: list[dict] = [{"match": {"message": {"query": query, "operator": "or"}}}]
        filters: list[dict] = []
        if service:
            filters.append({"term": {"service": service}})
        if level:
            filters.append({"term": {"level": level}})
        if start or end:
            rng: dict = {}
            if start:
                rng["gte"] = start.isoformat()
            if end:
                rng["lte"] = end.isoformat()
            filters.append({"range": {"ts": rng}})
        return {"bool": {"must": must, "filter": filters}}

    def search_logs(
        self,
        query: str,
        service: str | None = None,
        level: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 20,
    ) -> list[dict]:
        body = self.build_query(query, service, level, start, end)
        resp = self._es.search(index=self._index, query=body, size=limit)
        out = []
        for hit in resp["hits"]["hits"]:
            src = hit["_source"]
            out.append(
                {
                    "id": _coerce_id(hit["_id"]),
                    "ts": src["ts"],
                    "service": src["service"],
                    "level": src["level"],
                    "component": src.get("component", ""),
                    "message": src["message"],
                    "template_id": src.get("template_id"),
                    "rank": -float(hit.get("_score") or 0.0),
                }
            )
        return out
