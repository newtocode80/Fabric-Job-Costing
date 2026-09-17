"""The numeric-groundedness check, including the failures that motivated it."""

from __future__ import annotations

from decimal import Decimal

from jobcosting.agent import ToolCall
from jobcosting.grounding import check_answer, declared_figures, extract_numbers


def call(columns, rows):
    return ToolCall(sql="x", ok=True, columns=list(columns), rows=[tuple(r) for r in rows])


def test_extracts_numbers_in_every_form_the_prose_uses():
    found = extract_numbers("$4,497,901.88 across 3,911 lines, up 23.1%, down -12")
    assert found == [Decimal("4497901.88"), Decimal("3911"), Decimal("23.1"), Decimal("-12")]


# ------------------------------------------ the failure this check exists for

def test_the_observed_failure_is_caught():
    """33 late jobs came back; "33 of 44" invents the 44."""
    late = call(["JobNumber"], [(f"J-2025{i:02d}",) for i in range(33)])
    result = check_answer("33 of 44 jobs finished late.", [late])
    assert not result.ok
    assert result.ungrounded == [Decimal("44")]
    assert "44" in result.summary()


def test_the_same_answer_without_the_invented_denominator_passes():
    late = call(["JobNumber"], [(f"J-2025{i:02d}",) for i in range(33)])
    assert check_answer("33 jobs finished late.", [late]).ok


def test_a_grand_total_not_in_the_result_is_caught():
    """"all 52 known jobs" -- 52 was never returned by this query."""
    result = call(["JobNumber"], [("J-202501",), ("J-202502",)])
    assert check_answer("2 of all 52 known jobs are affected.", [result]).ungrounded == [Decimal("52")]


def test_a_denominator_that_was_queried_for_passes():
    """The fix the rule asks for: query the denominator, then state it."""
    both = call(["late", "total"], [(33, 44)])
    assert check_answer("33 of 44 jobs finished late.", [both]).ok


# ------------------------------------------------- what must NOT be flagged

def test_cell_values_are_grounded():
    rows = call(["CostType", "total"], [("Material", Decimal("4497901.88"))])
    assert check_answer("Material cost $4,497,901.88.", [rows]).ok


def test_the_row_count_is_grounded():
    rows = call(["JobNumber"], [("a",), ("b",), ("c",)])
    assert check_answer("3 jobs matched.", [rows]).ok


def test_a_column_sum_is_grounded():
    rows = call(["cost"], [(Decimal("100.00"),), (Decimal("250.50"),)])
    assert check_answer("Together they come to $350.50.", [rows]).ok


def test_min_and_max_are_grounded():
    rows = call(["cost"], [(Decimal("10"),), (Decimal("99"),), (Decimal("50"),)])
    assert check_answer("They range from 10 to 99.", [rows]).ok


def test_a_rounded_restatement_at_scale_is_grounded():
    rows = call(["total"], [(Decimal("4497901.88"),)])
    assert check_answer("Roughly $4.5 million of material cost.", [rows]).ok


def test_a_percentage_of_two_aggregates_is_grounded():
    rows = call(["budget", "actual"], [(Decimal("8021432.92"), Decimal("7684852.81"))])
    # actual / budget = 95.8%
    assert check_answer("Actual came in at 95.8% of budget.", [rows]).ok


def test_numbers_from_the_question_are_grounded():
    rows = call(["n"], [(7,)])
    got = check_answer("In 2026 there were 7.", [rows], question="How many in 2026?")
    assert got.ok


def test_a_year_inside_a_date_cell_is_grounded():
    rows = call(["d"], [("2026-07-31",)])
    assert check_answer("The last activity was in July 2026.", [rows]).ok


def test_an_answer_with_no_numbers_is_fine():
    assert check_answer("There is no region attribute on the job.", []).ok
    assert check_answer("There is no region attribute on the job.", []).summary() == "no numbers stated"


def test_a_refusal_naming_no_figures_passes_with_no_results_at_all():
    assert check_answer("This data cannot answer that.", []).ok


def test_rows_from_a_failed_call_do_not_ground_anything():
    failed = ToolCall(sql="x", ok=False, error="boom")
    assert not check_answer("The total was 999.", [failed]).ok


def test_identifiers_are_not_read_as_numbers():
    """J-202551 is a job number, not minus two hundred thousand."""
    assert extract_numbers("The worst is J-202551.") == []
    assert extract_numbers("Job J-202505 cost $337,661.61.") == [Decimal("337661.61")]
    assert extract_numbers("version 2.1.3 and ref AB-99") == []


def test_a_standalone_negative_is_still_a_number():
    assert extract_numbers("variance of -12,500.40") == [Decimal("-12500.40")]


def test_an_answer_naming_job_numbers_is_grounded():
    late = call(["JobNumber"], [("J-202551",), ("J-202549",)])
    assert check_answer("2 jobs finished late: J-202551 and J-202549.", [late]).ok


# ------------------------------------------- declared known issues are supported

def test_the_four_figures_the_owner_named_are_grounded():
    """These reach the agent in the system prompt so it can caveat its answers."""
    rows = call(["JobNumber", "cost"], [("J-202501", Decimal("1000.00"))])
    for caveat in [
        "16,882.98 of cost cannot be attributed to any job.",
        "Two rows are duplicated, double-counting $347.30.",
        "30 cost rows reference jobs that do not exist.",
        "The orphan keys are 53 to 71.",
    ]:
        assert check_answer(caveat, [rows]).ok, caveat


def test_a_full_data_quality_caveat_passes():
    rows = call(["CostType", "total"], [("Material", Decimal("4497901.88"))])
    answer = (
        "Material cost is $4,497,901.88. Note that 30 cost rows, worth $16,882.98, "
        "reference 14 JobKey values that do not exist in the job dimension, so "
        "job-level totals will not reconcile to this figure."
    )
    assert check_answer(answer, [rows]).ok


def test_table_row_counts_are_not_declared_figures():
    """The boundary: a known issue is quoted as a caveat, a row count is computed with."""
    declared = declared_figures()
    assert Decimal("52") not in declared      # dim_job rows
    assert Decimal("10365") not in declared   # fact_job_cost rows


def test_the_original_invented_denominator_still_fails():
    """Widening the supported set must not have blunted the check it exists for."""
    late = call(["JobNumber"], [(f"J-2025{i:02d}",) for i in range(33)])
    assert check_answer("33 of 44 jobs finished late.", [late]).ungrounded == [Decimal("44")]


def test_all_52_known_jobs_still_fails():
    result = call(["JobNumber"], [("J-202501",), ("J-202502",)])
    assert Decimal("52") in check_answer("2 of all 52 known jobs are affected.", [result]).ungrounded


# --------------------------------------- list markers and ordinals are not figures

def test_ordered_list_markers_are_not_quantities():
    """An answer that numbers its points was reporting 8, 9, 10... as figures."""
    answer = (
        "The jobs over budget are:\n"
        "8. J-202551 - $232,248 over\n"
        "9. J-202549 - $167,391 over\n"
        "10. J-202505 - $148,829 over\n"
    )
    assert extract_numbers(answer) == [
        Decimal("232248"), Decimal("167391"), Decimal("148829"),
    ]


def test_list_markers_with_a_bracket_are_also_ignored():
    assert extract_numbers("1) first\n2) second\n3) third") == []


def test_indented_list_markers_are_ignored():
    assert extract_numbers("  1. alpha\n  2. beta") == []


def test_a_number_starting_a_sentence_is_still_a_quantity():
    """The marker rule must not swallow real figures at the start of a line."""
    assert extract_numbers("30 cost rows reference jobs that do not exist.") == [Decimal("30")]
    assert extract_numbers("2 jobs finished late.") == [Decimal("2")]


def test_ordinals_are_not_quantities():
    assert extract_numbers("the 1st and 2nd quarters, and the 23rd job") == []
    assert extract_numbers("4th quarter cost was $1,200.00") == [Decimal("1200.00")]


def test_a_bullet_list_keeps_its_figures():
    """Bullets are not numbered markers; the numbers in them are real."""
    answer = "- Material: $4,497,901.88\n- Labour: $1,715,039.31"
    assert extract_numbers(answer) == [Decimal("4497901.88"), Decimal("1715039.31")]


def test_a_numbered_list_of_real_figures_still_reports_the_figures():
    rows = call(["JobNumber", "over"], [("J-202551", Decimal("232248.41"))])
    answer = "Jobs over budget:\n1. J-202551 is $232,248.41 over budget."
    assert check_answer(answer, [rows]).ok
