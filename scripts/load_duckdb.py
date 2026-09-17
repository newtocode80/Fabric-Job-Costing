"""Load the silver Parquet exports into DuckDB and print row counts per table.

Reads the real exports of the `lh_job_costing` lakehouse, schema `silver`, from
data/silver. DuckDB runs in memory and each table is a view over its Parquet file,
so refreshing an export is a file drop -- there is no database to rebuild.

Relation names are the Parquet file stems, which are already the model's table
names (dim_job.parquet -> dim_job), so no entity-to-table mapping is needed.

Usage:  python scripts/load_duckdb.py [--data data/silver]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "silver"

# Row counts confirmed against the source lakehouse by the model owner. The loader
# does not enforce these -- scripts/verify_exports.py does -- but they record what a
# correct export looks like.
EXPECTED_ROWS: dict[str, int] = {
    "dim_change_order": 26,
    "dim_cost_type": 4,
    "dim_date": 669,
    "dim_employee": 45,
    "dim_job": 52,
    "fact_job_budget": 196,
    "fact_job_cost": 10365,
}


def discover(data_dir: Path) -> dict[str, Path]:
    """Map relation name -> Parquet path for every export in `data_dir`."""
    paths = sorted(data_dir.glob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"No Parquet exports found in {data_dir}")
    return {path.stem: path for path in paths}


def load(data_dir: Path = DEFAULT_DATA_DIR) -> duckdb.DuckDBPyConnection:
    """Return an in-memory connection with one view per exported table."""
    con = duckdb.connect()
    for table, path in discover(data_dir).items():
        # DuckDB cannot prepare DDL, so the path is inlined with quotes escaped.
        literal = path.as_posix().replace("'", "''")
        con.execute(f"""CREATE VIEW "{table}" AS SELECT * FROM read_parquet('{literal}')""")
    return con


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()

    try:
        con = load(args.data)
        tables = discover(args.data)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"{'Table':<20} {'Columns':>7} {'Rows':>9}")
    print("-" * 38)
    total = 0
    for table in tables:
        rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        columns = len(con.execute(f'SELECT * FROM "{table}" LIMIT 0').description)
        total += rows
        print(f"{table:<20} {columns:>7} {rows:>9,}")
    print("-" * 38)
    print(f"{'total':<20} {'':>7} {total:>9,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
