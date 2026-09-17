"""Guardrails on the SQL the model is allowed to run.

Five rules, from the build spec:

  1. SELECT and WITH only, decided by PARSING the statement, never by matching
     keywords in the string. A blocklist regex both over- and under-rejects: it
     refuses `SELECT 'DROP TABLE x'` and admits `/* SELECT */ DROP TABLE x`.
  2. Only the tables declared in model.yaml may be referenced.
  3. LIMIT 500 is added when the statement has none at the top level, and any
     explicit limit above 500 is clamped down to it. A clamp is never silent: it
     produces a note telling the model what it asked for, what it got, and to
     aggregate rather than try again for more rows.
  4. Queries are killed after 10 seconds (enforced by the engine, not here).
  5. Every rejection raises QueryRejected carrying a sentence the model can act
     on -- what was wrong and what to do instead.

Parsing is sqlglot in its DuckDB dialect. The parse is used for the decision only;
the SQL that runs is the model's own text, so the SQL panel shows what the model
wrote rather than a reformatted version of it.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class CheckedQuery:
    """A query that passed the guardrails, and what they changed on the way."""

    sql: str                              # what to execute
    original_sql: str                     # exactly what the model wrote
    limit_injected: bool = False          # had no limit; LIMIT 500 added
    limit_clamped_from: int | None = None # had a bigger limit; capped at 500
    row_limit: int = DEFAULT_ROW_LIMIT

    @property
    def rewritten(self) -> bool:
        return self.sql != self.original_sql

    @property
    def note(self) -> str | None:
        """What to tell the model. Only a clamp needs saying.

        Injecting a limit overrides nothing the model chose, so it is silent.
        Clamping overrides an explicit instruction, so it never is -- and the
        guidance matches the truncation note, because the wrong recovery is the
        same one: asking for more rows instead of aggregating.
        """
        if self.limit_clamped_from is None:
            return None
        return (
            f"Your LIMIT of {self.limit_clamped_from} was reduced to "
            f"{self.row_limit}, the most this tool will return. You are seeing "
            f"{self.row_limit} rows, not {self.limit_clamped_from}. Do NOT raise "
            f"the LIMIT again or page with OFFSET -- the cap applies to every "
            f"query, so a call spent that way is wasted. If you need a figure "
            f"over all the matching rows, compute it in SQL with an aggregate. "
            f"If you need particular rows, narrow the WHERE clause or use ORDER "
            f"BY so the rows you want come back first."
        )


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


def _top_level_limit(statement: exp.Expression) -> int | None:
    """The statement's own LIMIT, if it has one and it is a plain number.

    A LIMIT inside a CTE or subquery does not bound the result the caller gets, so
    only the top-level node is consulted.
    """
    limit = statement.args.get("limit")
    if limit is None:
        return None
    try:
        return int(limit.expression.name)
    except (AttributeError, ValueError):
        # A non-literal limit (an expression or a parameter). Treat it as present
        # but unknown, and let the wrapper below cap it anyway.
        return -1


def check(
    sql: str,
    allowed: frozenset[str] | None = None,
    row_limit: int = DEFAULT_ROW_LIMIT,
) -> CheckedQuery:
    """Validate `sql` and return what to execute, plus what was changed.

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

    trimmed = sql.rstrip().rstrip(";")
    existing = _top_level_limit(statement)

    if existing is None:
        # Append rather than regenerate from the AST, so the SQL panel shows the
        # model's own formatting. Safe: the statement is already proven to be a
        # single query, so there is nothing after it for the LIMIT to land inside.
        return CheckedQuery(
            sql=f"{trimmed}\nLIMIT {row_limit}",
            original_sql=sql,
            limit_injected=True,
            row_limit=row_limit,
        )

    if 0 <= existing <= row_limit:
        return CheckedQuery(sql=sql, original_sql=sql, row_limit=row_limit)

    # Over the cap. Wrap rather than rewrite the LIMIT in place: the model's query
    # survives verbatim inside, so the panel shows both what was asked for and the
    # cap that was applied, instead of a silently edited number.
    return CheckedQuery(
        sql=f"SELECT * FROM (\n{trimmed}\n) LIMIT {row_limit}",
        original_sql=sql,
        limit_clamped_from=existing,
        row_limit=row_limit,
    )
