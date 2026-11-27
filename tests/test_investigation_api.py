from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from fivewhys.api import create_app


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("FIVEWHYS_DEMO_ACCOUNTS", "1")
    return TestClient(create_app())


def test_incident_page_and_auth_boundary(client, monkeypatch):
    assert client.get("/").status_code == 200
    body = {"question": "어떤 배포가 관련됐나요?"}
    assert client.post("/v1/analyses", json=body).status_code == 401
    monkeypatch.setenv("FIVEWHYS_ORDERS_TOKEN", "orders-test")
    monkeypatch.setattr("fivewhys.investigation.routes.submit_job", lambda kind, payload, team: str(uuid4()))
    response = client.post("/v1/analyses", json=body, headers={"Authorization": "Bearer orders-test"})
    assert response.status_code == 202
    assert response.headers["location"].endswith(response.json()["id"])
    assert client.post("/v1/analyses", json={**body, "team": "platform"}).status_code == 401


def test_demo_session_and_job_visibility(client, monkeypatch):
    from fivewhys.investigation import routes

    job_id = str(uuid4())
    evidence = {"id": "E-18", "kind": "log", "title": "pool", "text": "pool full", "source": "order.log", "version": "v1", "service": "order-service", "locator":"L18"}
    result = {"engine": "scripted", "hypotheses": [], "evidence": [evidence], "vector": [], "graph": [], "paths": []}
    def lookup(key, team):
        if team == "orders" and key == job_id:
            return {"id": key, "status": "completed", "kind": "analysis", "team": team, "result": result, "error": None, "llm_calls": 0, "callbacks": []}

    monkeypatch.setattr(routes, "lookup_job", lookup)
    assert client.post("/v1/demo/session", json={"team":"orders"}).status_code == 200
    assert "result" not in client.get(f"/v1/jobs/{job_id}").json()
    assert client.get(f"/v1/jobs/{job_id}/evidence/E-18").json()["text"] == "pool full"
    assert client.get(f"/v1/jobs/{job_id}/result").json()["evidence"][0]["id"] == "E-18"
    client.post("/v1/demo/session", json={"team":"platform"})
    for suffix in ("", "/result", "/evidence/E-18"):
        assert client.get(f"/v1/jobs/{job_id}{suffix}").status_code == 404


def test_running_job_result_returns_409(client, monkeypatch):
    from fivewhys.investigation import routes

    job_id = str(uuid4())

    def lookup(key, team):
        if team == "orders" and key == job_id:
            return {
                "id": key,
                "status": "running",
                "kind": "analysis",
                "team": team,
                "result": None,
                "error": None,
                "llm_calls": 0,
                "callbacks": [],
            }

    monkeypatch.setattr(routes, "lookup_job", lookup)
    assert client.post("/v1/demo/session", json={"team": "orders"}).status_code == 200

    response = client.get(f"/v1/jobs/{job_id}/result")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "완료된 결과가 없습니다. 작업 상태와 오류를 확인하세요."
    }


def test_failed_job_result_returns_409(client, monkeypatch):
    from fivewhys.investigation import routes

    job_id = str(uuid4())

    def lookup(key, team):
        if team == "orders" and key == job_id:
            return {
                "id": key,
                "status": "failed",
                "kind": "analysis",
                "team": team,
                "result": None,
                "error": "analysis failed",
                "llm_calls": 0,
                "callbacks": [],
            }

    monkeypatch.setattr(routes, "lookup_job", lookup)
    assert client.post("/v1/demo/session", json={"team": "orders"}).status_code == 200

    response = client.get(f"/v1/jobs/{job_id}/result")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "완료된 결과가 없습니다. 작업 상태와 오류를 확인하세요."
    }


def test_disabled_demo_cannot_mint_session(monkeypatch):
    monkeypatch.delenv("FIVEWHYS_DEMO_ACCOUNTS", raising=False)
    client = TestClient(create_app())
    assert client.post("/v1/demo/session", json={"team":"orders"}).status_code == 404
    client.cookies.set("fw_session","orders")
    assert client.post("/v1/analyses", json={"question":"query"}).status_code == 401


def test_indexing_rejects_team_and_callback_url_override(client):
    client.post("/v1/demo/session", json={"team":"orders"})
    data = {"source":"ops.md","title":"절차","text":"1. 확인","service":"order-service","kind":"runbook","team":"platform"}
    assert client.post("/v1/indexes", json={"documents":[data]}).status_code == 422
    assert client.post("/v1/analyses", json={"question":"query","callback_url":"http://example.com"}).status_code == 422


def test_changed_demo_identity_requires_reselection(client):
    client.post("/v1/demo/session", json={"team": "platform"})
    resp = client.post("/v1/analyses", headers={"X-Expected-Team": "orders"},
                       json={"question": "주문 서비스 배포 확인"})
    assert resp.status_code == 409
    assert resp.headers["X-Session-Changed"] == "1"


def test_openapi_documents_cookie_bearer_accepted_link_and_callback(client):
    spec = client.get("/openapi.json").json()
    route = spec["paths"]["/v1/analyses"]["post"]
    assert {"DemoCookie": []} in route["security"]
    assert {"HTTPBearer": []} in route["security"]
    assert "Location" in route["responses"]["202"]["headers"]
    assert "job.completed" in spec["webhooks"]


def test_rejects_unstorable_question_before_dispatch(client):
    client.post("/v1/demo/session", json={"team": "orders"})
    response = client.post("/v1/analyses", json={"question": "log\0entry"})
    assert response.status_code == 422
