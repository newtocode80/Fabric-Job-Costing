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
| 2 | Show the `model/model.yaml` **draft for review before wiring it into the prompt**. | M2 | Active |
| 3 | README must state that `docs/data-dictionary.md` is a generated artifact, not a source of truth; the TMDL is ground truth. | M6 | **Superseded** — see below |
| 4 | Flag any relationship the dictionary claims that the TMDL does not define. | M2 | **Superseded** — see below |

Instructions 3 and 4 were issued when the project was believed to target
`lh_operations_intelligence`. That model has a TMDL and a dictionary; this one has
neither. The *principle* behind instruction 4 survives as instruction 1.

### Open question for the owner

`docs/data-dictionary.md` documents the five-table `lh_operations_intelligence`
model — **a separate project**. It is still the only committed schema description in
the repo, so it is a live trip hazard for anyone reading it as authoritative.
Awaiting a decision: delete it, move it under `docs/archive/` with a header naming
the other project, or leave it as is. Untouched until then.

## Retired: the fixture generator

`scripts/generate_fixtures.py` is **retired and must not be run to produce app
data.** It is kept in the repo as a record, not as a live tool.

**Why it existed.** At M0 the repo held no data, no schema files and no Fabric
credentials, and this environment has no Fabric connectivity. Given the choice
between blocking and generating stand-in data, the owner chose synthetic fixtures
so M1–M6 could proceed.

**Why it was replaced.** It was built against the wrong model — the five-table
`lh_operations_intelligence` schema described in `docs/data-dictionary.md`. The app
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
