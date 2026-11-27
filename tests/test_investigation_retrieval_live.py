"""Run against seeded Milvus/Neo4j. Graph-ACL canaries are rolled back."""
import os
from uuid import uuid4

import pytest

from fivewhys.investigation.retrieval import _EXPAND, _neo, index_documents, research, vector_search


pytestmark = pytest.mark.skipif(os.getenv("FIVEWHYS_INTEGRATION") != "1", reason="requires seeded Milvus and Neo4j")


def test_default_query_grounding_and_comparison():
    result = research("배포 이후 주문 서비스의 응답이 느려졌다. 어떤 변경이 관련돼 있나?", "orders")
    archived = next(e for e in result["vector"] if e["source"] == "investigation/cache-restart.md")
    assert archived["id"] not in {e["id"] for e in result["graph"]}
    assert any("AFFECTS" in d["reason"] for d in result["decisions"] if d["evidence_id"] == archived["id"])
    change = next(e for e in result["graph"] if e["kind"] == "deployment")
    assert "v2.4.0" in change["text"]
    assert any(e["kind"] == "log" and e["incident"] for e in result["graph"])
    assert all("orders" in e["teams"] for e in result["graph"] + result["vector"])


def test_milvus_acl_and_immutable_retries():
    unique_text = f"versioned-corpus-{uuid4().hex}"
    doc = dict(kind="log", title=unique_text, source="check/test.log", service="probe", text=unique_text)
    first = index_documents([doc], "orders")["evidence_ids"]
    assert index_documents([doc], "orders")["evidence_ids"] == first
    second = index_documents([{**doc, "text": unique_text + " revision two"}], "orders")["evidence_ids"]
    assert set(first).isdisjoint(second)
    allowed = {e["id"] for e in vector_search(unique_text, "orders", "log")}
    assert set(first + second) <= allowed
    denied = research(unique_text, "platform", "log")
    assert set(first + second).isdisjoint(e["id"] for e in denied["graph"] + denied["vector"])
    current = vector_search(unique_text, "orders", "log", current_only=True)
    assert all(e["origin"] == "scenario" for e in current)


def test_neo4j_denies_each_node_and_edge_in_real_multi_hop_query():
    driver = _neo(os.getenv("NEO4J_URI", "bolt://neo4j:7687"), os.getenv("NEO4J_USER", "neo4j"),
                  os.getenv("NEO4J_PASSWORD", "fivewhys-demo"))
    incident = "test/" + uuid4().hex
    anchor = incident + "/gateway-observation"
    with driver.session() as session, session.begin_transaction() as tx:
        tx.run("""
            CREATE (seed:Evidence {id:$anchor}), (origin:Service {name:'probe-api'}),
                   (s:Service {name:'probe-db'}), (e:Evidence {id:$target,kind:'deployment',
                    observed_at:'2026-07-03T13:20:00+09:00',incident:$incident}),
                   (i:Incident {name:$incident,scoped_topology:true,lookback:'2026-07-03T13:00:00+09:00',
                    start:'2026-07-03T14:00:00+09:00',end:'2026-07-03T14:15:00+09:00'}),
                   (seed)-[ob:OBSERVED_ON]->(origin)-[d:DEPENDS_ON {incident:$incident}]->(s)<-[ch:CHANGED]-(e),
                   (i)-[a:AFFECTS]->(s)
            FOREACH(n IN [seed,origin,s,e,i] | SET n.teams=['orders'], n.fixture=$incident)
            FOREACH(r IN [ob,d,ch,a] | SET r.teams=['orders'], r.fixture=$incident)
            """, anchor=anchor, incident=incident, target=incident+"/rollout").consume()
        def fetch():
            return list(tx.run(_EXPAND, ids=[anchor], team="orders", incident=incident))
        assert fetch()[0]["relations"] == ["OBSERVED_ON", "DEPENDS_ON", "CHANGED"]
        for pattern in ("(n {fixture:$fixture})", "()-[n {fixture:$fixture}]->()"):
            keys = [row["key"] for row in tx.run(f"MATCH {pattern} RETURN elementId(n) AS key", fixture=incident)]
            for key in keys:
                set_acl = f"MATCH {pattern} WHERE elementId(n)=$key SET n.teams=$teams"
                tx.run(set_acl, key=key, fixture=incident, teams=["platform"]).consume()
                assert fetch() == []
                tx.run(set_acl, key=key, fixture=incident, teams=["orders"]).consume()
        for foreign_incident in (None, incident + "/different"):
            tx.run("MATCH ()-[d:DEPENDS_ON {fixture:$fixture}]->() SET d.incident=$foreign",
                   fixture=incident, foreign=foreign_incident).consume()
            assert fetch() == []
        tx.rollback()
