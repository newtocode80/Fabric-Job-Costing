"""The /ask and /health contract, driven against a stubbed agent."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app as app_module
from jobcosting.agent import Answer, ToolCall


@pytest.fixture
def client(monkeypatch):
    return TestClient(app_module.main)


def stub(monkeypatch, answer: Answer):
    monkeypatch.setattr(app_module._agent, "ask", lambda q: answer)


def test_health_reports_what_matters_before_a_demo(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert len(body["tables"]) == 7 and "fact_job_cost" in body["tables"]
    assert body["row_limit"] == 500
    assert body["max_tool_calls"] == 3
    assert body["query_timeout_seconds"] == 10.0
    assert "credentials_configured" in body


def test_ask_returns_every_field_the_spec_names(client, monkeypatch):
    stub(monkeypatch, Answer(
        question="q",
        answer="Material is the largest category.",
        tool_calls=[ToolCall(
            sql="SELECT CostType, sum(CostAmount) FROM fact_job_cost GROUP BY 1",
            executed_sql="SELECT CostType, sum(CostAmount) FROM fact_job_cost GROUP BY 1\nLIMIT 500",
            ok=True,
            columns=["CostType", "cost"],
            rows=[("Material", Decimal("4497901.88"))],
        )],
    ))
    body = client.post("/ask", json={"question": "cost by type"}).json()

    assert set(body) >= {"answer", "sql", "rows", "columns", "tool_calls", "error"}
    assert body["answer"] == "Material is the largest category."
    assert body["sql"].endswith("LIMIT 500")          # what ran, not what was written
    assert body["columns"] == ["CostType", "cost"]
    assert body["error"] is None
    assert body["tool_calls"][0]["row_count"] == 1


def test_decimals_survive_as_exact_strings_not_floats(client, monkeypatch):
    stub(monkeypatch, Answer(question="q", answer="a", tool_calls=[ToolCall(
        sql="x", ok=True, columns=["total"], rows=[(Decimal("7684852.81"),)])]))
    rows = client.post("/ask", json={"question": "total"}).json()["rows"]
    assert rows == [["7684852.81"]], "a float round-trip would reintroduce .81000002"


def test_dates_are_serialised_as_iso_strings(client, monkeypatch):
    stub(monkeypatch, Answer(question="q", answer="a", tool_calls=[ToolCall(
        sql="x", ok=True, columns=["d"], rows=[(dt.datetime(2026, 7, 31),)])]))
    assert client.post("/ask", json={"question": "when"}).json()["rows"] == [["2026-07-31T00:00:00"]]


def test_a_clamp_is_visible_in_the_response(client, monkeypatch):
    stub(monkeypatch, Answer(question="q", answer="a", tool_calls=[ToolCall(
        sql="SELECT CostID FROM fact_job_cost LIMIT 100000",
        executed_sql="SELECT * FROM (\nSELECT CostID FROM fact_job_cost LIMIT 100000\n) LIMIT 500",
        ok=True, clamped_from=100000, columns=["CostID"], rows=[(1,)])]))
    body = client.post("/ask", json={"question": "all ids"}).json()
    call = body["tool_calls"][0]
    assert call["clamped_from"] == 100000
    assert call["sql"] != call["executed_sql"]        # both are reported
    assert body["row_limit"] == 500


def test_a_rejection_is_reported_without_a_500(client, monkeypatch):
    stub(monkeypatch, Answer(question="q", answer="I could not run that.", tool_calls=[ToolCall(
        sql="DROP TABLE dim_job", ok=False, rejected=True,
        error="This is a DROP statement. This tool runs only SELECT ...")]))
    response = client.post("/ask", json={"question": "drop it"})
    assert response.status_code == 200
    call = response.json()["tool_calls"][0]
    assert call["rejected"] is True and call["ok"] is False


def test_an_agent_error_is_reported_as_error_not_as_a_crash(client, monkeypatch):
    stub(monkeypatch, Answer(question="q", answer="", error="TypeError: no credentials"))
    body = client.post("/ask", json={"question": "anything"}).json()
    assert body["error"] == "TypeError: no credentials"
    assert body["sql"] is None and body["rows"] == []


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "x" * 2001}])
def test_bad_requests_are_rejected_by_validation(client, payload):
    assert client.post("/ask", json=payload).status_code == 422


def test_the_page_is_served_and_references_its_assets(client):
    html = client.get("/").text
    assert "Job Costing Query Assistant" in html
    for asset in ("/static/style.css", "/static/app.js"):
        assert asset in html
        assert client.get(asset).status_code == 200
