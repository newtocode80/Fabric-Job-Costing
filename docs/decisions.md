# Decisions and standing instructions

Running log of design decisions I made that the build spec did not specify, plus
instructions from the project owner that apply to a later milestone.

## Standing instructions (owner)

| # | Instruction | Applies at |
|---|---|---|
| 1 | `docs/data-dictionary.md` is a **generated artifact, not a source of truth**. The TMDL is the model's ground truth. Say this in the README. | M6 |
| 2 | When parsing the TMDL, **flag any relationship the dictionary claims but the TMDL does not define** — report the gap, do not fill it in. | M2 |

Instruction 2 already has a known trigger: §6 of the dictionary documents three
many-to-one relationships and states they were confirmed verbally by the model owner
because the supplied table files contained no relationship definitions. If the
committed `relationships.tmdl` does not define all three, that is a gap to report at
M2, not to patch.

## M0 — fixture data

| # | Decision | Why |
|---|---|---|
| 1 | Fixture rows are **generated, not real**. `scripts/generate_fixtures.py` is seeded (`SEED = 42`) so every run produces identical data. | No Fabric connectivity from this environment. Owner chose synthetic fixtures to unblock M1–M6. |
| 2 | Parquet files are named for the **silver source entity** (`silver_jobs.parquet`); DuckDB relations are named for the **model table** (`FactJobs`). | Mirrors the TMDL `entityName` → table mapping, so the M2 schema context and the M3 allowlist name the same things the agent writes SQL against. |
| 3 | DuckDB is **in-memory over Parquet views**, with no persisted `.duckdb` file. | Swapping fixtures for real exports is a file drop, not a rebuild. Keeps `DuckDBEngine` (M1) stateless. |
| 4 | `data/` is **gitignored**; the generator is committed. | Keeps generated data out of history and makes the swap to real exports unambiguous. |
| 5 | The fixture `MarginTarget` is written in **dollars**, not a percentage. | Dictionary open issue #3 records the units as unverified. The fixture has to pick one; this records which. Real exports settle it. |
| 6 | Fixture "today" is frozen at **2025-12-31** (`AS_OF`). | Invoice ageing and job completion depend on a current date; a frozen one keeps runs reproducible. |
| 7 | The entity → table mapping lives in `scripts/load_duckdb.py` as a literal. | Temporary. M2 replaces it with the TMDL-derived mapping. |
