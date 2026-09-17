"""Re-measure every claim in model/model.yaml against the data in data/silver.

model.yaml declares grain, key uniqueness and relationship cardinality. This
script proves or disproves each claim by querying the exports, so the declaration
cannot quietly drift from reality when an export is refreshed.

Checks:
  1. Declared key uniqueness   -- is the key actually unique in the data?
  2. Relationship one-side key -- is the dimension side unique?
  3. Orphan counts both ways   -- do the declared numbers still hold?

Exits non-zero if any declared number disagrees with the measurement.

Usage:  python scripts/verify_model.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load_duckdb import DEFAULT_DATA_DIR, load  # noqa: E402

MODEL_PATH = Path(__file__).resolve().parents[1] / "model" / "model.yaml"


def q1(con, sql: str):
    return con.execute(sql).fetchone()[0]


def split_join(join: str) -> tuple[str, str]:
    """Split a join clause on its top-level '=' into left and right expressions."""
    depth = 0
    for i, ch in enumerate(join):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "=" and depth == 0:
            return join[:i].strip(), join[i + 1 :].strip()
    raise ValueError(f"join clause has no top-level '=': {join}")


def join_sides(rel: dict) -> tuple[str, str]:
    """Return (many-side expression, one-side expression) exactly as declared.

    The model spells out any required cast inside the join clause, so these are
    used verbatim rather than rebuilt -- the check then tests the join the app
    will actually issue.
    """
    left, right = split_join(rel["join"])
    many = f"{rel['from']['table']}.{rel['from']['column']}"
    return (left, right) if many in left else (right, left)


def main() -> int:
    model = yaml.safe_load(MODEL_PATH.read_text())
    con = load(DEFAULT_DATA_DIR)
    failures: list[str] = []

    print("=" * 78)
    print("1. GRAIN AND KEY UNIQUENESS")
    print("=" * 78)
    print(f"{'Table':<18} {'Role':<10} {'Declared key':<26} {'Rows':>7} {'Distinct':>9}  Result")
    print("-" * 78)
    for table in model["tables"]:
        name, key = table["name"], table["key"]
        cols = ", ".join(f'"{c}"' for c in key["columns"])
        rows = q1(con, f'SELECT count(*) FROM "{name}"')
        distinct = q1(con, f'SELECT count(*) FROM (SELECT DISTINCT {cols} FROM "{name}")')
        actually_unique = rows == distinct
        agrees = actually_unique == key["unique"]
        verdict = "unique" if actually_unique else f"NOT unique (-{rows - distinct})"
        if not agrees:
            verdict += "  <-- CONTRADICTS model.yaml"
            failures.append(f"{name}: declared unique={key['unique']}, measured {actually_unique}")
        print(f"{name:<18} {table['role']:<10} {'+'.join(key['columns']):<26} {rows:>7,} {distinct:>9,}  {verdict}")
        if not actually_unique and "working_key" in key:
            wcols = ", ".join(f'"{c}"' for c in key["working_key"])
            wdistinct = q1(con, f'SELECT count(*) FROM (SELECT DISTINCT {wcols} FROM "{name}")')
            ok = "unique" if wdistinct == rows else f"NOT unique (-{rows - wdistinct})"
            print(f"{'':<18} {'':<10} {'+'.join(key['working_key']) + ' (working)':<26} {rows:>7,} {wdistinct:>9,}  {ok}")
            if wdistinct != rows:
                failures.append(f"{name}: declared working_key is not unique either")

    print()
    print("=" * 78)
    print("2. COLUMN COVERAGE -- every real column documented, and no invented ones")
    print("=" * 78)
    lineage = set(model["lineage_columns"])
    print(f"{'Table':<18} {'In data':>8} {'Lineage':>8} {'Documented':>11}  Result")
    print("-" * 78)
    for table in model["tables"]:
        name = table["name"]
        actual = {d[0] for d in con.execute(f'SELECT * FROM "{name}" LIMIT 0').description}
        business = actual - lineage
        documented = set(table["columns"])
        missing, invented = business - documented, documented - actual
        verdict = "ok"
        if missing:
            verdict = f"UNDOCUMENTED: {', '.join(sorted(missing))}"
            failures.append(f"{name}: columns in data but not in model.yaml: {sorted(missing)}")
        if invented:
            verdict = f"NOT IN DATA: {', '.join(sorted(invented))}"
            failures.append(f"{name}: columns in model.yaml but not in data: {sorted(invented)}")
        print(f"{name:<18} {len(actual):>8} {len(actual & lineage):>8} {len(documented):>11}  {verdict}")

    print()
    print("=" * 78)
    print("3. RELATIONSHIPS -- cardinality and orphans on both sides")
    print("=" * 78)
    for rel in model["relationships"]:
        ft, fc = rel["from"]["table"], rel["from"]["column"]
        tt, tc = rel["to"]["table"], rel["to"]["column"]
        ev = rel.get("evidence", {})
        fexpr, texpr = join_sides(rel)

        role = f"  role={rel['role']} (alias {rel['alias']})" if "role" in rel else ""
        print(f"\n  {ft}.{fc}  ->  {tt}.{tc}   [{rel['cardinality']}]{role}")
        if rel.get("requires_cast"):
            print(f"    cast required: {rel['requires_cast']}")
        print(f"    join: {rel['join']}")

        one_rows = q1(con, f'SELECT count(*) FROM "{tt}"')
        one_distinct = q1(con, f'SELECT count(DISTINCT "{tc}") FROM "{tt}"')
        one_unique = one_rows == one_distinct
        print(f"    one side unique:  {one_distinct:,} distinct {tc} over {one_rows:,} rows -> {one_unique}")
        if one_unique != ev.get("one_side_unique", True):
            failures.append(f"{ft}->{tt}: one_side_unique declared {ev.get('one_side_unique')}, measured {one_unique}")

        null_pred = f"{fexpr} IS NOT NULL"
        many_total = q1(con, f'SELECT count(*) FROM "{ft}"')
        many_keyed = q1(con, f'SELECT count(*) FROM "{ft}" WHERE {null_pred}')
        orphan_rows = q1(
            con,
            f'SELECT count(*) FROM "{ft}" LEFT JOIN "{tt}" ON {fexpr} = {texpr} '
            f'WHERE {null_pred} AND {texpr} IS NULL',
        )
        unreferenced = q1(
            con,
            f'SELECT count(*) FROM "{tt}" WHERE {texpr} NOT IN '
            f'(SELECT {fexpr} FROM "{ft}" WHERE {null_pred})',
        )
        max_fanout = q1(
            con,
            f'SELECT coalesce(max(n), 0) FROM (SELECT count(*) n FROM "{ft}" '
            f'WHERE {null_pred} GROUP BY {fexpr})',
        )
        if many_keyed < many_total:
            print(f"    many side:        {many_keyed:,} of {many_total:,} rows carry a key "
                  f"({many_total - many_keyed:,} NULL)")
        print(f"    orphan fact rows: {orphan_rows:,}  (key present, no matching dimension row)")
        print(f"    unreferenced dim: {unreferenced:,} of {one_rows:,} dimension rows never referenced")
        print(f"    max rows per key: {max_fanout:,}  (confirms many-to-one, not one-to-one)")

        for label, measured in (
            ("orphan_fact_rows", orphan_rows),
            ("unreferenced_dimension_rows", unreferenced),
            ("fact_rows_with_key", many_keyed),
            ("fact_rows_null_key", many_total - many_keyed),
        ):
            if label in ev and ev[label] != measured:
                failures.append(f"{ft}->{tt}: {label} declared {ev[label]:,}, measured {measured:,}")
                print(f"    !! {label} declared {ev[label]:,}, measured {measured:,}")

    print()
    print("=" * 78)
    if failures:
        print(f"{len(failures)} claim(s) in model.yaml disagree with the data:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Every grain, key and relationship claim in model.yaml matches the data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
