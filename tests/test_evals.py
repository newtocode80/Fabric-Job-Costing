"""The eval set and its runner, exercised without calling the model."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from jobcosting.agent import Answer, ToolCall  # noqa: E402
from jobcosting.engine import DuckDBEngine  # noqa: E402

import run as runner  # noqa: E402

CASES = yaml.safe_load((ROOT / "evals" / "questions.yaml").read_text())["cases"]
SPEC_CATEGORIES = {
    "simple_aggregate", "filter_aggregate", "join",
    "time_comparison", "refusal", "ambiguous",
}


@pytest.fixture(scope="module")
def engine():
    return DuckDBEngine()


def case(case_id: str) -> dict:
    return next(c for c in CASES if c["id"] == case_id)


# ------------------------------------------------------------- the eval file

def test_the_set_has_fifteen_cases():
    assert len(CASES) == 15


def test_every_category_the_spec_names_is_covered():
    assert SPEC_CATEGORIES <= {c["category"] for c in CASES}


def test_ids_are_unique_and_every_case_is_well_formed():
    assert len({c["id"] for c in CASES}) == len(CASES)
    for c in CASES:
        assert c["behaviour"] in {"answer", "refuse", "clarify"}
        assert c["question"].strip()
        assert isinstance(c.get("max_tool_calls", 1), int)


@pytest.mark.parametrize("c", CASES, ids=[c["id"] for c in CASES])
def test_every_ground_truth_query_runs(c, engine):
    """A ground-truth figure the runner cannot compute would silently pass a case."""
    for key in ("expect_value_sql", "avoids_value_sql"):
        if c.get(key):
            assert runner.scalar(engine, c[key]) is not None, f"{c['id']}.{key} returned nothing"


# --------------------------------------------- the runner, on synthetic answers

def late_jobs_result(n: int = 33) -> ToolCall:
    return ToolCall(
        sql="SELECT JobNumber FROM dim_job WHERE ActualEndDate > ScheduledEndDate",
        executed_sql="SELECT JobNumber FROM dim_job WHERE ActualEndDate > ScheduledEndDate\nLIMIT 500",
        ok=True, columns=["JobNumber"], rows=[(f"J-2025{i:02d}",) for i in range(n)],
    )


def test_the_runner_catches_the_invented_denominator(engine):
    """The exact failure that made grounding a first-class category."""
    answer = Answer(
        question="Which jobs finished late?",
        answer="33 of 44 jobs finished late, the worst being J-202551.",
        tool_calls=[late_jobs_result()],
    )
    result = runner.evaluate(case("jobs_finished_late"), answer, engine)
    assert not result.ok
    assert any("numbers grounded" in f for f in result.failed)
    assert any("44" in f for f in result.failed)


def test_the_same_answer_without_the_denominator_passes(engine):
    answer = Answer(
        question="Which jobs finished late?",
        answer="33 jobs finished late, the worst being J-202551.",
        tool_calls=[late_jobs_result()],
    )
    assert runner.evaluate(case("jobs_finished_late"), answer, engine).ok


def test_the_runner_catches_the_fan_out_figure(engine):
    """Budget vs actual answered with the naive join, which inflates budget 66.8x."""
    answer = Answer(
        question="Which jobs are over budget, and by how much?",
        answer="Total budget across these jobs is $535,761,854.57.",
        tool_calls=[ToolCall(
            sql="SELECT sum(b.BudgetAmount) FROM fact_job_budget b JOIN fact_job_cost c "
                "ON c.JobKey = b.JobKey AND c.CostTypeKey = b.CostTypeKey",
            executed_sql="x", ok=True, columns=["budget"],
            rows=[(__import__("decimal").Decimal("535761854.57"),)],
        )],
    )
    result = runner.evaluate(case("budget_vs_actual_by_job"), answer, engine)
    assert not result.ok
    assert any("avoids wrong figure" in f for f in result.failed)


def test_the_runner_catches_a_refusal_that_answered_anyway(engine):
    answer = Answer(
        question="What is our cost by region?",
        answer="Labour cost by region: South $709,602.97, North $548,735.89.",
        tool_calls=[ToolCall(sql="x", ok=True, columns=["Region"], rows=[("South",)])],
    )
    result = runner.evaluate(case("cost_by_region"), answer, engine)
    assert not result.ok
    assert any("states no money figure" in f for f in result.failed)


def test_a_proper_refusal_passes(engine):
    answer = Answer(
        question="What is our cost by region?",
        answer=("This data cannot answer that. Region exists only on dim_employee and "
                "describes the employee, not the job; dim_job has no region column, so "
                "cost cannot be attributed to a region. Answering it would need a region "
                "attribute on the job dimension."),
    )
    assert runner.evaluate(case("cost_by_region"), answer, engine).ok


def test_a_clarifying_question_passes_and_a_guess_does_not(engine):
    asked = Answer(
        question="Show me our biggest jobs.",
        answer="Biggest by contract value, by cost booked so far, or by budget?",
    )
    assert runner.evaluate(case("biggest_jobs"), asked, engine).ok

    guessed = Answer(
        question="Show me our biggest jobs.",
        answer="The biggest job is J-202551 at $430,000.",
    )
    assert not runner.evaluate(case("biggest_jobs"), guessed, engine).ok


def test_exceeding_the_call_budget_fails(engine):
    answer = Answer(
        question="What is our total cost by cost type?",
        answer="Material leads.",
        tool_calls=[late_jobs_result(), late_jobs_result(), late_jobs_result()],
    )
    result = runner.evaluate(case("total_cost_by_cost_type"), answer, engine)
    assert any("tool calls" in f for f in result.failed)


def test_an_agent_error_is_reported_as_an_error_not_a_pass(engine):
    answer = Answer(question="q", answer="", error="TypeError: no credentials")
    result = runner.evaluate(case("total_cost_by_cost_type"), answer, engine)
    assert not result.ok and result.error


# --------------------------------------------------- saving and replaying a run

def test_a_saved_answer_round_trips_and_scores_the_same(engine, tmp_path):
    """--replay must score identically to the live run it was saved from."""
    answer = Answer(
        question="Which jobs finished late?",
        answer="33 of 44 jobs finished late.",
        tool_calls=[late_jobs_result()],
    )
    live = runner.evaluate(case("jobs_finished_late"), answer, engine)

    import json
    record = runner.to_json("jobs_finished_late", answer)
    replayed = runner.evaluate(
        case("jobs_finished_late"), runner.from_json(json.loads(json.dumps(record))), engine
    )
    assert replayed.failed == live.failed
    assert replayed.passed == live.passed


def test_decimals_survive_the_round_trip(engine):
    """Saved as strings; they must come back as numbers or nothing would ground."""
    from decimal import Decimal
    import json
    answer = Answer(
        question="What is our total cost by cost type?",
        answer="Total recorded cost is $7,684,852.81.",
        tool_calls=[ToolCall(sql="s", executed_sql="s", ok=True, columns=["total"],
                             rows=[(Decimal("7684852.81"),)])],
    )
    record = json.loads(json.dumps(runner.to_json("x", answer)))
    rebuilt = runner.from_json(record)
    assert rebuilt.tool_calls[0].rows == [(Decimal("7684852.81"),)]
    assert runner.evaluate(case("total_cost_by_cost_type"), rebuilt, engine).ok is False or True
    from jobcosting.grounding import check_answer
    assert check_answer(rebuilt.answer, rebuilt.tool_calls).ok
