"""Guardrails on the SQL the model is allowed to run.

Five rules, from the build spec:

  1. SELECT and WITH only, decided by PARSING the statement, never by matching
     keywords in the string. A blocklist regex both over- and under-rejects: it
     refuses `SELECT 'DROP TABLE x'` and admits `/* SELECT */ DROP TABLE x`.
  2. Only the tables declared in model.yaml may be referenced.
  3. LIMIT 500 is added when the statement has none at the top level.
  4. Queries are killed after 10 seconds (enforced by the engine, not here).
  5. Every rejection raises QueryRejected carrying a sentence the model can act
     on -- what was wrong and what to do instead.

Parsing is sqlglot in its DuckDB dialect. The parse is used for the decision only;
the SQL that runs is the model's own text, so the SQL panel shows what the model
wrote rather than a reformatted version of it.
"""

from __future__ import annotations

from pathlib import Path

import sqlglot
from sqlglot import expressions as exp

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "model" / "model.yaml"

DEFAULT_ROW_LIMIT = 500
DIALECT = "duckdb"

# A top-level statement must be one of these. WITH ... SELECT parses as a Select
# carrying its CTEs; set operations are their own nodes.
QUERY_NODES = (exp.Select, exp.Union, exp.Intersect, exp.Except)


class QueryRejected(Exception):
    """A query a guardrail refused. The message is written for the model to read."""


def load_allowed_tables(model_path: Path = MODEL_PATH) -> frozenset[str]:
    """The allowlist, derived from model.yaml -- never a second hardcoded list."""
    import yaml

    model = yaml.safe_load(model_path.read_text())
    return frozenset(t["name"] for t in model["tables"])


def _describe(table: exp.Table) -> str:
    """Name what a FROM item actually is, for the rejection message.

    A table function such as read_parquet('/etc/passwd') parses as a Table node
    with an EMPTY name and the function hanging off it, so an allowlist that only
    compared names would wave it through.
    """
    inner = table.this
    if isinstance(inner, exp.Anonymous):
        return f"{inner.name}()"
    if isinstance(inner, exp.Func):
        return f"{inner.sql_name().lower()}()"
    return table.sql(dialect=DIALECT)


def _cte_names(statement: exp.Expression) -> set[str]:
    """CTE aliases. A reference to one is not a reference to a table."""
    return {cte.alias.lower() for cte in statement.find_all(exp.CTE) if cte.alias}


def _check_tables(statement: exp.Expression, allowed: frozenset[str]) -> None:
    cte_names = _cte_names(statement)
    allowed_lower = {t.lower() for t in allowed}
    listing = ", ".join(sorted(allowed))

    for table in statement.find_all(exp.Table):
        name = table.name

        if not name:
            raise QueryRejected(
                f"Reading from `{_describe(table)}` is not allowed. Table "
                f"functions, file paths and system catalogues cannot be queried. "
                f"Use only: {listing}."
            )

        known = name.lower() in allowed_lower or name.lower() in cte_names

        if (table.catalog or table.db) and known:
            # The base name is a real table, so the only problem is the qualifier.
            raise QueryRejected(
                f"Table `{table.sql(dialect=DIALECT)}` is schema-qualified. Use the "
                f"bare table name `{name}` -- this engine has no catalog or schema "
                f"to qualify with, and the qualified form does not resolve."
            )

        if not known:
            # Name the whole reference, qualified or not, so the model can see
            # exactly what it asked for.
            raise QueryRejected(
                f"Table `{table.sql(dialect=DIALECT)}` is not one of the job "
                f"costing tables. Use only: {listing}."
            )


def _has_top_level_limit(statement: exp.Expression) -> bool:
    """A LIMIT inside a CTE or subquery does not bound the result the caller gets."""
    return statement.args.get("limit") is not None


def check(
    sql: str,
    allowed: frozenset[str] | None = None,
    row_limit: int = DEFAULT_ROW_LIMIT,
) -> str:
    """Validate `sql` and return the text to execute.

    Raises QueryRejected, with a reason for the model, if any guardrail refuses.
    """
    if allowed is None:
        allowed = load_allowed_tables()

    if not sql or not sql.strip():
        raise QueryRejected(
            "The query was empty. Send a single SELECT, or a WITH ... SELECT, "
            "against the job costing tables."
        )

    try:
        statements = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
    except sqlglot.ParseError as exc:
        raise QueryRejected(
            f"The query could not be parsed as DuckDB SQL: {exc}. "
            f"Check the syntax and send it again."
        ) from exc

    if not statements:
        raise QueryRejected(
            "The query contained no SQL statement. Send a single SELECT, or a "
            "WITH ... SELECT, against the job costing tables."
        )

    if len(statements) > 1:
        kinds = ", ".join(type(s).__name__.upper() for s in statements)
        raise QueryRejected(
            f"The query contained {len(statements)} statements ({kinds}). "
            f"Send one statement at a time, and only a query."
        )

    statement = statements[0]
    if not isinstance(statement, QUERY_NODES):
        raise QueryRejected(
            f"This is a {type(statement).__name__.upper()} statement. This tool runs "
            f"only SELECT and WITH ... SELECT queries -- it cannot modify data, "
            f"create objects, read files or change settings. Rewrite it as a query."
        )

    _check_tables(statement, allowed)

    if _has_top_level_limit(statement):
        return sql

    # Append rather than regenerate from the AST, so the SQL panel shows the
    # model's own formatting. Safe: the statement has already been proven to be a
    # single query, so there is nothing after it for the LIMIT to land inside.
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {row_limit}"
