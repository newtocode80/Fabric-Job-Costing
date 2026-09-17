"""The agent loop: one tool, `run_sql`, and a bounded conversation around it.

The system prompt is model/schema_context.md verbatim, wrapped in the behavioural
rules the build spec fixes: answer in business terms, never state a number that did
not come back from a query, and say what is missing when the schema cannot answer
the question.

Every query goes through jobcosting.guardrails before it reaches the engine. A
rejection comes back to the model as an error tool result carrying the reason, so
it can correct itself and try again within its remaining budget.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from .engine import QueryEngine, QueryResult
from .guardrails import DEFAULT_ROW_LIMIT, QueryRejected, check, load_allowed_tables

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_CONTEXT_PATH = ROOT / "model" / "schema_context.md"

# Set by the build spec.
MODEL = "claude-sonnet-4-6"
MAX_TOOL_CALLS = 3

# Rows are rendered into the tool result the model reads back. Set above the
# largest result the documented patterns produce (231 rows for budget vs actual at
# job x cost type), so an ordinary analytical answer is never cut. A result bigger
# than this is one the model should be aggregating or filtering, not reading.
MAX_ROWS_TO_MODEL = 250

SYSTEM_RULES = """\
You answer questions about job costing data by writing DuckDB SQL, running it with \
the run_sql tool, and explaining what came back in business terms.

Rules, in order of precedence:

1. Never state a number you did not get back from run_sql. Not an estimate, not a \
number you recall from earlier in the conversation, not arithmetic you did in your \
head on top of a result. If you want a total, query for it.
2. State only counts, totals and percentages that are present in the returned rows \
or follow directly from them. A denominator, a comparison group, a grand total or a \
share is not something to estimate, recall or infer -- query for it. If you have a \
filtered result and want to say "X of Y", then Y must itself have come back from a \
query; a result containing only the X rows cannot tell you Y. If your remaining \
calls will not let you establish it, give the number you do have and say plainly \
what you could not establish.

3. If the question cannot be answered from the schema below, say so plainly, say \
exactly what is missing, and say what would be needed to answer it. Do not answer a \
narrower question instead and present it as the answer.
4. Where the schema below gives a required query pattern for the kind of question \
asked, use it. Those patterns exist because the obvious SQL returns a wrong number.
5. If a question is ambiguous enough that two readings would give materially \
different answers, ask which is meant instead of guessing.
6. Where a known data quality issue affects the answer you are giving, say so in the \
answer.

run_sql is guarded. Queries that break these rules are rejected before they run \
and cost you a call, so write within them:

- Only a single SELECT, or a single WITH ... SELECT. No DDL, no DML, no PRAGMA, \
no settings, no reading files.
- Only the tables listed in the schema below. No system catalogues, no table \
functions such as read_parquet, no file paths.
- LIMIT {row_limit} is added automatically when your query has none, so detail \
queries come back capped. Aggregate in SQL when you need a figure over everything.
- A query is stopped after 10 seconds.

A rejection tells you what was wrong; fix it and try again.

You have at most {max_calls} run_sql calls per question. Answer in prose a project \
manager would understand -- no markdown tables, no SQL in the answer itself. The SQL \
is shown to the user separately.

The data model follows.

{schema_context}"""

RUN_SQL_TOOL: dict[str, Any] = {
    "name": "run_sql",
    "description": (
        "Run a read-only DuckDB SQL query against the job costing model and return "
        "the rows. Use the exact table and column names from the schema."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "The DuckDB SQL to execute."}
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
}


@dataclass
class ToolCall:
    """One run_sql call and what it returned."""

    sql: str                 # what the model wrote
    ok: bool
    executed_sql: str | None = None   # what actually ran, after LIMIT injection
    rejected: bool = False            # refused by a guardrail, never reached the engine
    clamped_from: int | None = None   # the model's LIMIT, if it was capped
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    error: str | None = None
    truncated: bool = False


@dataclass
class Answer:
    question: str
    answer: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    error: str | None = None
    stop_reason: str | None = None

    @property
    def sql(self) -> str | None:
        """The SQL behind the answer -- the last call that succeeded."""
        for call in reversed(self.tool_calls):
            if call.ok:
                # What ran, not what was written -- the panel must show the query
                # that produced these rows, LIMIT and all.
                return call.executed_sql or call.sql
        return self.tool_calls[-1].sql if self.tool_calls else None

    @property
    def rows(self) -> list[tuple[Any, ...]]:
        for call in reversed(self.tool_calls):
            if call.ok:
                return call.rows
        return []

    @property
    def columns(self) -> list[str]:
        for call in reversed(self.tool_calls):
            if call.ok:
                return call.columns
        return []


def build_system_prompt(schema_context_path: Path = SCHEMA_CONTEXT_PATH) -> str:
    if not schema_context_path.exists():
        raise FileNotFoundError(
            f"{schema_context_path} is missing. "
            "Run: python scripts/build_schema_context.py"
        )
    return SYSTEM_RULES.format(
        max_calls=MAX_TOOL_CALLS,
        row_limit=DEFAULT_ROW_LIMIT,
        schema_context=schema_context_path.read_text(),
    )


def _render(result: QueryResult) -> tuple[str, bool]:
    """Render rows for the model. Returns (text, truncated)."""
    if not result.rows:
        return "Query succeeded and returned 0 rows.", False
    shown = result.rows[:MAX_ROWS_TO_MODEL]
    truncated = len(result.rows) > len(shown)
    payload = {
        "columns": result.columns,
        "row_count": result.row_count,
        "rows": [list(r) for r in shown],
    }
    if truncated:
        # Paging is the wrong recovery: a second call spent on OFFSET burns a third
        # of the budget and still does not produce a whole-result answer.
        payload["note"] = (
            f"This result has {result.row_count} rows and you are seeing the first "
            f"{len(shown)}. Do NOT page through the rest with OFFSET or a second "
            f"LIMIT -- that spends another of your {MAX_TOOL_CALLS} calls and still "
            f"will not let you state a figure over all {result.row_count} rows. "
            "If you need a total, an average or a count, compute it in SQL with an "
            "aggregate. If you need particular rows, narrow the WHERE clause or use "
            "ORDER BY with a LIMIT so the rows you want come back first. If the "
            f"{len(shown)} rows you already have answer the question, just answer it."
        )
    return json.dumps(payload, default=str), truncated


class Agent:
    """Bounded tool-use loop over a single run_sql tool.

    A manual loop rather than the SDK's beta tool_runner, for three reasons: the
    call budget has to be enforced and surfaced to the model when it runs out;
    every call's SQL, columns and rows have to be captured for the /ask response
    at M4; and the runner is beta.
    """

    def __init__(
        self,
        engine: QueryEngine,
        client: Any | None = None,
        model: str = MODEL,
        max_tool_calls: int = MAX_TOOL_CALLS,
        schema_context_path: Path = SCHEMA_CONTEXT_PATH,
    ) -> None:
        self.engine = engine
        self.client = client if client is not None else anthropic.Anthropic()
        self.model = model
        self.max_tool_calls = max_tool_calls
        self.allowed_tables = load_allowed_tables()
        self.system_prompt = build_system_prompt(schema_context_path)

    def _execute(self, sql: str) -> tuple[ToolCall, str, bool]:
        """Guard, then run one query. Returns (record, text for the model, is_error)."""
        try:
            checked = check(sql, self.allowed_tables)
        except QueryRejected as exc:
            # A guardrail refused. The reason is written for the model to act on.
            return (
                ToolCall(sql=sql, ok=False, rejected=True, error=str(exc)),
                f"The query was rejected and did not run.\n{exc}",
                True,
            )

        try:
            result = self.engine.run(checked.sql)
        except TimeoutError as exc:
            return (
                ToolCall(sql=sql, ok=False, executed_sql=checked.sql, error=str(exc)),
                str(exc),
                True,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            return (
                ToolCall(sql=sql, ok=False, executed_sql=checked.sql, error=message),
                f"The query failed.\n{message}\nFix the SQL and try again.",
                True,
            )
        text, truncated = _render(result)
        # A clamp overrode something the model asked for, so it is told, ahead of
        # the rows -- the note must not be something it has to scroll past.
        if checked.note:
            text = f"{checked.note}\n\n{text}"
        return (
            ToolCall(
                sql=sql,
                ok=True,
                executed_sql=checked.sql,
                clamped_from=checked.limit_clamped_from,
                columns=result.columns,
                rows=result.rows,
                truncated=truncated,
            ),
            text,
            False,
        )

    def ask(self, question: str) -> Answer:
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        out = Answer(question=question, answer="")
        calls_made = 0

        while True:
            # Once the budget is gone the tool is withdrawn, so the model answers
            # from what it has instead of emitting a call that cannot run.
            budget_left = calls_made < self.max_tool_calls
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    thinking={"type": "adaptive"},
                    system=self.system_prompt,
                    tools=[RUN_SQL_TOOL] if budget_left else [],
                    messages=messages,
                )
            except Exception as exc:
                # Deliberately broad. Reaching the model can fail for reasons that
                # are not APIError -- unresolved credentials raise TypeError, for
                # one -- and a question that cannot be sent must be reported as a
                # failed answer, not crash a run of several questions.
                out.error = f"{type(exc).__name__}: {exc}"
                return out

            out.stop_reason = response.stop_reason

            if response.stop_reason == "refusal":
                out.error = "The model declined to answer this request."
                out.answer = out.error
                return out

            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if not tool_uses:
                out.answer = text
                return out

            messages.append({"role": "assistant", "content": response.content})

            results = []
            for block in tool_uses:
                sql = (block.input or {}).get("sql", "")
                record, rendered, is_error = self._execute(sql)
                out.tool_calls.append(record)
                calls_made += 1
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": rendered,
                        "is_error": is_error,
                    }
                )

            if calls_made >= self.max_tool_calls:
                results.append(
                    {
                        "type": "text",
                        "text": (
                            f"You have used all {self.max_tool_calls} run_sql calls. "
                            "Answer from the results above, or say what you could not "
                            "establish. Do not state any number you did not retrieve."
                        ),
                    }
                )
            messages.append({"role": "user", "content": results})
