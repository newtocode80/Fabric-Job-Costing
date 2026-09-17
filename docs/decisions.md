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

## M2 rulings (owner) — the four undeclared candidates

All four were put to the owner and ruled. None was decided by me.

| Candidate | Ruling | Where it landed |
|---|---|---|
| `dim_job` → `dim_date` (3 date columns) | **All three as named roles** | `relationships`, roles `job_start`, `job_scheduled_end`, `job_actual_end` |
| `dim_change_order` → `dim_date` (2 date columns) | **Both as named roles** | `relationships`, roles `co_submitted`, `co_approved` |
| budget ↔ actual | **Prescribe the join pattern** | `analysis_patterns.budget_vs_actual`, with runnable SQL |
| `dim_employee.Region` | **Refuse, specifically and constructively** | `cannot_answer.cost_or_revenue_by_region` |

The region refusal was further specified by the owner and must: name that
`dim_employee.Region` is the only region attribute; say it describes the employee
and not the job; say `dim_job` has no region column; say answering would require a
region attribute on the job dimension; and offer the labour-only figure **as a
separate question the user may choose to ask**, never as the answer given unasked.

## M2 decisions

| # | Decision | Why |
|---|---|---|
| 11 | `model/schema_context.md` is generated from `model.yaml` by `scripts/build_schema_context.py`, and `--check` fails if the cache is stale. | The spec requires a cached generated schema file. Generating it means the prompt cannot drift from the declaration. ~4,000 tokens. |
| 12 | The YAML keeps evidence, provenance and commentary; the rendered context keeps only what the agent needs to write correct SQL. | Orphan counts and lineage notes are for reviewing the model, not for the model's prompt. |
| 13 | `verify_model.py` also checks **column coverage** — every non-lineage column documented, and nothing documented that is not in the data. | Added after six column descriptions were silently truncated by unquoted commas inside YAML flow mappings. The check catches that class of loss rather than trusting review. |
| 14 | Role-playing joins declare an explicit `alias` per role. | Three joins to `dim_date` from one table need three aliased copies. Naming the alias in the declaration removes a decision the agent would otherwise make differently each time. |

## dim_date extension — verified on arrival

The 822-row export landed as commit `d75791c`. Every claim was re-measured, not assumed:

| Check | Before | After |
|---|---|---|
| `verify_exports.py` | 669 | **822, all 7 tables ok** |
| `fact_job_cost.CostDate` orphans | 85 rows / 41 dates | **0** |
| `job_scheduled_end` orphans | 2 | **0** |
| `job_actual_end` orphans | 1 | **0** |
| Contiguity | 669 days | **822 rows over 822 calendar days, no gaps** |
| `DateKey` vs `Date` | consistent | **consistent, 0 mismatches** |

Appended rows carry `_source_file = dim_date_extension_2026H2` and
`_bronze_run_id = manual-extension-20260917`, exactly as reported. **DQ3 was removed
only after the containment check came back clean.** The provenance risk is recorded
on the `dim_date` table as a `provenance` key that the renderer deliberately does not
emit — it is something the repository must remember, not something the agent acts on.

## M1 decisions

| # | Decision | Why |
|---|---|---|
| 15 | A **manual tool-use loop**, not the SDK's beta `tool_runner`. | Three reasons the runner does not cover: the 3-call budget must be enforced *and* announced to the model when spent; every call's SQL, columns and rows must be captured for the `/ask` response shape at M4; and the runner is beta. |
| 16 | `QueryEngine.run` returns a `QueryResult` (columns + rows), not bare rows as the spec writes it. | `/ask` must return `columns`, and column names cannot be recovered from tuples afterwards. A deliberate widening of the spec, noted here rather than made silently. |
| 17 | When the call budget is spent the tool is **withdrawn** from the request. | Otherwise the model emits a call that cannot run and the turn is wasted. It is also told, in the same turn, to answer from what it has. |
| 18 | The model sees at most 50 rows plus the true `row_count`; the caller gets every row. | A wide result would crowd the conversation. The model is told it was cut, so it aggregates in SQL rather than counting rows by eye. |
| 19 | A failed query returns to the model as `is_error` with the DuckDB message. | This is error handling, not a guardrail. Parse-based rejection, the allowlist, LIMIT injection and the timeout all arrive at M3. |
| 20 | Adaptive thinking (`thinking: {"type": "adaptive"}`). | Recommended for `claude-sonnet-4-6`; SQL over a model with cast-dependent joins and a required pattern is not a one-shot task. |
| 21 | Reaching the model is wrapped in a **broad** `except Exception`. | Found by testing: unresolved credentials raise `TypeError`, not `APIError`, so a narrow handler let a traceback escape and would have killed a multi-question run. |

## Three fixes from the first live run

All three came from watching the agent actually fail, not from review. Each is fixed
where the agent will see it, and covered by a test.

| # | Symptom observed | Fix | Where |
|---|---|---|---|
| 1 | First SQL used `lh_job_costing.silver.fact_job_cost` and failed with a Catalog Error | New `sql_dialect` block states relations are bare table names, with a correct and a failing example side by side | `model.yaml` → rendered as "Writing SQL (DuckDB)" near the top of `schema_context.md` |
| 2 | A 66-row result truncated at 50; the model spent its second call on `OFFSET 50` | Row cap raised 50 → **250**, above the largest documented pattern (231 rows). When a result still truncates, the note gives the true row count and says explicitly not to page, naming aggregate-or-filter as the recovery | `agent.py` |
| 3 | SQL panel showed `4497901.879999999` | `sql_dialect.money_rule` requires `CAST(... AS DECIMAL(18,2))` on money in the SELECT list, cast at the end of the arithmetic so rounding does not accumulate | `model.yaml` → `schema_context.md` |

Fix 3 also applies to the **prescribed `budget_vs_actual` SQL itself**, which returned
raw doubles. That SQL is the exemplar the agent copies, so an unrounded exemplar
teaches the behaviour being corrected. It now casts, and returns `Decimal` values.

The pattern also gained a `grain_note`: "budget vs actual by job" aggregates to **66
rows** (52 `dim_job` keys + 14 orphan keys from DQ2), while the declared
`JobKey × CostTypeKey` SQL is 231 rows. Both reconcile to the same two totals, and
the note states them so a wrong query is self-evident.

## M3 decisions — guardrails

Tests were written first: `tests/test_guardrails.py`, 69 cases, failing on a missing
module before any implementation existed.

| # | Decision | Why |
|---|---|---|
| 22 | Parsing is **sqlglot** in its DuckDB dialect. | A real parser is what "not by regex" requires. Three tests exist only to prove the difference: a `DROP TABLE` inside a string literal is allowed, one inside a comment is allowed, and `/* SELECT */ DROP TABLE dim_job` is rejected. A keyword blocklist gets all three wrong. |
| 23 | The allowlist check rejects any `Table` node with an **empty name**. | `read_parquet('/etc/passwd')` parses as an ordinary `Select`, and the file-reading function hangs off a nameless `Table` node. An allowlist that only compared names would wave it through — statement type alone stops nothing here. |
| 24 | CTE aliases are collected and excluded from the allowlist check. | `WITH b AS (...) SELECT * FROM b` references a CTE, not a table. Without this the prescribed `budget_vs_actual` pattern would be rejected by its own guardrails. |
| 25 | LIMIT is **appended as text**, not regenerated from the AST. | The parse decides *whether* to inject; the SQL that runs is the model's own text. Regenerating would reformat the query and strip its comments, and the SQL panel is a first-class feature. Safe because the statement is already proven to be a single query. |
| 26 | The timeout is a `threading.Timer` calling DuckDB's `interrupt()`. | DuckDB has no per-query time limit. A test asserts the connection still works **after** an interrupt — one slow question must not break the next. |
| 27 | `ToolCall` records both `sql` (as written) and `executed_sql` (as run), and `Answer.sql` returns the executed one. | The panel must show the query that produced the rows, LIMIT included. Keeping both means neither is misrepresented. |
| 28 | The guardrails are stated in the system prompt, not only enforced. | A rejection costs one of three calls. Telling the model the rules up front means it rarely spends one discovering them; the rejection path is the backstop, not the teacher. |
| 29 | Guardrails sit in the **tool layer**, not the engine. | `check()` is engine-agnostic, so a `FabricEngine` inherits rules 1-3 unchanged. Only the timeout is engine-specific, because only the engine can cancel its own query. |

### Closed: explicit LIMITs are now clamped

The owner ruled: any explicit LIMIT above 500 comes down to 500, and the clamp must
not be silent.

| # | Decision | Why |
|---|---|---|
| 30 | The clamp **wraps** the query — `SELECT * FROM (<original>) LIMIT 500` — rather than editing the LIMIT in place or regenerating from the AST. | The model's query survives verbatim inside, so the panel shows both what was asked for and the cap applied, instead of a silently edited number. Verified against ORDER BY (ordering preserved), duplicate column names (auto-renamed), CTEs and trailing comments. |
| 31 | `check()` now returns a `CheckedQuery` rather than a string. | A clamp has to report the requested limit, the applied limit and a note; a bare string cannot carry that, and returning it out-of-band would invite callers to ignore it. |
| 32 | **Injection is silent; clamping is not.** | Injecting a limit overrides nothing the model chose. Clamping overrides an explicit instruction, so it always produces a note. |
| 33 | The clamp note is prepended **ahead of the rows** in the tool result. | A note the model has to scroll past is a note it may act on too late. |
| 34 | The note's guidance matches the truncation note's word for word in substance. | The wrong recovery is the same in both cases — asking for more rows instead of aggregating — so the instruction should not differ. |

`ToolCall` records `sql` (as written), `executed_sql` (as run) and `clamped_from`, and
the CLI prints the clamp explicitly rather than showing only the wrapped query.

## M4 decisions — API and page

| # | Decision | Why |
|---|---|---|
| 35 | The FastAPI instance is named `main` in `app.py`. | The spec says `uvicorn app:main` runs it. That is `module:attribute`, so the app object has to be called `main` — unusual, but it is what was asked for. |
| 36 | `DuckDBEngine.run` now takes a lock. | FastAPI serves sync endpoints from a threadpool, and one DuckDB connection is not safe across threads. Queries are serialised; the 10s timeout stops any one of them holding the lock. |
| 37 | `Decimal` is serialised as a **string**, not a float. | Money is cast to `DECIMAL(18,2)` precisely so it prints exactly. A float round-trip would put `.81000002` straight back. A test asserts this. |
| 38 | The page builds every node with `document.createElement` and `textContent`. | Answers, SQL and row values all originate outside the page. Nothing from the API is ever interpreted as markup. |
| 39 | The panel shows `executed_sql`, with the clamp called out separately. | The panel must show the query that produced the rows. When a clamp wrapped it, a flag says so rather than leaving a reader to spot the wrapper. |
| 40 | Table values are shown exactly as returned — no thousands separators. | The panel's job is to show what the query returned. Formatting there would diverge from the data; formatting belongs in the prose answer. Numerics are right-aligned in a monospace column instead. |
| 41 | Body text 18px, answer 21.6px, one accent (`#1b5e9c`), system font stack. | Readable from the back of a room. No gradients, no chat bubbles, no typing animation, per the spec. |

**Bug found by running it, not by review:** `renderTable` used
`table.append(el("thead")).firstChild`, but `Node.append()` returns `undefined`, so
the call threw and the panel never opened. Only launching the page and watching the
console caught it — `TestClient` never executes the JavaScript.

## Narrative arithmetic — the third failure of the same kind

Observed three times: "33 of 44 finished late" (44 never queried), "all 52 known jobs"
and "30 jobs under budget". The guardrails protect the SQL; nothing protected the prose.

**System prompt rule 2** now requires that counts, totals and percentages be present
in the returned rows or follow directly from them, and that a denominator or
comparison group be *queried for* rather than estimated — with an explicit
instruction that if the remaining call budget will not allow it, the model gives the
number it has and says what it could not establish.

**`jobcosting/grounding.py`** enforces it mechanically. A number in the prose counts
as supported when it is a cell value, a row count, a column sum/min/max/count, a
number from the question, a percentage of any two of those, or any of these restated
at scale ("$4.5 million"). Anything else is reported, by value, for a human to judge.

It is **conservative and reports rather than concludes**: it can flag a number that
was derivable in a way it does not model. That is the right direction for the error
to run — a false flag costs a glance, a missed one ships a wrong figure.

| # | Decision | Why |
|---|---|---|
| 42 | Groundedness is a **universal check on every eval case**, not one case among fifteen. | The failure is not tied to a question type. It appeared on a late-jobs question and a budget question alike. |
| 43 | The checker lives in `jobcosting/`, with its own tests, not inside the runner script. | It is a real component with real edge cases; burying it in a script would make it untestable. |
| 44 | Numbers glued to letters, dots or hyphens are not numbers. | Found by its own test: `J-202551` parsed as `-202551` and was reported as invented. Job numbers are identifiers. |

## M5 decisions — eval set and runner

| # | Decision | Why |
|---|---|---|
| 45 | Cases declare `expect_value_sql` / `avoids_value_sql`: ground-truth queries the runner executes. | "Expected behaviour, not an exact string" still allows objective numeric truth. The `avoids` form is how the fan-out figure and the unfiltered change-order total are caught. |
| 46 | `--dry-run` validates the file and runs every ground-truth query without calling the model. | The eval set can be checked for correctness for free, and a ground-truth query that returns nothing would silently pass a case. A test asserts all of them return a value. |
| 47 | The runner is tested against **synthetic answers** carrying each known failure. | Otherwise the eval suite is only as trustworthy as the one time someone watched it run. Tests cover the invented denominator, the fan-out figure, a refusal that answered anyway, and a guess where a clarifying question was required. |

### Two refusal cases, not one

`cost_by_region` (region is an employee attribute) and `profit_by_job` — **this model
has no revenue at all**. Contract value is not revenue recognised, and there are no
invoices. Profit is not computable, and it is the more tempting question of the two.

## Declared known issues are a supported source

The owner ruled: figures declared as known issues are grounded by definition. They
reach the agent through the system prompt precisely so it can caveat its answers,
and rule 6 requires it to. Quoting `16,882.98` is reading the declaration, not
inventing a number.

| # | Decision | Why |
|---|---|---|
| 48 | Scoped to the **known-issues section only** (`data_quality` in model.yaml), not all of model.yaml. | Table row counts are declared too, but "all 52 known jobs" was ruled a genuine failure. The line is what a figure is used FOR: a known issue is quoted as a caveat, a row count gets computed into an assertion about a result. |
| 49 | Extraction from declarations is **permissive** — no identifier lookbehind, so `(53-71)` yields both endpoints. | This reads a declaration, not prose. A stray match here can only remove a false positive, never create one. |
| 50 | Relationship evidence figures (orphan counts, NULL shares) are **not** included yet. | All four figures the owner named are in `data_quality`, so the tighter scope suffices. Easy to widen if flagged numbers turn out to come from the join warnings. |

Tests pin both sides: the six declared figures ground, and the invented denominator
and the row-count-as-denominator still fail.

## Replaying a run

`evals/run.py` now saves every answer to `evals/last-run.json`, and `--replay`
re-scores that file with no model calls.

The checks are what gets iterated on. Without this, every adjustment to the
groundedness rule costs another 15 live calls to see its effect — which is exactly
the situation this change was made in. A test asserts a replayed answer scores
identically to the live one, including that Decimals survive the JSON round trip;
saved as strings, they would otherwise ground nothing.

`last-run.json` is gitignored: it is output, not a source file.

## Losing a completed run to the final write

A run made all fifteen model calls and then crashed serialising them: `date` was
not handled. The serialisation bug was the trigger; **the defect was that one
write at the end could discard fifteen completed calls.**

Root cause of the trigger: `app.py` already had a correct encoder covering
`Decimal`, `date`, `datetime`, `time` and `bytes`. `evals/run.py` had a second,
weaker copy that handled only `Decimal`. Two encoders, one of them wrong.

| # | Decision | Why |
|---|---|---|
| 51 | **One serialisation module**, `jobcosting/serialisation.py`, used by both the API and the runner. | The duplication was the bug. A second copy of a conversion is a second place to forget a type. |
| 52 | Two encodings, deliberately: `encode`/`decode` (tagged, lossless) for saved runs, `to_display` (plain) for the API. | A replay must score identically to the live run, so a Decimal has to come back a Decimal. The browser wants `"2026-04-24"`, not a tagged wrapper. |
| 53 | **Neither encoder can raise.** An unknown type degrades to its string form. | No bug in a conversion function is worth fifteen model calls. Tested with a deliberately exotic object. |
| 54 | The run is saved **after every case**, before scoring, to a temp file that is then renamed. | A crash at case twelve now costs one answer, not twelve. Saving before scoring means a bug in a *check* cannot cost the calls either. The rename makes a half-written file impossible. |
| 55 | `save()` catches its own failures and warns. | It is called inside the loop; if it raised, it would become the very failure it exists to prevent. |
| 56 | The whole loop is wrapped in `try/finally` that saves and prints the table. | Belt and braces over the per-case save. |

Types covered and round-trip tested individually: `Decimal`, `date`, `datetime`,
`time`, `bytes`, `timedelta`, `UUID`, `int`, `float`, `bool`, `None`, `str`, plus
lists and dicts — and the unknown-type fallback.

**A second bug, found by the test for the first:** `save(records, path=LAST_RUN)`
bound the path as a default argument, so it was captured at import and ignored any
override — the test meant to write to a temp directory wrote into the repo instead.
The path is now resolved at call time.

## List markers and ordinals are not figures

An answer that numbered its findings had its own list markers reported as invented
numbers: `8. J-202551 - $232,248 over` contributed an 8. Same for ordinals -- the
"1" in "1st quarter".

`extract_numbers` now strips a leading `N.` or `N)` marker from each line before
extracting, and rejects a number followed by an ordinal suffix. A number that merely
*opens* a sentence keeps its meaning: "30 cost rows are orphaned" still reports 30.

Two process notes, both mine:

- **A backtracking hole.** `23rd` failed the ordinal check as "23", then the engine
  backtracked to "2" and passed it. Fixed with a not-followed-by-digit guard.
- **Two `str.replace` calls silently no-opped** because I did not assert the anchor
  was present, leaving `LIST_MARKER` referenced but undefined. Every edit to a
  source file now asserts its anchor before replacing.

`evals/last-run.json` is **no longer gitignored.** Ignoring it contradicted the one
thing it is for: re-scoring a run in a different session without spending fifteen
more model calls. It is output, but it has to be shareable.
