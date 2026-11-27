from fastapi.testclient import TestClient

from fivewhys.api import create_app


def test_scenarios_endpoint():
    client = TestClient(create_app())
    resp = client.get("/scenarios")
    assert resp.status_code == 200
    assert "db_pool_exhaustion" in resp.json()


def test_analyze_and_fetch_report():
    client = TestClient(create_app())
    resp = client.post(
        "/analyze", json={"scenario": "disk_full", "engine": "scripted", "seed": 42}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["report"]["root_cause_service"] == "inventory-service"
    assert body["scores"]["root_service_correct"] is True

    fetched = client.get(f"/reports/{body['report_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["report_id"] == body["report_id"]


def test_analyze_unknown_scenario_404():
    client = TestClient(create_app())
    resp = client.post("/analyze", json={"scenario": "nope", "engine": "scripted"})
    assert resp.status_code == 404


def test_claude_engine_requires_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = TestClient(create_app())
    resp = client.post("/analyze", json={"scenario": "disk_full", "engine": "claude"})
    assert resp.status_code == 400
