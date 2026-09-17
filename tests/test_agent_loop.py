"""Loop mechanics, exercised with a scripted client instead of the live API.

These prove the control flow -- tool results fed back, the call budget enforced,
a failing query returned as an error the model can act on. They cannot prove what
the model generates for a real question; that needs credentials and a live call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jobcosting.agent import Agent, MAX_TOOL_CALLS
from jobcosting.engine import DuckDBEngine


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(sql, block_id="tu_1"):
    return SimpleNamespace(type="tool_use", id=block_id, name="run_sql", input={"sql": sql})


class ScriptedClient:
    """Returns pre-set responses in order and records every request."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        content, stop = self.script.pop(0)
        return SimpleNamespace(content=content, stop_reason=stop)


@pytest.fixture(scope="module")
def engine():
    return DuckDBEngine()


def make(engine, script):
    return Agent(engine=engine, client=ScriptedClient(script))


def test_answer_without_any_tool_call(engine):
    agent = make(engine, [([text_block("There is no region on the job.")], "end_turn")])
    out = agent.ask("cost by region?")
    assert out.answer == "There is no region on the job."
    assert out.tool_calls == []
    assert out.sql is None


def test_tool_result_is_fed_back_and_answer_returned(engine):
    sql = "SELECT CostType, sum(CostAmount) FROM fact_job_cost GROUP BY 1"
    agent = make(engine, [
        ([tool_block(sql)], "tool_use"),
        ([text_block("Material is the largest category.")], "end_turn"),
    ])
    out = agent.ask("total cost by cost type?")

    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].ok
    assert out.sql == sql
    assert out.columns == ["CostType", "sum(CostAmount)"]
    assert len(out.rows) == 4
    assert out.answer == "Material is the largest category."

    # The second request must carry the assistant turn and the tool result.
    second = agent.client.requests[1]["messages"]
    assert second[1]["role"] == "assistant"
    result = second[2]["content"][0]
    assert result["type"] == "tool_result" and result["is_error"] is False
    assert json.loads(result["content"])["row_count"] == 4


def test_failed_query_returns_is_error_and_the_model_can_retry(engine):
    agent = make(engine, [
        ([tool_block("SELECT * FROM table_that_does_not_exist")], "tool_use"),
        ([tool_block("SELECT count(*) FROM dim_job", "tu_2")], "tool_use"),
        ([text_block("52 jobs.")], "end_turn"),
    ])
    out = agent.ask("how many jobs?")

    assert out.tool_calls[0].ok is False
    assert "table_that_does_not_exist" in out.tool_calls[0].error
    result = agent.client.requests[1]["messages"][2]["content"][0]
    assert result["is_error"] is True
    assert "Fix the SQL" in result["content"]

    # sql/rows skip the failed call and report the one that worked.
    assert out.sql == "SELECT count(*) FROM dim_job"
    assert out.rows == [(52,)]


def test_call_budget_is_enforced_and_the_tool_is_withdrawn(engine):
    script = [([tool_block(f"SELECT {i}", f"tu_{i}")], "tool_use") for i in range(MAX_TOOL_CALLS)]
    script.append(([text_block("Answering from what I have.")], "end_turn"))
    agent = make(engine, script)
    out = agent.ask("something needing many queries")

    assert len(out.tool_calls) == MAX_TOOL_CALLS
    # The first MAX_TOOL_CALLS requests offer the tool; the last withdraws it.
    offered = [bool(r["tools"]) for r in agent.client.requests]
    assert offered == [True] * MAX_TOOL_CALLS + [False]

    final = agent.client.requests[-1]["messages"][-1]["content"]
    assert any(b.get("type") == "text" and "used all" in b["text"] for b in final)


def test_refusal_stop_reason_is_surfaced_not_swallowed(engine):
    agent = make(engine, [([], "refusal")])
    out = agent.ask("something refused")
    assert out.stop_reason == "refusal"
    assert out.error and "declined" in out.error


def test_large_result_is_truncated_for_the_model_but_not_for_the_caller(engine):
    agent = make(engine, [
        ([tool_block("SELECT CostID FROM fact_job_cost")], "tool_use"),
        ([text_block("done")], "end_turn"),
    ])
    out = agent.ask("every cost id")
    call = out.tool_calls[0]
    assert call.truncated is True
    assert len(call.rows) == 10365                     # caller gets everything
    sent = json.loads(agent.client.requests[1]["messages"][2]["content"][0]["content"])
    assert len(sent["rows"]) == 50 and sent["row_count"] == 10365   # model gets 50 + the count
    assert "first 50 of 10365" in sent["note"]
