"""Guardrails, written before the implementation.

Five rules from the build spec:
  1. SELECT and WITH only, enforced by PARSING, not by regex on the string.
  2. Table allowlist derived from model.yaml.
  3. LIMIT 500 injected when absent.
  4. 10 second query timeout.
  5. Every rejection returns a reason the model can act on.
"""

from __future__ import annotations

import time

import pytest

from jobcosting.engine import DuckDBEngine
from jobcosting.guardrails import (
    DEFAULT_ROW_LIMIT,
    QueryRejected,
    check,
    load_allowed_tables,
)

MODEL_TABLES = [
    "dim_change_order", "dim_cost_type", "dim_date", "dim_employee",
    "dim_job", "fact_job_budget", "fact_job_cost",
]


@pytest.fixture(scope="module")
def allowed():
    return load_allowed_tables()


@pytest.fixture(scope="module")
def engine():
    return DuckDBEngine()


def reject(sql, allowed) -> str:
    """Assert the query is rejected and hand back the reason."""
    with pytest.raises(QueryRejected) as excinfo:
        check(sql, allowed)
    return str(excinfo.value)


# ---------------------------------------------------------------- rule 2 (source)

def test_allowlist_is_derived_from_model_yaml_not_hardcoded(allowed):
    assert allowed == frozenset(MODEL_TABLES)


# ------------------------------------------------- rule 1: SELECT and WITH only

@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM dim_job",
    "select COUNT(*) from DIM_JOB",                                  # case
    "  \n SELECT count(*) FROM dim_job  ",                           # whitespace
    "-- a leading comment\nSELECT count(*) FROM dim_job",
    "/* block comment */ SELECT count(*) FROM dim_job",
    "WITH j AS (SELECT * FROM dim_job) SELECT count(*) FROM j",
    "SELECT count(*) FROM dim_job UNION ALL SELECT count(*) FROM dim_employee",
    "SELECT (SELECT count(*) FROM dim_job) AS n",
    "SELECT count(*) FROM dim_job WHERE JobKey IN (SELECT JobKey FROM fact_job_cost)",
])
def test_select_and_with_are_allowed(sql, allowed):
    assert check(sql, allowed)


@pytest.mark.parametrize("sql", [
    "INSERT INTO dim_job VALUES (1)",
    "UPDATE dim_job SET Status = 'Complete'",
    "DELETE FROM dim_job",
    "DROP TABLE dim_job",
    "CREATE TABLE evil AS SELECT * FROM dim_job",
    "CREATE VIEW evil AS SELECT * FROM dim_job",
    "ALTER TABLE dim_job RENAME TO gone",
    "TRUNCATE dim_job",
    "ATTACH '/tmp/other.db' AS other",
    "COPY dim_job TO '/tmp/leak.csv'",
    "PRAGMA database_list",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET memory_limit = '1GB'",
    "CALL pragma_version()",
])
def test_everything_that_is_not_a_query_is_rejected(sql, allowed):
    assert "only SELECT" in reject(sql, allowed)


def test_a_second_statement_is_rejected(allowed):
    reason = reject("SELECT count(*) FROM dim_job; DROP TABLE dim_job", allowed)
    assert "one statement" in reason


def test_unparseable_sql_is_rejected_with_the_parser_message(allowed):
    reason = reject("SELECT FROM WHERE ((((", allowed)
    assert "could not be parsed" in reason


@pytest.mark.parametrize("sql", ["", "   ", "\n\t"])
def test_empty_sql_is_rejected(sql, allowed):
    assert "empty" in reject(sql, allowed)


# --- the cases that separate parsing from regex ------------------------------
# A blocklist regex gets all three of these wrong.

def test_a_forbidden_keyword_inside_a_string_literal_is_still_allowed(allowed):
    # Regex sees DROP TABLE and rejects a perfectly good query.
    assert check("SELECT 'DROP TABLE dim_job' AS label FROM dim_job", allowed)


def test_a_forbidden_keyword_inside_a_comment_is_still_allowed(allowed):
    assert check("SELECT count(*) FROM dim_job -- DELETE FROM dim_job", allowed)


def test_a_comment_mentioning_select_does_not_smuggle_a_drop(allowed):
    # Regex anchored on a leading SELECT lets this through.
    assert "only SELECT" in reject("/* SELECT */ DROP TABLE dim_job", allowed)


def test_a_column_named_like_a_keyword_is_allowed(allowed):
    assert check('SELECT "Status" AS update FROM dim_job', allowed)


# ------------------------------------------------------- rule 2: table allowlist

@pytest.mark.parametrize("table", MODEL_TABLES)
def test_every_model_table_is_reachable(table, allowed):
    assert check(f"SELECT * FROM {table}", allowed)


@pytest.mark.parametrize("sql,offender", [
    ("SELECT * FROM pg_tables", "pg_tables"),
    ("SELECT * FROM information_schema.tables", "tables"),
    ("SELECT * FROM sqlite_master", "sqlite_master"),
    ("SELECT * FROM duckdb_settings()", "duckdb_settings"),
    ("SELECT * FROM dim_job JOIN secret_payroll USING (JobKey)", "secret_payroll"),
])
def test_tables_outside_the_model_are_rejected(sql, offender, allowed):
    reason = reject(sql, allowed)
    assert offender in reason
    # The reason must tell the model what it MAY use, not just what it may not.
    assert "fact_job_cost" in reason


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_parquet('/etc/passwd')",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_json('/etc/passwd')",
    "SELECT * FROM glob('/**')",
    "SELECT * FROM '/etc/passwd'",
])
def test_file_reading_table_functions_are_rejected(sql, allowed):
    """These parse as an ordinary SELECT, so statement type alone does not stop them."""
    assert reject(sql, allowed)


def test_schema_qualified_names_are_rejected_and_the_reason_says_use_bare_names(allowed):
    reason = reject("SELECT * FROM lh_job_costing.silver.fact_job_cost", allowed)
    assert "bare table name" in reason


def test_cte_names_are_not_treated_as_tables(allowed):
    sql = """
        WITH b AS (SELECT JobKey, sum(BudgetAmount) AS budget FROM fact_job_budget GROUP BY 1),
             a AS (SELECT JobKey, sum(CostAmount)  AS actual FROM fact_job_cost   GROUP BY 1)
        SELECT * FROM b FULL OUTER JOIN a ON a.JobKey = b.JobKey"""
    assert check(sql, allowed)


def test_aliases_are_not_treated_as_tables(allowed):
    assert check("SELECT j.JobKey FROM dim_job AS j JOIN fact_job_cost c ON c.JobKey = j.JobKey", allowed)


def test_table_names_are_matched_case_insensitively(allowed):
    assert check("SELECT * FROM DIM_JOB", allowed)


# ----------------------------------------------------------- rule 3: LIMIT 500

def test_limit_is_injected_when_absent(allowed):
    assert f"LIMIT {DEFAULT_ROW_LIMIT}" in check("SELECT * FROM dim_job", allowed).upper()


def test_an_existing_smaller_limit_is_left_alone(allowed):
    out = check("SELECT * FROM dim_job LIMIT 10", allowed).upper()
    assert "LIMIT 10" in out and f"LIMIT {DEFAULT_ROW_LIMIT}" not in out


def test_a_limit_inside_a_cte_does_not_count_as_the_outer_limit(allowed):
    out = check("WITH j AS (SELECT * FROM dim_job LIMIT 5) SELECT * FROM j", allowed).upper()
    assert f"LIMIT {DEFAULT_ROW_LIMIT}" in out


def test_the_injected_query_still_runs_and_is_capped(allowed, engine):
    sql = check("SELECT CostID FROM fact_job_cost", allowed)
    assert engine.run(sql).row_count == DEFAULT_ROW_LIMIT


def test_injection_does_not_change_the_answer_of_a_small_query(allowed, engine):
    sql = check("SELECT count(*) FROM dim_job", allowed)
    assert engine.run(sql).rows == [(52,)]


# --------------------------------------------------------- rule 4: 10s timeout

def test_the_default_timeout_is_ten_seconds():
    assert DuckDBEngine().timeout_seconds == 10.0


def test_a_slow_query_is_interrupted_and_says_so():
    engine = DuckDBEngine(timeout_seconds=1.0)
    started = time.monotonic()
    with pytest.raises(TimeoutError) as excinfo:
        engine.run("SELECT count(*) FROM range(100000000000)")
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"took {elapsed:.1f}s -- the timeout did not fire"
    assert "1.0 second" in str(excinfo.value)


def test_the_connection_still_works_after_a_timeout():
    """An interrupt must not leave the connection unusable for the next question."""
    engine = DuckDBEngine(timeout_seconds=1.0)
    with pytest.raises(TimeoutError):
        engine.run("SELECT count(*) FROM range(100000000000)")
    assert engine.run("SELECT count(*) FROM dim_job").rows == [(52,)]


# ------------------------------------------- rule 5: reasons the model can act on

@pytest.mark.parametrize("sql", [
    "DROP TABLE dim_job",
    "SELECT * FROM pg_tables",
    "SELECT * FROM read_parquet('/etc/passwd')",
    "SELECT 1; SELECT 2",
    "SELECT FROM WHERE ((((",
    "",
])
def test_every_rejection_carries_an_actionable_reason(sql, allowed):
    reason = reject(sql, allowed)
    assert len(reason) > 40, "a reason the model can act on, not a bare code"
    assert reason[0].isupper() and reason.rstrip().endswith("."), "a full sentence"
