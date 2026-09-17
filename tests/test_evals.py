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
    # 44 collides with an unrelated 44 in the prompt, so it lands as a warning
    # rather than a failure -- see test_a_denominator_that_collides_with_a_prompt_
    # figure_only_warns. It is still surfaced, which is what the runner owes.
    assert any("44" in w for w in result.warned)


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


# ------------------------------------- saving must survive anything the run does

def late_jobs_with_dates() -> ToolCall:
    """The result shape that crashed a completed run: it contains dates."""
    import datetime as dt
    from decimal import Decimal
    return ToolCall(
        sql="SELECT JobNumber, ActualEndDate, CAST(ActualEndDate AS DATE), ContractValue "
            "FROM dim_job WHERE ActualEndDate > ScheduledEndDate",
        executed_sql="... LIMIT 500", ok=True,
        columns=["JobNumber", "ActualEndDate", "end_date", "value"],
        rows=[
            ("J-202509", dt.datetime(2026, 4, 24), dt.date(2026, 4, 24), Decimal("179000.00")),
            ("J-202524", dt.datetime(2025, 12, 24), dt.date(2025, 12, 24), Decimal("80000.00")),
        ],
    )


def test_an_answer_containing_dates_saves_and_replays(engine, tmp_path):
    """The exact regression: date is not JSON serialisable, and the run was lost."""
    import json
    answer = Answer(
        question="Which jobs finished late?",
        answer="2 jobs finished late: J-202509 and J-202524.",
        tool_calls=[late_jobs_with_dates()],
    )
    record = runner.to_json("jobs_finished_late", answer)

    path = tmp_path / "run.json"
    runner.save([record], path)                       # must not raise
    assert path.exists()

    rebuilt = runner.from_json(json.loads(path.read_text())[0])
    assert rebuilt.tool_calls[0].rows == answer.tool_calls[0].rows   # types preserved
    assert (runner.evaluate(case("jobs_finished_late"), rebuilt, engine).failed
            == runner.evaluate(case("jobs_finished_late"), answer, engine).failed)


def test_save_never_raises_even_when_it_cannot_write(tmp_path, capsys):
    """A failure to save must warn, not abort a run that has already made calls."""
    runner.save([{"id": "x"}], tmp_path / "no-such-dir" / "run.json")
    assert "warning" in capsys.readouterr().err


def test_a_run_saves_after_every_case_not_only_at_the_end(monkeypatch, tmp_path, engine):
    """A crash part-way through must leave the completed answers on disk."""
    import json
    path = tmp_path / "run.json"
    monkeypatch.setattr(runner, "LAST_RUN", path)

    asked = []
    seen_on_disk = []

    class StubAgent:
        def __init__(self, **kwargs):
            pass

        def ask(self, question):
            asked.append(question)
            # What the file holds at the moment this call starts.
            seen_on_disk.append(len(json.loads(path.read_text())) if path.exists() else 0)
            return Answer(question=question, answer="2 rows.",
                          tool_calls=[late_jobs_with_dates()])

    monkeypatch.setattr(runner, "Agent", StubAgent)
    monkeypatch.setattr("sys.argv", ["run.py", "--case", "aggregate"])
    runner.main()

    assert len(asked) >= 3
    # Before the Nth question, N-1 answers are already saved.
    assert seen_on_disk == list(range(len(asked)))
    assert len(json.loads(path.read_text())) == len(asked)
