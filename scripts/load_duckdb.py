"""Load the silver Parquet exports into DuckDB and print row counts per table.

DuckDB runs in memory and each model table is a view over its Parquet file, so
replacing the fixtures with real exports is a file drop -- there is no database to
rebuild. Relations are named for the MODEL table (FactJobs), while the files keep
their silver ENTITY name (silver_jobs.parquet), mirroring the TMDL entityName
mapping the agent will see in its schema context.

Usage:  python scripts/load_duckdb.py [--data data/silver]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

# Entity -> model table name. TEMPORARY: M2 replaces this literal with the mapping
# derived from the TMDL, which is the model's ground truth.
ENTITY_TO_TABLE: dict[str, str] = {
    "silver_customers": "DimCustomer",
    "silver_jobs": "FactJobs",
    "silver_expenses": "FactExpenses",
    "silver_invoices": "FactInvoices",
    "silver_targets": "FactTargets",
}

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "silver"


def load(data_dir: Path) -> duckdb.DuckDBPyConnection:
    """Return an in-memory connection with one view per model table."""
    missing = [
        entity
        for entity in ENTITY_TO_TABLE
        if not (data_dir / f"{entity}.parquet").exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"No Parquet export for {', '.join(sorted(missing))} in {data_dir}.\n"
            "Run: python scripts/generate_fixtures.py"
        )

    con = duckdb.connect()
    for entity, table in ENTITY_TO_TABLE.items():
        # DuckDB cannot prepare DDL, so the path is inlined with quotes escaped.
        path = (data_dir / f"{entity}.parquet").as_posix().replace("'", "''")
        con.execute(f"""CREATE VIEW "{table}" AS SELECT * FROM read_parquet('{path}')""")
    return con


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()

    try:
        con = load(args.data)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"{'Table':<14} {'Entity':<18} {'Columns':>7} {'Rows':>9}")
    print("-" * 51)
    total = 0
    for entity, table in ENTITY_TO_TABLE.items():
        rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        columns = len(con.execute(f'SELECT * FROM "{table}" LIMIT 0').description)
        total += rows
        print(f"{table:<14} {entity:<18} {columns:>7} {rows:>9,}")
    print("-" * 51)
    print(f"{'total':<14} {'':<18} {'':>7} {total:>9,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
