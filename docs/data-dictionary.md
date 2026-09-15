# Job Costing Semantic Model — Data Dictionary

**Source lakehouse:** `lh_operations_intelligence`
**Storage mode:** DirectLake (all tables)
**Expression source:** `'DirectLake - lh_operations_intelligence'`
**Schema:** `dbo`
**Scope:** 5 tables · 34 columns · 4 measures · 3 relationships

---

## Status of this document

This dictionary reflects the **corrected** state of the model, not the state of the
TMDL files as originally supplied. Three corrections are incorporated:

| # | Correction | Status |
|---|---|---|
| 1 | `DImCustomer` → `DimCustomer` (capitalization) | **To apply** — see §7.1 |
| 2 | `Gross Margin $` formatted as currency | **To apply** — see §7.2 |
| 3 | Relationships: single-direction, many-to-one | **Confirmed** — see §6 |

Corrections 1 and 2 are changes that must still be made in the TMDL. Correction 3 is a
confirmation of intent: the supplied table files contained **no relationship definitions**
(TMDL stores these at model level, in `relationships.tmdl`, not in table files). The
relationships in §6 are documented as confirmed by the model owner and should be
validated against `relationships.tmdl` when that file is available.

---

## 1. DimCustomer

**Role:** Dimension — the only dimension in the model
**Grain:** One row per customer
**Key:** `CustomerID`
**DirectLake source:** `dbo.silver_customers`
**Table lineage tag:** `a77c2d2e-92a8-4ce7-af56-4fde0590585c`

| Column | Data type | Summarize by | Source column | Notes |
|---|---|---|---|---|
| `CustomerID` | string | none | CustomerID | Primary key; must be unique |
| `CustomerName` | string | none | CustomerName | Display name |
| `Industry` | string | none | Industry | Segmentation attribute |
| `Region` | string | none | Region | **Only** Region in the model — see §5 |
| `AccountManager` | string | none | AccountManager | Commercial owner |
| `CustomerSince` | dateTime | none | CustomerSince | Tenure / cohort analysis |
| `CustomerStatus` | string | none | CustomerStatus | e.g. active / dormant |

**Measures:** none.

> `Region` living only here — and not on any fact — is the root of the FactTargets
> isolation described in §5.

---

## 2. FactJobs

**Role:** Fact — **accumulating snapshot**, and the model's hub
**Grain:** One row per job
**Key:** `JobID`
**DirectLake source:** `dbo.silver_jobs`
**Table lineage tag:** `b4724e24-c47b-4d58-8250-a3701fc4d556`

| Column | Data type | Summarize by | Source column | Notes |
|---|---|---|---|---|
| `JobID` | string | none | JobID | Primary key; **must be unique** |
| `CustomerID` | string | none | CustomerID | FK → `DimCustomer` |
| `JobName` | string | none | JobName | Display name |
| `ProjectType` | string | none | ProjectType | Classification |
| `StartDate` | dateTime | none | StartDate | Milestone 1 |
| `ScheduledEndDate` | dateTime | none | ScheduledEndDate | Milestone 2 (planned) |
| `ActualEndDate` | dateTime | none | ActualEndDate | Milestone 3 (actual) |
| `ContractValue` | double | **sum** | ContractValue | Booked value at signing |
| `Status` | string | none | Status | **Mutable** lifecycle state |
| `ProjectManager` | string | none | ProjectManager | Delivery owner |

**Measures:**

| Measure | DAX | Meaning |
|---|---|---|
| `Total Contract Value` | `SUM(FactJobs[ContractValue])` | Booked/sold value of jobs in context — what customers agreed to pay, not what has been billed |

**Grain notes**

Rows are **updated in place** as a job progresses (three milestone dates plus a mutable
`Status`), so this is an accumulating snapshot, not a transaction fact. `ContractValue`
is one stable amount per job: safe to sum across dates, but double-counts under any
slicer that can repeat a job.

Schedule variance is computable per row as `ActualEndDate − ScheduledEndDate`.

**Dual role.** FactJobs carries a measure (so it is a fact) but also sits on the *one*
side of two relationships, serving as the dimension through which expenses and invoices
reach each other and the customer. `JobID` uniqueness in `silver_jobs` is therefore a
hard deployment requirement — duplicates will prevent the relationships in §6 from
being created.

---

## 3. FactExpenses

**Role:** Fact — **transaction**
**Grain:** One row per expense line
**Key:** `ExpenseID`
**DirectLake source:** `dbo.silver_expenses`
**Table lineage tag:** `fa515342-af77-4630-879c-b74ebb8b1c7a`

| Column | Data type | Summarize by | Source column | Notes |
|---|---|---|---|---|
| `ExpenseID` | string | none | ExpenseID | Primary key |
| `JobID` | string | none | JobID | FK → `FactJobs` |
| `ExpenseDate` | dateTime | none | ExpenseDate | Event date |
| `ExpenseType` | string | none | ExpenseType | Cost category |
| `ExpenseAmount` | double | **sum** | ExpenseAmount | Fully additive |
| `Vendor` | string | none | Vendor | Supplier |

**Measures:**

| Measure | DAX | Meaning |
|---|---|---|
| `Total Expenses` | `SUM(FactExpenses[ExpenseAmount])` | Direct cost recorded against jobs in context |

**Grain notes**

The purest fact in the model: immutable, append-only, one event with one date and one
additive amount. Fully additive across every dimension it touches. Grain is finer than
job, so many rows per `JobID` is expected.

---

## 4. FactInvoices

**Role:** Fact — **accumulating snapshot** (not a transaction fact)
**Grain:** One row per invoice
**Key:** `InvoiceID`
**DirectLake source:** `dbo.silver_invoices`
**Table lineage tag:** `59eff13a-80c0-4798-9a02-46adfec63634`

| Column | Data type | Summarize by | Source column | Notes |
|---|---|---|---|---|
| `InvoiceID` | string | none | InvoiceID | Primary key |
| `JobID` | string | none | JobID | FK → `FactJobs` |
| `InvoiceDate` | dateTime | none | InvoiceDate | Lifecycle 1 — issued |
| `DueDate` | dateTime | none | DueDate | Lifecycle 2 — due |
| `InvoiceAmount` | double | **sum** | InvoiceAmount | Fully additive |
| `PaymentStatus` | string | none | PaymentStatus | **Mutable** state |
| `PaymentDate` | dateTime | none | PaymentDate | Lifecycle 3 — settled |

**Measures:**

| Measure | DAX | Meaning |
|---|---|---|
| `Total Invoiced` | `SUM(FactInvoices[InvoiceAmount])` | Billed revenue. Counts every invoice **regardless of `PaymentStatus`** — money invoiced, not money collected |
| `Gross Margin $` | `[Total Invoiced] - [Total Expenses]` | Billed revenue minus recorded cost, in dollars |

**Grain notes**

Looks transactional but is not: `PaymentStatus` and `PaymentDate` are lifecycle fields
rewritten when an invoice settles, giving each row three dates across its life
(`InvoiceDate` → `DueDate` → `PaymentDate`). `InvoiceAmount` is additive; the payment
fields are state, not events.

There is **no line-item detail** — an invoice is atomic here, so revenue cannot be
decomposed below invoice level.

**`Gross Margin $` — two caveats**

1. **Cross-table dependency.** It is defined on FactInvoices but calls a measure on
   FactExpenses. This is valid DAX and is *correct* at job, customer, region and
   project-type level, because each SUM resolves independently in its own filter
   context — there is no fan trap. It breaks only when sliced by a column reaching
   just one of the two tables (`PaymentStatus`, `Vendor`, `ExpenseType`): one half of
   the subtraction responds to the filter, the other silently does not.
2. **Timing mismatch.** `Total Invoiced` moves on `InvoiceDate`; `Total Expenses`
   moves on `ExpenseDate`. Within any single month the two sides do not describe the
   same work, so month-by-month margin is noisy. Read it by job.

---

## 5. FactTargets ⚠️

**Role:** Fact — **periodic snapshot** (budget/plan)
**Grain:** One row per `Region` per `Month` (composite)
**Key:** none declared
**DirectLake source:** `dbo.silver_targets`
**Table lineage tag:** `63bc3501-7461-44b5-b44d-aeecb8e968f9`

| Column | Data type | Summarize by | Source column | Notes |
|---|---|---|---|---|
| `Month` | dateTime | none | Month | Grain part 1 — no join target |
| `Region` | string | none | Region | Grain part 2 — no join target |
| `RevenueTarget` | double | **sum** | RevenueTarget | Implicit aggregation only |
| `MarginTarget` | double | **sum** | MarginTarget | Implicit aggregation only |

**Measures:** none. `RevenueTarget` and `MarginTarget` aggregate implicitly when placed
on a visual, but no explicit measure exists for other DAX to reference.

### ⚠️ STRUCTURALLY ISOLATED — the model's central open issue

**FactTargets participates in zero relationships and has no available join path to any
other table.** It is not merely missing a relationship; the columns required to build
one do not exist anywhere that would connect.

| Blocker | Detail |
|---|---|
| No entity key | No `JobID`, no `CustomerID` — cannot reach the FactJobs hub at all |
| Region is unreachable | `Region` exists on `DimCustomer` only. Joining there is **many-to-many** (many target rows per region × many customers per region) and violates the single-direction many-to-one standard in §6 |
| No date dimension | The model has **no Date table**. `Month` has nothing to relate to |
| Grain mismatch | Region × Month is dramatically coarser than the per-job / per-line grain of every other fact |
| Type mismatch | `Month` is `dateTime` while `InvoiceDate` / `ExpenseDate` are full timestamps. Even with a Date table, `Month` must land exactly on month-start values to relate cleanly |

**Consequence.** Target-versus-actual reporting is **not currently possible**. Any visual
placing `RevenueTarget` beside `Total Invoiced` will either fail on ambiguity or repeat
the target value against every row. Related: no margin **percentage** measure exists to
compare against `MarginTarget`, and it is not yet established whether `MarginTarget` is
stored as a percentage or a dollar amount — this must be confirmed at source before any
comparison measure is written.

**Resolution requires two new conformed dimensions** (neither currently in the model):

1. **`DimDate`** — conformed to month grain, joined to `FactTargets[Month]`,
   `FactInvoices[InvoiceDate]`, `FactExpenses[ExpenseDate]` and the FactJobs milestone
   dates. Also unblocks all time intelligence, which is presently impossible across
   **six** dateTime columns in four tables.
2. **`DimRegion`** — a single-column region dimension that both `FactTargets[Region]`
   and `DimCustomer[Region]` join to, preserving many-to-one in both directions.

Until both exist, FactTargets should be treated as reference data and kept off any
visual that mixes it with actuals.

---

## 6. Relationships

All relationships are **many-to-one** with **single-direction** cross-filtering.
No bidirectional filtering and no many-to-many relationships are used or permitted.

| From (many) | To (one) | Key | Cardinality | Cross-filter | Active |
|---|---|---|---|---|---|
| `FactJobs[CustomerID]` | `DimCustomer[CustomerID]` | CustomerID | Many-to-one | Single | Yes |
| `FactExpenses[JobID]` | `FactJobs[JobID]` | JobID | Many-to-one | Single | Yes |
| `FactInvoices[JobID]` | `FactJobs[JobID]` | JobID | Many-to-one | Single | Yes |
| `FactTargets` | — | — | **none** | — | — |

```
                DimCustomer
              (silver_customers)
               [CustomerID] PK
                     ▲
                     │ many-to-one, single direction
                     │
                FactJobs  ◄─── hub
               (silver_jobs)
                [JobID] PK
                     ▲
          ┌──────────┴──────────┐
          │ many-to-one         │ many-to-one
          │ single direction    │ single direction
   FactExpenses              FactInvoices
 (silver_expenses)         (silver_invoices)


   FactTargets   ──✕──  NO JOIN PATH  (see §5)
 (silver_targets)
```

**Filter propagation.** Filters flow `DimCustomer → FactJobs → FactExpenses` and
`DimCustomer → FactJobs → FactInvoices`, chaining two one-to-many hops. Both reach all
the way down **without** bidirectional cross-filtering, so a customer, region, industry
or account-manager slicer correctly filters costs and revenue alike. Reverse propagation
does not occur by design: filtering by `ExpenseType` will **not** restrict invoices.

**Requirements for deployment.** `DimCustomer[CustomerID]` and `FactJobs[JobID]` must each
be unique in the source. All join columns are `string` on both sides — types are
consistent, which DirectLake requires.

---

## 7. Corrections to apply in TMDL

### 7.1 Rename `DImCustomer` → `DimCustomer`

The table is currently spelled with a capital `I`. The name is user-visible in every
report and DAX reference. Rename the table object only:

```tmdl
table DimCustomer
    lineageTag: a77c2d2e-92a8-4ce7-af56-4fde0590585c
    sourceLineageTag: [dbo].[silver_customers]
    ...
    partition DimCustomer = entity
        mode: directLake
        source
            entityName: silver_customers
            schemaName: dbo
            expressionSource: 'DirectLake - lh_operations_intelligence'

    changedProperty = Name
```

**Safe to rename:** `lineageTag` and `sourceLineageTag` are unchanged by a rename, the
DirectLake `entityName` still points at `silver_customers`, and **no existing measure
references this table** — all four measures reference FactJobs, FactExpenses or
FactInvoices only.

**Still requires attention:** the partition name, any relationship definitions in
`relationships.tmdl`, and any report-layer visual or bookmark that binds to the table by
name. Rename via a tool that updates these together where possible.

### 7.2 Format `Gross Margin $` as currency

The measure currently has **no** `formatString` and no `PBI_FormatHint`, unlike the other
three measures (which carry `{"isGeneralNumber":true}`). It therefore renders with default
formatting rather than as currency.

```tmdl
measure 'Gross Margin $' =

        [Total Invoiced] - [Total Expenses]
    formatString: \$#,0.00;(\$#,0.00);\$#,0.00
    lineageTag: 3b8696dd-45bc-42b8-b2d2-17abe39a95e5

    changedProperty = Name

    annotation PBI_FormatHint = {"currencyCulture":"en-US"}
```

The format string renders negatives in parentheses — appropriate for a margin, which can
legitimately go negative.

> **Assumption to confirm:** `en-US` / `$` is assumed as the currency culture. Adjust
> `currencyCulture` and the symbol if the reporting currency differs.

**Consistency note.** `Total Invoiced`, `Total Expenses` and `Total Contract Value` are all
dollar amounts currently annotated `{"isGeneralNumber":true}`. Applying the same currency
format to those three is recommended for a consistent report surface, but is outside the
scope of this correction and is **not** reflected in this document.

---

## 8. Open issues

| # | Issue | Severity | Resolution |
|---|---|---|---|
| 1 | **FactTargets is structurally isolated** — no join path to any table | **Critical** | Add `DimDate` and `DimRegion` conformed dimensions (§5) |
| 2 | No Date dimension — 6 dateTime columns across 4 tables, none related | **High** | Add `DimDate`; unblocks issue 1 and all time intelligence |
| 3 | `MarginTarget` units unverified — percentage or dollars? | **High** | Confirm at source before writing any comparison measure |
| 4 | No margin percentage measure exists | Medium | `DIVIDE([Gross Margin $], [Total Invoiced])` once issue 3 is resolved |
| 5 | FactTargets has no explicit measures | Medium | Add explicit measures so other DAX can reference the targets |
| 6 | `Gross Margin $` misreads when sliced by single-table columns | Medium | Document for report authors; consider a guard using `ISFILTERED` |
| 7 | `Total Invoiced` counts unpaid invoices | Low — by design | Add a separate collected-revenue measure filtered on `PaymentStatus` if needed |
| 8 | Dollar measures inconsistently formatted (§7.2) | Low | Apply currency format to the other three measures |

---

## Appendix — DirectLake source map

| Table | Entity | Schema | Mode | Expression source |
|---|---|---|---|---|
| `DimCustomer` | `silver_customers` | `dbo` | directLake | `DirectLake - lh_operations_intelligence` |
| `FactJobs` | `silver_jobs` | `dbo` | directLake | `DirectLake - lh_operations_intelligence` |
| `FactExpenses` | `silver_expenses` | `dbo` | directLake | `DirectLake - lh_operations_intelligence` |
| `FactInvoices` | `silver_invoices` | `dbo` | directLake | `DirectLake - lh_operations_intelligence` |
| `FactTargets` | `silver_targets` | `dbo` | directLake | `DirectLake - lh_operations_intelligence` |

All five tables read from the same lakehouse and the same silver layer. Every table
carries `changedProperty = Name`, indicating each was renamed from its source entity
name during modelling.
