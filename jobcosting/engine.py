"""Query engines.

`QueryEngine` is the seam between the agent and wherever the data physically
lives. `DuckDBEngine` reads the Parquet exports in data/silver. A `FabricEngine`
issuing the same SQL against the lakehouse would implement the same protocol; the
agent and the API layer never learn which one they hold.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data" / "silver"

# Guardrail 4. A runaway query must not hold the request open indefinitely.
DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class QueryResult:
    """One query's output.

    The build spec writes the protocol as `run(sql) -> rows`. Carrying the column
    names alongside the rows is a deliberate widening: the /ask response at M4 must
    return `columns` as well as `rows`, and recovering them from tuples afterwards
    is not possible.
    """

    columns: list[str]
    rows: list[tuple[Any, ...]]

    @property
    def row_count(self) -> int:
        return len(self.rows)


@runtime_checkable
class QueryEngine(Protocol):
    def run(self, sql: str) -> QueryResult: ...


class DuckDBEngine:
    """Runs SQL against the Parquet exports through an in-memory DuckDB.

    One connection per engine, with a view per table. Nothing is persisted, so
    refreshing an export is a file drop.
    """

    def __init__(
        self,
        data_dir: Path = DEFAULT_DATA_DIR,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.data_dir = data_dir
        self.timeout_seconds = timeout_seconds
        paths = sorted(data_dir.glob("*.parquet"))
        if not paths:
            raise FileNotFoundError(f"No Parquet exports found in {data_dir}")
        self._con = duckdb.connect()
        self.tables = [p.stem for p in paths]
        for path in paths:
            # DuckDB cannot prepare DDL, so the path is inlined with quotes escaped.
            literal = path.as_posix().replace("'", "''")
            self._con.execute(
                f"""CREATE VIEW "{path.stem}" AS SELECT * FROM read_parquet('{literal}')"""
            )

    def run(self, sql: str) -> QueryResult:
        """Execute `sql`, abandoning it after `timeout_seconds`.

        DuckDB has no per-query time limit, so the cap is a timer that calls
        `interrupt()` on the connection. The interrupt surfaces as an exception in
        the thread running the query, and the connection stays usable afterwards --
        one slow question must not break the next one.
        """
        timer = threading.Timer(self.timeout_seconds, self._con.interrupt)
        timer.start()
        try:
            cursor = self._con.execute(sql)
            rows = cursor.fetchall()
        except duckdb.InterruptException as exc:
            raise TimeoutError(
                f"The query ran longer than {self.timeout_seconds} seconds and was "
                f"stopped. Narrow it with a WHERE clause, aggregate instead of "
                f"returning detail rows, or query fewer tables at once."
            ) from exc
        finally:
            timer.cancel()
        columns = [d[0] for d in cursor.description] if cursor.description else []
        return QueryResult(columns=columns, rows=rows)

    def close(self) -> None:
        self._con.close()
