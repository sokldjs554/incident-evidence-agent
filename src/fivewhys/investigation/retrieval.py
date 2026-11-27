"""Team-scoped LangChain retrieval over Milvus, followed by Neo4j path verification."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from neo4j import GraphDatabase
from pymilvus import DataType, MilvusClient

from fivewhys.investigation.retrieval_corpus import (
    DEFAULT_SCENARIO, DEFAULT_SEED, KINDS, TEAMS,
    incident_key, rows_for_bundle, upload_rows, validate_team,
)
from fivewhys.rag.embedder import HashingEmbedder
from fivewhys.scenarios.catalog import get_scenario

TOP_K = 4
RELATIONS = {"log": "OBSERVED_ON", "deployment": "CHANGED", "report": "DESCRIBES", "runbook": "GUIDES"}
CAUSAL = re.compile(r"원인|왜|배포|변경|연쇄|롤백|관련|cause|why|deploy|chang|rollout|rollback", re.I)


class IncidentEmbeddings(Embeddings):
    """Reuse the original reproducible 512-dimensional lexical embedding."""
    encoder = HashingEmbedder()

    def embed_documents(self, texts):
        return [self.encoder.embed(t) for t in texts]

    def embed_query(self, text):
        return self.encoder.embed(text)


@lru_cache(maxsize=4)
def _milvus(uri):
    return MilvusClient(uri=uri, timeout=15)


@lru_cache(maxsize=4)
def _neo(uri, user, password):
    return GraphDatabase.driver(uri, auth=(user, password), connection_timeout=10)


def _stores():
    return (_milvus(os.getenv("MILVUS_URI", "http://milvus:19530")),
            os.getenv("MILVUS_COLLECTION", "fivewhys_evidence"),
            _neo(os.getenv("NEO4J_URI", "bolt://neo4j:7687"), os.getenv("NEO4J_USER", "neo4j"),
                 os.getenv("NEO4J_PASSWORD", "fivewhys-demo")))


def _initialize(client, collection, driver):
    if not client.has_collection(collection):
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, max_length=64, is_primary=True)
        schema.add_field("kind", DataType.VARCHAR, max_length=20)
        for team in TEAMS:
            schema.add_field("allow_" + team, DataType.BOOL)
        schema.add_field("payload", DataType.JSON)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=512)
        indexes = MilvusClient.prepare_index_params()
        indexes.add_index(field_name="vector", index_type="FLAT", metric_type="COSINE")
        client.create_collection(collection, schema=schema, index_params=indexes, consistency_level="Strong")
    client.load_collection(collection)
    for label, field in (("Evidence", "id"), ("Service", "key"), ("Incident", "key")):
        driver.execute_query(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{field} IS UNIQUE")


def _scope(team, kind=None, current_only=False, *, scenario=DEFAULT_SCENARIO, seed=DEFAULT_SEED):
    validate_team(team)
    if kind is not None and kind not in KINDS:
        raise ValueError("unknown evidence kind")
    predicate = "allow_" + team + " == true"
    incident = json.dumps(incident_key(scenario, seed))
    if current_only:
        predicate += f' and payload["origin"] == "scenario" and payload["incident"] == {incident}'
    else:
        predicate += f' and (payload["origin"] != "scenario" or payload["incident"] == {incident})'
    return predicate if kind is None else f'{predicate} and kind == "{kind}"'


def embedding_text(evidence):
    return f"{evidence['title']}\n{evidence['service']}\n{evidence['text']}"


def _write_rows(rows, client, collection, driver):
    # The evidence ID commits to source content, metadata and ACL. An idempotent
    # retry can finish the vector write even if a worker died after the graph write.
    for kind, relation in RELATIONS.items():
        group = [{k: row[k] for k in ("id", "service", "teams", "kind", "observed_at", "incident")}
                 for row in rows if row["kind"] == kind]
        if group:
            driver.execute_query(f"""
                UNWIND $rows AS row
                MERGE (e:Evidence {{id:row.id}}) SET e += row
                WITH row,e UNWIND row.teams AS team
                MERGE (s:Service {{key: team + '/' + row.service}})
                SET s.name=row.service, s.teams=[team]
                MERGE (e)-[r:{relation}]->(s) SET r.teams=[team]
                """, rows=group)
    embedder = IncidentEmbeddings()
    for offset in range(0, len(rows), 64):
        batch = rows[offset:offset + 64]
        documents = [Document(id=row["id"], page_content=embedding_text(row), metadata=row) for row in batch]
        vectors = embedder.embed_documents([document.page_content for document in documents])
        records = []
        for document, vector in zip(documents, vectors):
            evidence = document.metadata
            records.append(dict(id=document.id, kind=evidence["kind"], payload=evidence, vector=vector,
                                allow_orders="orders" in evidence["teams"], allow_platform="platform" in evidence["teams"]))
        client.upsert(collection, records)


def seed_corpus(scenario=DEFAULT_SCENARIO, seed=DEFAULT_SEED):
    incident = incident_key(scenario, seed)
    bundle = get_scenario(scenario, seed)
    start, end = bundle.incident
    rows = rows_for_bundle(bundle, incident)
    client, collection, driver = _stores()
    _initialize(client, collection, driver)
    # The fixture's directed service topology is scoped by team. A tenant upload
    # cannot attach itself to a differently scoped Service or create an AFFECTS edge.
    for team in TEAMS:
        services = sorted({r["service"] for r in rows if r["origin"] == "scenario"
                           and r["kind"] == "log" and team in r["teams"]})
        driver.execute_query("""
            MERGE (i:Incident {key:$team + '/' + $incident})
            SET i.name=$incident, i.teams=[$team], i.start=$start, i.end=$end, i.lookback=$lookback,
                i.scoped_topology=true
            WITH i UNWIND $services AS service
            MERGE (s:Service {key:$team + '/' + service}) SET s.name=service, s.teams=[$team]
            MERGE (i)-[a:AFFECTS]->(s) SET a.teams=[$team]
            """, team=team, incident=incident, services=services, start=start.isoformat(),
            end=end.isoformat(), lookback=bundle.baseline[0].isoformat())
        for caller, callee in bundle.dependencies:
            if caller not in services or callee not in services:
                continue
            driver.execute_query("""
                MATCH (caller:Service {key:$caller}), (callee:Service {key:$callee})
                MERGE (caller)-[r:DEPENDS_ON {incident:$incident}]->(callee) SET r.teams=[$team]
                """, caller=f"{team}/{caller}", callee=f"{team}/{callee}", team=team, incident=incident)
    _write_rows(rows, client, collection, driver)
    return {"chunks": len(rows), "incident": incident, "seed": seed}


def index_documents(documents, team):
    rows = upload_rows(documents, team)
    client, collection, driver = _stores()
    _write_rows(rows, client, collection, driver)
    ids = list(dict.fromkeys(row["id"] for row in rows))
    return {"documents": len(documents), "chunks": len(ids), "evidence_ids": ids}


class MilvusEvidenceRetriever(BaseRetriever):
    team: str
    kind: str | None = None
    current_only: bool = False
    scenario: str = DEFAULT_SCENARIO
    seed: int = DEFAULT_SEED

    def _get_relevant_documents(self, query, *, run_manager):
        client, collection, _ = _stores()
        hits = client.search(collection, data=[IncidentEmbeddings().embed_query(query)],
                             filter=_scope(self.team, self.kind, self.current_only, scenario=self.scenario, seed=self.seed),
                             anns_field="vector",
                             search_params={"metric_type": "COSINE"}, limit=TOP_K * 8,
                             output_fields=["payload"], consistency_level="Strong")[0]
        ranked = sorted(hits, key=lambda hit: (-hit["distance"], hit["id"]))
        # One best passage per source avoids a long historical report occupying
        # all four positions. Both comparison arms receive the same vector ranking.
        seen = set()
        docs = []
        for hit in ranked:
            row = hit["entity"]["payload"]
            source_key = (row["origin"], row["source"], row["version"])
            if source_key not in seen:
                docs.append(Document(page_content=row["text"], metadata=row))
                seen.add(source_key)
            if len(docs) == TOP_K:
                break
        return docs


def vector_search(question, team, kind=None, current_only=False, *, scenario=DEFAULT_SCENARIO, seed=DEFAULT_SEED):
    _scope(team, kind, current_only, scenario=scenario, seed=seed)
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must contain text")
    retriever = MilvusEvidenceRetriever(team=team, kind=kind, current_only=current_only, scenario=scenario, seed=seed)
    return [doc.metadata for doc in retriever.invoke(question)]



_ALLOWED_PATH = """
    all(n IN nodes(p) WHERE $team IN n.teams)
    AND all(r IN relationships(p) WHERE $team IN r.teams)
"""
_PATH_COLUMNS = """
    [n IN nodes(p) | coalesce(n.id,n.name)] AS nodes,
    [r IN relationships(p) | type(r)] AS relations,
    [r IN relationships(p) | {source:coalesce(startNode(r).id,startNode(r).name),
      target:coalesce(endNode(r).id,endNode(r).name),type:type(r)}] AS edges
"""
_VERIFY = f"""
    MATCH p=(e:Evidence)-->(s:Service)
    WHERE e.id IN $ids AND {_ALLOWED_PATH}
    OPTIONAL MATCH (i:Incident)-[a:AFFECTS]->(s)
    WHERE i.name=$incident AND $team IN i.teams AND $team IN a.teams
    RETURN e.id AS evidence_id, i.name AS affected_incident, {_PATH_COLUMNS}
"""
# Only real incident changes in the lookback can be promoted. The seed and
# candidate must independently pass the Milvus ACL, and every segment of both
# Neo4j paths must pass the node and relationship ACL.
_EXPAND = f"""
    MATCH p=(seed:Evidence)-->(origin:Service)-[:DEPENDS_ON*0..1]-(s:Service)<-[:CHANGED|OBSERVED_ON]-(e:Evidence)
    MATCH (i:Incident)-[a:AFFECTS]->(s)
    WHERE seed.id IN $ids AND seed.id <> e.id AND {_ALLOWED_PATH}
      AND $team IN i.teams AND $team IN a.teams AND i.name=$incident AND e.incident=i.name
      AND all(r IN relationships(p) WHERE type(r) <> 'DEPENDS_ON' OR r.incident=i.name
               OR (r.incident IS NULL AND i.scoped_topology IS NULL))
      AND ((e.kind='deployment' AND datetime(e.observed_at) >= datetime(i.lookback)
                                AND datetime(e.observed_at) <= datetime(i.start))
        OR (e.kind='log' AND datetime(e.observed_at) >= datetime(i.start)
                         AND datetime(e.observed_at) < datetime(i.end)))
    RETURN e.id AS evidence_id, seed.id AS anchor, e.observed_at AS time, i.name AS incident,
           e.kind AS kind, {_PATH_COLUMNS}
"""


def _expand(order, known, paths, client, collection, driver, predicate, team, kind, incident):
    candidates = driver.execute_query(_EXPAND, ids=order, team=team, incident=incident).records
    if not candidates:
        return
    ids = sorted({row["evidence_id"] for row in candidates})
    visible = client.query(collection, filter=predicate + " and id in " + json.dumps(ids),
                           output_fields=["payload"], consistency_level="Strong")
    known.update((hit["payload"]["id"], hit["payload"]) for hit in visible)
    anchors = list(order)
    insertion = 1
    for target_kind in (("deployment", "log") if kind is None else (kind,)):
        options = [r for r in candidates if r["kind"] == target_kind and r["evidence_id"] in known]
        # For changes use the most recent related rollout. For observations,
        # favor the strongest seed and shortest path, then the earliest event.
        options.sort(key=lambda r: (anchors.index(r["anchor"]), len(r["nodes"]), r["time"], r["evidence_id"]))
        if target_kind == "deployment":
            options.sort(key=lambda r: r["time"], reverse=True)
        if not options or known[order[0]]["kind"] == target_kind:
            continue
        choice = options[0]
        eid = choice["evidence_id"]
        if eid in order:
            order.remove(eid)
        order.insert(insertion, eid)
        insertion += 1
        path = choice.data()
        path["incident_edge"] = {"source": path["incident"], "type": "AFFECTS", "target": path["edges"][-1]["target"]}
        paths[eid] = path


def research(question, team, kind=None, current_only=False, *, scenario=DEFAULT_SCENARIO, seed=DEFAULT_SEED):
    incident = incident_key(scenario, seed)
    predicate = _scope(team, kind, current_only, scenario=scenario, seed=seed)
    vector = vector_search(question, team, kind, current_only, scenario=scenario, seed=seed)
    client, collection, driver = _stores()
    known = {e["id"]: e for e in vector}
    if not known:
        return {"vector": [], "graph": [], "paths": [], "decisions": []}
    verified = driver.execute_query(_VERIFY, team=team, ids=list(known), incident=incident).records
    paths = {row["evidence_id"]: row.data() for row in verified}
    causal = bool(CAUSAL.search(question))
    order = []
    reasons = {}
    for row in vector:
        eid = row["id"]
        if eid not in paths:
            reasons[eid] = "팀 권한으로 확인되는 서비스 연결이 없어 제외했다."
        elif causal and row["origin"] == "archive" and not paths[eid]["affected_incident"]:
            reasons[eid] = "과거 보고서의 서비스가 이번 장애의 영향 범위(AFFECTS)에 없어 제외했다."
        else:
            order.append(eid)
    if order and causal and kind in (None, "deployment", "log"):
        _expand(order, known, paths, client, collection, driver, predicate, team, kind, incident)
    order = order[:TOP_K]
    decisions = []
    for eid in dict.fromkeys([*(e["id"] for e in vector), *order]):
        if eid not in order:
            reason = reasons.get(eid, "이번 장애 시간대의 연결 근거가 우선되어 상위 4개에서 제외됐다.")
        elif "incident_edge" in paths[eid]:
            reason = "영향 서비스와의 경로 및 배포·로그의 시간 범위를 확인해 우선했다."
        elif known[eid]["origin"] == "archive":
            reason = "과거 보고서의 서비스 연결을 확인했다. 현재 원인 판단에는 이번 사건의 로그가 더 필요하다."
        else:
            reason = "이 근거가 속한 서비스와 팀의 열람 권한을 확인했다."
        decisions.append(dict(evidence_id=eid, action="retained" if eid in order else "excluded", reason=reason))
    return {"vector": vector, "graph": [known[eid] for eid in order], "paths": [paths[eid] for eid in order],
            "decisions": decisions}


if __name__ == "__main__":
    print(json.dumps([seed_corpus(), seed_corpus("cert_expiry")], ensure_ascii=False))
