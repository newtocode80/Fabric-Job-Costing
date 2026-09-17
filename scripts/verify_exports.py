"""Check the silver exports in data/silver against their confirmed row counts.

Guards against a truncated, stale or partially-written export reaching the agent.
Exits non-zero and names every discrepancy if anything disagrees.

Usage:  python scripts/verify_exports.py [--data data/silver]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load_duckdb import DEFAULT_DATA_DIR, EXPECTED_ROWS, discover, load  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()

    try:
        con = load(args.data)
        found = discover(args.data)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    problems: list[str] = []
    for table in sorted(set(EXPECTED_ROWS) - set(found)):
        problems.append(f"{table}: export missing from {args.data}")
    for table in sorted(set(found) - set(EXPECTED_ROWS)):
        problems.append(f"{table}: unexpected export, not in the confirmed table list")

    print(f"{'Table':<20} {'Expected':>9} {'Actual':>9}  Result")
    print("-" * 51)
    for table, expected in sorted(EXPECTED_ROWS.items()):
        if table not in found:
            print(f"{table:<20} {expected:>9,} {'--':>9}  MISSING")
            continue
        actual = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        ok = actual == expected
        print(f"{table:<20} {expected:>9,} {actual:>9,}  {'ok' if ok else 'MISMATCH'}")
        if not ok:
            problems.append(f"{table}: expected {expected:,} rows, found {actual:,}")

    print("-" * 51)
    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"All {len(EXPECTED_ROWS)} exports match their confirmed row counts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
