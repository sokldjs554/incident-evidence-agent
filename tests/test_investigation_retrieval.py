from datetime import timedelta
import hashlib
from pathlib import Path
import re

import pytest

from fivewhys.investigation.retrieval import _scope
from fivewhys.investigation.retrieval_corpus import REPORT_DIR, seed_rows, upload_rows
from fivewhys.scenarios.catalog import get_scenario


def test_seed_citations_resolve_to_original_sources():
    bundle = get_scenario("db_pool_exhaustion", 42)
    start, end = bundle.incident
    rows = seed_rows()
    assert len({r["id"] for r in rows}) == len(rows)
    for row in rows:
        if row["kind"] == "report":
            source = (REPORT_DIR.parent / row["source"]).read_text(encoding="utf-8")
        elif row["kind"] == "log":
            service = row["service"]
            component, level = Path(row["source"]).stem.rsplit("-", 1)
            records = [x for x in bundle.logs if x.service == service and x.component == component
                       and x.level == level and start <= x.ts < end]
            source = "\n".join(f"{x.ts.isoformat()} {x.level} [{x.component}] {x.message}" for x in records)
            assert row["observed_at"] == records[0].ts.isoformat()
            assert row["end_at"] == records[-1].ts.isoformat()
        elif row["kind"] == "deployment":
            [event] = [x for x in bundle.deploys if start - timedelta(hours=1) <= x.ts <= start]
            assert bundle.ground_truth.culprit_change_match in event.description
            source = f"{event.ts.isoformat()} {event.kind} {event.description}"
        else:
            continue
        assert row["version"] == "sha256:" + hashlib.sha256(source.encode()).hexdigest()
        begin, last = map(int, re.fullmatch(r"L(\d+)-L(\d+)", row["locator"]).groups())
        assert row["text"] == "\n".join(source.splitlines()[begin - 1:last])
    assert all("orders" not in r["teams"] for r in rows if r["service"] == "mysql")
    # Compare the companion case to a separate original scenario, not the main incident.
    comparison = get_scenario("cache_stampede", 42)
    assert comparison.ground_truth.root_cause_service == "redis-cache"
    assert any(x.service == "order-service" and "Cache miss" in x.message for x in comparison.logs)
    assert any(x.service == "api-gateway" and "Upstream timeout" in x.message for x in comparison.logs)


def test_index_acl_version_and_semantic_boundaries():
    doc = dict(kind="runbook", source="manual.md", title="Recovery", service="api-gateway",
               text="# Recovery\n1. Inspect\n  collect records\n2. Restore", version="r1")
    initial = upload_rows([doc], "orders")
    assert initial == upload_rows([doc], "orders")
    assert [r["locator"] for r in initial] == ["L2-L3", "L4-L4"]
    revised = upload_rows([{**doc, "text": doc["text"] + " and verify"}], "orders")
    assert {r["id"] for r in initial}.isdisjoint(r["id"] for r in revised)
    other_team = upload_rows([doc], "platform")
    assert {r["id"] for r in initial}.isdisjoint(r["id"] for r in other_team)
    with pytest.raises(ValueError):
        upload_rows([{**doc, "teams": ["platform"]}], "orders")
    with pytest.raises(ValueError):
        _scope('orders" or true')
    with pytest.raises(ValueError):
        _scope("orders", "log or true")


def test_deployment_entries_own_their_multiline_notes():
    docs = [dict(kind="deployment", title="Releases", service="order-service", source="changes.txt",
                 text="2026-07-03 12:00 release A\n  note for A\n2026-07-03 13:20 release B\n  note for B")]
    rows = upload_rows(docs, "orders")
    assert [r["locator"] for r in rows] == ["L1-L2", "L3-L4"]
    assert rows[1]["text"].endswith("note for B")


def test_long_step_keeps_exact_body_and_rejects_unstorable_batch_before_connecting(monkeypatch):
    import json
    from fivewhys.investigation import retrieval
    text = "1. " + "조" * 10920
    doc = dict(kind="runbook", title="점검", source="manual.md", text=text, service="orders")
    [row] = upload_rows([doc], "orders")
    assert row["text"] == text and row["locator"] == "L1-L1"
    assert len(json.dumps(row, ensure_ascii=False).encode()) < 65536
    monkeypatch.setattr(retrieval, "_stores", lambda: pytest.fail("invalid batch must never write"))
    for invalid in ("bad\x00value", "\x01" * 32760):
        with pytest.raises(ValueError):
            retrieval.index_documents([doc, {**doc, "text": invalid}], "orders")


def test_judgments_require_distinct_immutable_ids_even_when_coordinates_collide():
    from fivewhys.evaluation.retrieval_benchmark import metrics
    same_coordinates = {"source": "archive.md", "locator": "L1-L3"}
    rows = [{"id": "foreign-version", **same_coordinates}, {"id": "gold", **same_coordinates},
            {"id": "gold", **same_coordinates}]
    assert metrics(rows, {"gold", "other-gold"}) == {"recall": 0.5, "reciprocal_rank": 0.5}


def test_certificate_incident_uses_its_own_observations_and_service_permissions():
    rows = seed_rows(scenario="cert_expiry", seed=17)
    observations = [r for r in rows if r["origin"] == "scenario"]
    assert {r["incident"] for r in observations} == {"cert_expiry/seed-17"}
    assert {r["kind"] for r in observations} == {"log"}
    assert {r["source"].split("/logs/")[1] for r in observations} == {
        "api-gateway/proxy-ERROR.log", "user-service/auth-ERROR.log",
        "user-service/tls-ERROR.log", "payment-service/auth-WARN.log",
    }
    assert {r["service"] for r in observations if "orders" in r["teams"]} == {"api-gateway"}
    assert all("platform" in r["teams"] for r in observations)
    tls = next(r for r in observations if "/tls-" in r["source"])
    events = [log for log in get_scenario("cert_expiry", 17).logs if log.component == "tls"]
    assert tls["observed_at"] == events[0].ts.isoformat()
    assert tls["text"] == "\n".join(f"{e.ts.isoformat()} ERROR [tls] {e.message}" for e in events[:3])
    assert {r["id"] for r in rows if r["origin"] != "scenario"} == {
        r["id"] for r in seed_rows() if r["origin"] != "scenario"
    }


def test_search_filters_match_case_and_seed_in_both_search_modes():
    shared = _scope("platform", scenario="cert_expiry", seed=17)
    current = _scope("orders", "log", True, scenario="cert_expiry", seed=17)
    assert 'payload["incident"] == "cert_expiry/seed-17"' in shared
    assert 'payload["origin"] != "scenario"' in shared
    assert 'payload["incident"] == "cert_expiry/seed-17"' in current
    assert 'payload["origin"] == "scenario"' in current
    with pytest.raises(ValueError):
        _scope("orders", scenario='cert_expiry" or true')


def test_scripted_tls_claim_requires_observed_expiry_and_cites_downstream_symptoms():
    from fivewhys.investigation.analysis import _candidates

    gateway = {"id": "gateway", "service": "edge", "text": "Upstream 401 from login-svc POST /login"}
    token = {"id": "tokens", "service": "login-svc", "text": "Token issuance failed: upstream TLS error"}
    expired = {"id": "handshake", "service": "login-svc",
               "text": "SSL handshake failed with auth-provider: certificate expired"}
    old = {**expired, "title": "Previous TLS incident", "id": "archived"}
    rows = {"log": [gateway, token], "deployment": [], "report": [old]}
    assert _candidates(rows) == []
    rows["log"].append(expired)
    [candidate] = _candidates(rows)
    assert "인증서 만료" in candidate["title"] and "login-svc" in candidate["title"]
    assert set(candidate["evidence_ids"]) == {"handshake", "tokens", "gateway"}


def test_case_policy_changes_current_evidence_without_relabeling_shared_documents():
    from fivewhys.investigation.retrieval_corpus import rows_for_bundle

    bundle = get_scenario("cert_expiry", 17)
    original = rows_for_bundle(bundle, "cert_expiry/seed-17")
    bundle.service_teams["api-gateway"] = ("platform",)
    restricted = rows_for_bundle(bundle, "cert_expiry/seed-17")
    assert all("orders" not in r["teams"] for r in restricted if r["origin"] == "scenario")
    assert [r for r in restricted if r["origin"] != "scenario"] == [r for r in original if r["origin"] != "scenario"]
    assert "orders" in get_scenario("cert_expiry", 17).service_teams["api-gateway"]


def test_change_window_uses_the_selected_scenario_baseline():
    from fivewhys.investigation.retrieval_corpus import rows_for_bundle

    bundle = get_scenario("db_pool_exhaustion", 42)
    # The order rollout is 40 minutes before incident start. A fixture whose
    # observation period begins only 20 minutes earlier cannot attribute it.
    start = bundle.incident[0]
    bundle.baseline = (start - timedelta(minutes=20), start)
    rows = rows_for_bundle(bundle, "db_pool_exhaustion/seed-42")
    assert any(r["kind"] == "log" for r in rows)
    assert not any(r["kind"] == "deployment" for r in rows)
