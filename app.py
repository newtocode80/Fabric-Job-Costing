"""FastAPI wrapper around the job costing agent.

    uvicorn app:main --reload

Serves the single page at /, the API at /ask and /health.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from jobcosting.agent import MAX_TOOL_CALLS, MODEL, Agent
from jobcosting.engine import DuckDBEngine
from jobcosting.guardrails import DEFAULT_ROW_LIMIT

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

main = FastAPI(
    title="Job Costing Query Assistant",
    description="Ask a question in English; get an answer, the SQL behind it, and the rows.",
)

_engine = DuckDBEngine()
_agent = Agent(engine=_engine)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ToolCallOut(BaseModel):
    sql: str
    executed_sql: str | None = None
    ok: bool
    rejected: bool = False
    clamped_from: int | None = None
    truncated: bool = False
    row_count: int = 0
    error: str | None = None


class AskResponse(BaseModel):
    answer: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    tool_calls: list[ToolCallOut] = []
    error: str | None = None
    row_limit: int = DEFAULT_ROW_LIMIT   # so the page can name the cap it reports


def jsonable(value: Any) -> Any:
    """Make a DuckDB value JSON-safe.

    Decimal becomes a string, not a float: money is cast to DECIMAL(18,2) by the
    agent precisely so it prints exactly, and routing it through a float would put
    the rounding error back.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


@main.get("/health")
def health() -> dict[str, Any]:
    """Liveness plus the facts worth checking before a demo."""
    return {
        "status": "ok",
        "model": MODEL,
        "tables": _engine.tables,
        "row_limit": DEFAULT_ROW_LIMIT,
        "max_tool_calls": MAX_TOOL_CALLS,
        "query_timeout_seconds": _engine.timeout_seconds,
        "credentials_configured": bool(_agent.client.api_key or _agent.client.auth_token),
    }


@main.post("/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    answer = _agent.ask(request.question)
    return AskResponse(
        answer=answer.answer,
        sql=answer.sql,
        columns=answer.columns,
        rows=[[jsonable(v) for v in row] for row in answer.rows],
        tool_calls=[
            ToolCallOut(
                sql=c.sql,
                executed_sql=c.executed_sql,
                ok=c.ok,
                rejected=c.rejected,
                clamped_from=c.clamped_from,
                truncated=c.truncated,
                row_count=len(c.rows),
                error=c.error,
            )
            for c in answer.tool_calls
        ],
        error=answer.error,
    )


@main.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


main.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
