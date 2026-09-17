"""Loop mechanics, exercised with a scripted client instead of the live API.

These prove the control flow -- tool results fed back, the call budget enforced,
a failing query returned as an error the model can act on. They cannot prove what
the model generates for a real question; that needs credentials and a live call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jobcosting.agent import Agent, MAX_ROWS_TO_MODEL, MAX_TOOL_CALLS
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
    # out.sql is what RAN, so it carries the injected LIMIT; the model's own text
    # is kept alongside it.
    assert out.tool_calls[0].sql == sql
    assert out.sql == f"{sql}\nLIMIT 500"
    assert out.columns == ["CostType", "sum(CostAmount)"]
    assert len(out.rows) == 4
    assert out.answer == "Material is the largest category."

    # The second request must carry the assistant turn and the tool result.
    second = agent.client.requests[1]["messages"]
    assert second[1]["role"] == "assistant"
    result = second[2]["content"][0]
    assert result["type"] == "tool_result" and result["is_error"] is False
    assert json.loads(result["content"])["row_count"] == 4


def test_a_guardrail_rejection_is_returned_as_an_error_the_model_can_retry(engine):
    agent = make(engine, [
        ([tool_block("SELECT * FROM table_that_does_not_exist")], "tool_use"),
        ([tool_block("SELECT count(*) FROM dim_job", "tu_2")], "tool_use"),
        ([text_block("52 jobs.")], "end_turn"),
    ])
    out = agent.ask("how many jobs?")

    rejected = out.tool_calls[0]
    assert rejected.ok is False and rejected.rejected is True
    assert rejected.executed_sql is None            # never reached the engine
    assert "table_that_does_not_exist" in rejected.error
    assert "fact_job_cost" in rejected.error        # the reason lists what it MAY use

    result = agent.client.requests[1]["messages"][2]["content"][0]
    assert result["is_error"] is True
    assert "rejected and did not run" in result["content"]

    # sql/rows skip the failed call and report the one that worked.
    assert out.sql == "SELECT count(*) FROM dim_job\nLIMIT 500"
    assert out.rows == [(52,)]


def test_a_real_engine_error_is_distinguished_from_a_guardrail_rejection(engine):
    """An allowed table with a bad column: the guardrails pass it, DuckDB rejects it."""
    agent = make(engine, [
        ([tool_block("SELECT NoSuchColumn FROM dim_job")], "tool_use"),
        ([text_block("recovered")], "end_turn"),
    ])
    out = agent.ask("something")
    call = out.tool_calls[0]
    assert call.ok is False
    assert call.rejected is False                   # not a guardrail
    assert call.executed_sql is not None            # it did reach the engine
    assert "NoSuchColumn" in call.error
    assert "Fix the SQL" in agent.client.requests[1]["messages"][2]["content"][0]["content"]


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


def test_a_sixty_six_row_result_is_not_truncated_at_all(engine):
    """Regression: budget vs actual by job returns 66 rows.

    At the old 50-row limit this came back truncated and the model spent its second
    call paging with OFFSET 50. It must now arrive whole.
    """
    sql = """
        WITH b AS (SELECT JobKey, sum(BudgetAmount) AS budget FROM fact_job_budget GROUP BY 1),
             a AS (SELECT JobKey, sum(CostAmount)  AS actual FROM fact_job_cost   GROUP BY 1)
        SELECT coalesce(b.JobKey, a.JobKey),
               CAST(coalesce(b.budget, 0) AS DECIMAL(18,2)),
               CAST(coalesce(a.actual, 0) AS DECIMAL(18,2))
        FROM b FULL OUTER JOIN a ON a.JobKey = b.JobKey"""
    agent = make(engine, [
        ([tool_block(sql)], "tool_use"),
        ([text_block("done")], "end_turn"),
    ])
    out = agent.ask("budget vs actual by job")

    assert len(out.rows) == 66
    assert out.tool_calls[0].truncated is False
    sent = json.loads(agent.client.requests[1]["messages"][2]["content"][0]["content"])
    assert len(sent["rows"]) == 66
    assert "note" not in sent          # nothing to warn about


def test_truncation_tells_the_model_the_total_and_forbids_paging(engine):
    agent = make(engine, [
        ([tool_block("SELECT CostID FROM fact_job_cost")], "tool_use"),
        ([text_block("done")], "end_turn"),
    ])
    out = agent.ask("every cost id")
    call = out.tool_calls[0]
    # LIMIT 500 was injected, so 10,365 rows never leave the engine.
    assert call.executed_sql.endswith("LIMIT 500")
    assert len(call.rows) == 500
    assert call.truncated is True                      # 500 > the 250 shown

    sent = json.loads(agent.client.requests[1]["messages"][2]["content"][0]["content"])
    assert len(sent["rows"]) == MAX_ROWS_TO_MODEL      # model gets the cap
    assert sent["row_count"] == 500                    # ... and the true total
    note = sent["note"]
    assert "500 rows" in note
    assert "Do NOT page" in note and "OFFSET" in note  # paging is the wrong recovery
    assert "aggregate" in note                         # ... and the right one is named


def test_money_is_rounded_when_the_documented_cast_is_used(engine):
    """The float artefact the SQL panel would otherwise show."""
    raw = engine.run("SELECT sum(CostAmount) FROM fact_job_cost").rows[0][0]
    cast = engine.run(
        "SELECT CAST(sum(CostAmount) AS DECIMAL(18,2)) FROM fact_job_cost"
    ).rows[0][0]
    assert str(raw) == "7684852.81000002"
    assert str(cast) == "7684852.81"
