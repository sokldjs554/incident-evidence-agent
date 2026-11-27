"""Elasticsearch 백엔드 실습 — 같은 질의를 두 저장소에 던져 비교한다.

이 프로젝트의 로그 검색은 저장소 인터페이스 뒤에 숨어 있어서,
SQLite FTS5(BM25) → Elasticsearch(BM25)로 갈아끼워도 에이전트 코드는
한 줄도 바뀌지 않는다. 이 스크립트는 그 사실을 눈으로 보여준다.

사전 준비:
    docker compose up -d          # ES 기동 (localhost:9200)
    pip install -e ".[es]"        # elasticsearch 클라이언트 설치

실행:
    python examples/es_backend_demo.py
"""

from __future__ import annotations

import json

from fivewhys.pipeline import build_store
from fivewhys.scenarios.catalog import get_scenario
from fivewhys.store.es import ElasticsearchLogSearch

QUERY = "connection pool timeout"
SERVICE = "order-service"
ES_HOST = "http://localhost:9200"


def show(title: str, rows: list[dict]) -> None:
    print(f"\n── {title} — 상위 {len(rows)}건")
    for r in rows:
        print(f"   [{r['id']}] {r['ts']} {r['service']} {r['level']}  {r['message'][:70]}")


def main() -> int:
    # 1) 시나리오 로그를 SQLite에 적재 (여기서 각 로그에 id가 부여된다)
    bundle = get_scenario("db_pool_exhaustion")
    store, _ = build_store(bundle)
    print(f"시나리오: {bundle.title} · 로그 {len(bundle.logs)}건 적재 완료")

    # 2) SQLite FTS5(BM25)로 검색
    sqlite_rows = store.search_logs(QUERY, service=SERVICE, limit=5)
    show("SQLite FTS5 (bm25)", sqlite_rows)

    # 3) 어댑터가 만들어내는 Elasticsearch 질의(Query DSL) — 클러스터 불필요
    #    build_query는 클라이언트를 쓰지 않으므로 더미를 주입해 호출한다
    dsl = ElasticsearchLogSearch(client=object()).build_query(
        QUERY, service=SERVICE, level="ERROR", start=bundle.incident[0], end=bundle.incident[1]
    )
    print("\n── 어댑터가 생성하는 Elasticsearch Query DSL")
    print(json.dumps({"query": dsl}, ensure_ascii=False, indent=2))
    print(
        "   같은 인자(query/service/level/기간)가 SQLite에서는 FTS5 MATCH + WHERE로,\n"
        "   ES에서는 match + term/range 필터로 번역된다 — 호출부는 동일하다."
    )

    # 4) 실제 클러스터가 있으면 색인 후 같은 질의를 던져 비교
    try:
        es = ElasticsearchLogSearch(hosts=[ES_HOST], index="fivewhys-demo")
        es.ensure_index()
        es.bulk_index(bundle.logs)
    except ImportError as e:
        print(f"\n⏭  라이브 비교 건너뜀: {e}")
        print('   → 실제 클러스터로 비교하려면: docker compose up -d && pip install -e ".[es]"')
        store.close()
        return 0
    except Exception as e:
        print(f"\n⏭  라이브 비교 건너뜀 — ES 연결 실패: {type(e).__name__}: {e}")
        print(f"   → docker compose up -d 로 ES가 {ES_HOST} 에 떠 있는지 확인하세요.")
        store.close()
        return 0

    # 5) 같은 질의를 ES로 검색 — 호출 시그니처가 SQLite 저장소와 동일하다
    es_rows = es.search_logs(QUERY, service=SERVICE, limit=5)
    show("Elasticsearch (bm25)", es_rows)

    sqlite_ids = {r["id"] for r in sqlite_rows}
    es_ids = {r["id"] for r in es_rows}
    overlap = len(sqlite_ids & es_ids)
    print(
        f"\n두 백엔드 상위 5건 중 {overlap}건 일치 "
        f"(랭킹 구현이 달라 순서/점수는 다를 수 있다)"
    )
    print("핵심: search_logs() 호출부는 두 경우 모두 동일 — 저장소만 교체된다.")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
