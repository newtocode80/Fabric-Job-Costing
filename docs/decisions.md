# Decisions and standing instructions

Running log of design decisions I made that the build spec did not specify, plus
instructions from the project owner that apply to a later milestone.

## The model this app is built against

**`lh_job_costing`, schema `silver` — seven tables**, exported to Parquet in
`data/silver` and tracked in git. Row counts are confirmed by the model owner and
enforced by `scripts/verify_exports.py`:

| Table | Rows |
|---|---|
| `dim_change_order` | 26 |
| `dim_cost_type` | 4 |
| `dim_date` | 669 |
| `dim_employee` | 45 |
| `dim_job` | 52 |
| `fact_job_budget` | 196 |
| `fact_job_cost` | 10,365 |

There is **no TMDL for this model.** M2 therefore inspects the Parquet schemas and
drafts `model/model.yaml` — role, grain, keys and relationships per table — which
becomes the derived artifact injected into the system prompt.

## Standing instructions (owner)

| # | Instruction | Applies at | Status |
|---|---|---|---|
| 1 | Declare **only relationships the data gives evidence for**. Report gaps; do not fill them. | M2 | Active |
| 2 | Show the `model/model.yaml` **draft for review before wiring it into the prompt**. | M2 | Active — draft written, **awaiting sign-off**, not wired in |
| 5 | For every declared relationship, show join keys, observed cardinality and orphan counts on both sides. | M2 | Done — `scripts/verify_model.py` |
| 6 | Declare each fact's grain explicitly and **prove** the candidate key is unique in the data. | M2 | Done — one proof failed, reported as DQ1, not smoothed over |
| 3 | README must state that the data dictionary is a generated artifact, not a source of truth; the TMDL is ground truth. | M6 | **Superseded** — see below |
| 4 | Flag any relationship the dictionary claims that the TMDL does not define. | M2 | **Superseded** — see below |

Instructions 3 and 4 were issued when the project was believed to target
`lh_operations_intelligence`. That model has a TMDL and a dictionary; this one has
neither. The *principle* behind instruction 4 survives as instruction 1.

### The archived dictionary

The five-table dictionary that used to sit at `docs/data-dictionary.md` documents the
`lh_operations_intelligence` model in a **separate workspace**. It was moved to
`docs/archive/operations-intelligence-data-dictionary.md` and given a header saying
so, on the owner's instruction, because leaving it at the top of `docs/` invited
reading it as this project's schema. It is reference material and is authoritative
for nothing here.

## Retired: the fixture generator

`scripts/generate_fixtures.py` is **retired and must not be run to produce app
data.** It is kept in the repo as a record, not as a live tool.

**Why it existed.** At M0 the repo held no data, no schema files and no Fabric
credentials, and this environment has no Fabric connectivity. Given the choice
between blocking and generating stand-in data, the owner chose synthetic fixtures
so M1–M6 could proceed.

**Why it was replaced.** It was built against the wrong model — the five-table
`lh_operations_intelligence` schema described in
`docs/archive/operations-intelligence-data-dictionary.md`. The app
targets the seven-table `lh_job_costing` star schema, and real exports of it now
exist. Nothing the generator produces is relevant to that model.

**Safety.** Its default output was moved from `data/silver` to `data/generated`
(gitignored) so running it cannot overwrite a real export, and its module docstring
opens with the retirement notice.

## Decisions

| # | Decision | Why |
|---|---|---|
| 1 | DuckDB is **in-memory over Parquet views**, no persisted `.duckdb` file. | Refreshing an export is a file drop, not a rebuild. Keeps `DuckDBEngine` stateless. |
| 2 | Relation names are the **Parquet file stems**. | File names are already the model's table names, so the M0 entity-to-table mapping collapsed to discovery. One less place for names to drift. |
| 3 | Confirmed row counts live in `load_duckdb.EXPECTED_ROWS` and are checked by `scripts/verify_exports.py`. | A truncated or stale export should fail loudly rather than quietly change every answer the agent gives. |
| 4 | `data/silver/` is **tracked**; only `data/generated/` is ignored. | The real exports are the model's data and belong in history. |

## M2 — deriving model.yaml without a TMDL

| # | Decision | Why |
|---|---|---|
| 5 | `model/model.yaml` is the **declaration of record**. With no TMDL there is no upstream truth to derive from, so this file is authored once and then defended by tests, rather than regenerated. | A generated file with no source to generate from would be a fiction. Making it hand-authored but machine-checked puts the honesty in the verifier. |
| 6 | `scripts/verify_model.py` **re-measures every claim** in model.yaml against the data and exits non-zero on disagreement. | The declaration cannot quietly drift from the exports. Refreshing an export re-runs the proof. |
| 7 | `dim_change_order` is declared **role: fact** despite its `dim_` prefix. Its source name is left unchanged. | It has a foreign key to `dim_job`, an additive `Amount` and an event date — a transaction fact by behaviour. Renaming it would break the match to the source table. |
| 8 | `fact_job_cost.CostID` is declared `unique: false` with a separate `working_key`, rather than picking a key that happens to work. | `CostID` is the intended key and it is violated. Declaring it clean would hide DQ1; declaring a different key would hide that the intended one is broken. |
| 9 | Four joins the data would support are listed under `undeclared_candidates` instead of `relationships`. | Each is a modelling decision the data cannot settle — role-playing date joins where more than one date column qualifies, and a region attribute with no path to a job. Owner's call. |
| 10 | Casts are written into each relationship's `join` clause, and the verifier runs that exact clause. | Three of the seven joins need a cast. Testing a reconstructed join would prove something other than what the app issues. |

## The dim_date extension — how it was made, and why it is fragile

**Reported by the owner.** `dim_date` was extended from 669 to 822 rows, now running
through 2026-12-31, to close the coverage gap recorded as DQ3.

**How.** The new rows were **appended directly to the silver Delta table**. The
bronze source `dim_date.csv` was *not* fixed and the pipeline was *not* re-run.
Appended rows are stamped `_source_file = dim_date_extension_2026H2`, distinguishing
them from the original 669 rows stamped `dim_date.csv`.

**Why this matters.**

| Risk | Consequence |
|---|---|
| Bronze is still short | `dim_date.csv` still ends 2026-07-31. Bronze and silver now disagree. |
| A pipeline rerun reverts it | Any full refresh that rebuilds silver from bronze **silently drops the 153 appended rows** and DQ3 returns. |
| The fix is invisible to lineage | Everything else in the model carries a single `_bronze_run_id` from one ingest. These rows do not come from that run. |

**Consequence for this app.** The date coverage the model depends on is not
reproducible from source. `scripts/verify_exports.py` pins the expected row count,
so a silent reversion fails loudly rather than quietly shortening every date-filtered
answer. That is a tripwire, not a fix — the durable fix is correcting bronze and
re-running the pipeline.

**Status in this repository: NOT YET PRESENT.** As of the latest fetch, every ref —
this branch, `origin/main` and `FETCH_HEAD` — carries the same `dim_date.parquet`
blob (`0e3da376440b`): 669 rows, ending 2026-07-31, with `dim_date.csv` as the only
`_source_file`. `EXPECTED_ROWS` still reads 669 and DQ3 still stands, because neither
may change until the extended export actually lands and the orphan count is measured
at zero.
