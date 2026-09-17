"""RETIRED -- superseded by the real exports in data/silver. Do not run this
to produce app data.

This targets the WRONG MODEL: the five-table `lh_operations_intelligence` model,
not the seven-table `lh_job_costing` star schema the app is built against. It is
kept only as a record of how the project was unblocked before real exports
existed. See docs/decisions.md.

Its output now goes to data/generated (gitignored), not data/silver, so running
it cannot overwrite a real export.

Generate fixture Parquet exports of the five silver tables.

These rows are SYNTHETIC. They stand in for real exports of the
`lh_operations_intelligence` lakehouse until those are available, and exist so the
rest of the app can be built and demonstrated. Every number here is made up.

The generator is seeded, so repeated runs produce byte-identical output. Column
names, types and referential integrity follow docs/data-dictionary.md:

    silver_customers  -> DimCustomer    one row per customer
    silver_jobs       -> FactJobs       one row per job          (hub)
    silver_expenses   -> FactExpenses   one row per expense line
    silver_invoices   -> FactInvoices   one row per invoice
    silver_targets    -> FactTargets    one row per Region x Month

Usage:  python scripts/generate_fixtures.py [--out data/silver]
"""

from __future__ import annotations

import argparse
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SEED = 42

# The fixture's "current date". Invoice ageing and job completion are relative to
# this rather than to the real clock, so runs stay reproducible.
AS_OF = datetime(2025, 12, 31)

WINDOW_START = datetime(2024, 1, 1)
WINDOW_END = datetime(2025, 9, 30)

N_CUSTOMERS = 40
N_JOBS = 180

REGIONS = ["North", "South", "East", "West", "Central"]
INDUSTRIES = [
    "Manufacturing",
    "Healthcare",
    "Financial Services",
    "Retail",
    "Public Sector",
    "Energy",
]
ACCOUNT_MANAGERS = [
    "A. Okafor",
    "B. Lindqvist",
    "C. Moreau",
    "D. Sharma",
    "E. Tanaka",
    "F. Byrne",
]
PROJECT_MANAGERS = [
    "G. Alvarez",
    "H. Novak",
    "I. Fitzgerald",
    "J. Mwangi",
    "K. Petrov",
    "L. Haddad",
    "M. Sorensen",
    "N. Delgado",
]
PROJECT_TYPES = [
    "Implementation",
    "Managed Service",
    "Consulting",
    "Upgrade",
    "Support Retainer",
]
EXPENSE_TYPES = [
    "Labour",
    "Subcontractor",
    "Materials",
    "Travel",
    "Equipment Hire",
    "Software Licence",
]
VENDORS = [
    "Arclight Supply Co",
    "Benton Contracting",
    "Cobalt Logistics",
    "Drayton Plant Hire",
    "Everline Software",
    "Fairhaven Materials",
    "Granite Field Services",
    "Harlow Staffing",
    "Ironvale Fabrication",
    "Juniper Travel Group",
]
CUSTOMER_PREFIX = [
    "Ashford", "Brightwater", "Calder", "Dunmore", "Eastgate", "Foxley",
    "Grayling", "Hollowell", "Inchcape", "Jarrow", "Kelsey", "Langmere",
    "Mossbank", "Northrop", "Oakhurst", "Pennington", "Quarry Hill", "Redmayne",
    "Stonebridge", "Thornbury", "Ullswater", "Vexley", "Westmount", "Yarrow",
    "Alderton", "Belvoir", "Crossfield", "Deepdale", "Elmsworth", "Fenwick",
    "Garnock", "Hartley", "Ingleby", "Kirkstall", "Linsdale", "Marchmont",
    "Netherby", "Orrell", "Padstow", "Rushmere",
]
CUSTOMER_SUFFIX = ["Group", "Industries", "Holdings", "Partners", "Systems", "Works"]
JOB_NOUN = [
    "Plant Upgrade", "Network Refresh", "Site Fit-Out", "ERP Rollout",
    "Compliance Audit", "Depot Expansion", "Line Automation", "Data Migration",
    "Safety Retrofit", "Warehouse Build", "Fleet Telematics", "Control Room Rebuild",
]


def _rand_datetime(rng: random.Random, start: datetime, end: datetime) -> datetime:
    """A random midnight-aligned datetime in [start, end]."""
    span = (end - start).days
    return start + timedelta(days=rng.randint(0, max(span, 0)))


def _money(value: float) -> float:
    return round(value, 2)


def build_customers(rng: random.Random) -> list[dict]:
    rows = []
    for i in range(N_CUSTOMERS):
        name = f"{CUSTOMER_PREFIX[i]} {rng.choice(CUSTOMER_SUFFIX)}"
        rows.append(
            {
                "CustomerID": f"CUST-{i + 1:04d}",
                "CustomerName": name,
                "Industry": rng.choice(INDUSTRIES),
                "Region": rng.choice(REGIONS),
                "AccountManager": rng.choice(ACCOUNT_MANAGERS),
                "CustomerSince": _rand_datetime(
                    rng, datetime(2015, 1, 1), datetime(2024, 6, 30)
                ),
                # Weighted so most customers are active.
                "CustomerStatus": rng.choices(
                    ["Active", "Dormant"], weights=[0.85, 0.15]
                )[0],
            }
        )
    return rows


def build_jobs(rng: random.Random, customers: list[dict]) -> list[dict]:
    rows = []
    for i in range(N_JOBS):
        customer = rng.choice(customers)
        start = _rand_datetime(rng, WINDOW_START, WINDOW_END)
        scheduled_end = start + timedelta(days=rng.randint(30, 270))
        status = rng.choices(
            ["Completed", "In Progress", "On Hold", "Cancelled"],
            weights=[0.55, 0.28, 0.09, 0.08],
        )[0]

        # Only completed jobs have an actual end date. Schedule variance runs from
        # three weeks early to six weeks late, skewed late.
        actual_end = None
        if status == "Completed":
            actual_end = scheduled_end + timedelta(days=rng.randint(-21, 45))
            if actual_end > AS_OF:
                actual_end = AS_OF

        # How far through delivery the job is. Cost and billing both accrue against
        # this, so work in flight is not costed as if it were finished. Private to
        # the generator: the Arrow schema selects columns by name, so it is not
        # written out, and FactJobs has no such column.
        progress = 1.0 if status == "Completed" else {
            "In Progress": rng.uniform(0.25, 0.85),
            "On Hold": rng.uniform(0.15, 0.60),
            "Cancelled": rng.uniform(0.05, 0.35),
        }[status]

        rows.append(
            {
                "_progress": progress,
                "JobID": f"JOB-{i + 1:05d}",
                "CustomerID": customer["CustomerID"],
                "JobName": f"{customer['CustomerName'].split()[0]} {rng.choice(JOB_NOUN)}",
                "ProjectType": rng.choice(PROJECT_TYPES),
                "StartDate": start,
                "ScheduledEndDate": scheduled_end,
                "ActualEndDate": actual_end,
                "ContractValue": _money(rng.uniform(25_000, 900_000)),
                "Status": status,
                "ProjectManager": rng.choice(PROJECT_MANAGERS),
            }
        )
    return rows


def build_expenses(rng: random.Random, jobs: list[dict]) -> list[dict]:
    rows = []
    seq = 0
    for job in jobs:
        # Cancelled jobs stop accruing cost early, so they carry fewer lines.
        n_lines = rng.randint(2, 8) if job["Status"] == "Cancelled" else rng.randint(5, 30)

        # Cost to deliver the whole job as a share of contract value, then scaled to
        # the share delivered so far. The upper bound exceeds the billed share, so
        # some jobs legitimately run at a loss.
        cost_ratio = rng.uniform(0.55, 0.92)
        total_cost = job["ContractValue"] * cost_ratio * job["_progress"]

        # Weights split the total unevenly across lines.
        weights = [rng.uniform(0.4, 1.6) for _ in range(n_lines)]
        weight_sum = sum(weights)

        cost_end = min(job["ActualEndDate"] or job["ScheduledEndDate"], AS_OF)
        if cost_end < job["StartDate"]:
            cost_end = job["StartDate"]

        for weight in weights:
            seq += 1
            rows.append(
                {
                    "ExpenseID": f"EXP-{seq:06d}",
                    "JobID": job["JobID"],
                    "ExpenseDate": _rand_datetime(rng, job["StartDate"], cost_end),
                    "ExpenseType": rng.choice(EXPENSE_TYPES),
                    "ExpenseAmount": _money(total_cost * weight / weight_sum),
                    "Vendor": rng.choice(VENDORS),
                }
            )
    return rows


def build_invoices(rng: random.Random, jobs: list[dict]) -> list[dict]:
    rows = []
    seq = 0
    for job in jobs:
        # Billing tracks delivery, but leads or lags it a little -- so billed revenue
        # and recorded cost do not line up month for month. That mismatch is real:
        # see the Gross Margin timing caveat in the data dictionary.
        billed_share = job["_progress"] * rng.uniform(0.88, 1.02)
        total_billed = job["ContractValue"] * min(billed_share, 1.0)

        n_invoices = 1 if job["Status"] == "Cancelled" else rng.randint(1, 6)
        weights = [rng.uniform(0.6, 1.4) for _ in range(n_invoices)]
        weight_sum = sum(weights)

        bill_end = min(job["ActualEndDate"] or job["ScheduledEndDate"], AS_OF)
        if bill_end < job["StartDate"]:
            bill_end = job["StartDate"]

        for weight in weights:
            seq += 1
            invoice_date = _rand_datetime(rng, job["StartDate"], bill_end)
            due_date = invoice_date + timedelta(days=30)

            # PaymentStatus and PaymentDate are lifecycle state, rewritten when an
            # invoice settles -- see the FactInvoices grain notes in the dictionary.
            if due_date > AS_OF:
                payment_status = rng.choices(
                    ["Outstanding", "Paid"], weights=[0.75, 0.25]
                )[0]
            else:
                payment_status = rng.choices(
                    ["Paid", "Overdue", "Outstanding"], weights=[0.78, 0.14, 0.08]
                )[0]

            payment_date = None
            if payment_status == "Paid":
                latest = min(due_date + timedelta(days=25), AS_OF)
                if latest < invoice_date:
                    latest = invoice_date
                payment_date = _rand_datetime(rng, invoice_date, latest)

            rows.append(
                {
                    "InvoiceID": f"INV-{seq:06d}",
                    "JobID": job["JobID"],
                    "InvoiceDate": invoice_date,
                    "DueDate": due_date,
                    "InvoiceAmount": _money(total_billed * weight / weight_sum),
                    "PaymentStatus": payment_status,
                    "PaymentDate": payment_date,
                }
            )
    return rows


def build_targets(rng: random.Random) -> list[dict]:
    """One row per Region x Month across 2024-2025.

    MarginTarget is written in DOLLARS. The dictionary records the units as
    unverified (open issue #3); the fixture has to pick one, so this records which.
    """
    rows = []
    for year in (2024, 2025):
        for month in range(1, 13):
            for region in REGIONS:
                # Scaled to sit plausibly alongside actual invoiced revenue
                # (~500k per region-month), so the numbers bear eyeballing.
                revenue_target = _money(rng.uniform(350_000, 900_000))
                rows.append(
                    {
                        "Month": datetime(year, month, 1),
                        "Region": region,
                        "RevenueTarget": revenue_target,
                        "MarginTarget": _money(revenue_target * rng.uniform(0.18, 0.34)),
                    }
                )
    return rows


# Explicit Arrow schemas: string -> VARCHAR, dateTime -> TIMESTAMP, double -> DOUBLE,
# matching the data types in docs/data-dictionary.md.
_TS = pa.timestamp("us")
SCHEMAS: dict[str, pa.Schema] = {
    "silver_customers": pa.schema(
        [
            ("CustomerID", pa.string()),
            ("CustomerName", pa.string()),
            ("Industry", pa.string()),
            ("Region", pa.string()),
            ("AccountManager", pa.string()),
            ("CustomerSince", _TS),
            ("CustomerStatus", pa.string()),
        ]
    ),
    "silver_jobs": pa.schema(
        [
            ("JobID", pa.string()),
            ("CustomerID", pa.string()),
            ("JobName", pa.string()),
            ("ProjectType", pa.string()),
            ("StartDate", _TS),
            ("ScheduledEndDate", _TS),
            ("ActualEndDate", _TS),
            ("ContractValue", pa.float64()),
            ("Status", pa.string()),
            ("ProjectManager", pa.string()),
        ]
    ),
    "silver_expenses": pa.schema(
        [
            ("ExpenseID", pa.string()),
            ("JobID", pa.string()),
            ("ExpenseDate", _TS),
            ("ExpenseType", pa.string()),
            ("ExpenseAmount", pa.float64()),
            ("Vendor", pa.string()),
        ]
    ),
    "silver_invoices": pa.schema(
        [
            ("InvoiceID", pa.string()),
            ("JobID", pa.string()),
            ("InvoiceDate", _TS),
            ("DueDate", _TS),
            ("InvoiceAmount", pa.float64()),
            ("PaymentStatus", pa.string()),
            ("PaymentDate", _TS),
        ]
    ),
    "silver_targets": pa.schema(
        [
            ("Month", _TS),
            ("Region", pa.string()),
            ("RevenueTarget", pa.float64()),
            ("MarginTarget", pa.float64()),
        ]
    ),
}


def check_integrity(tables: dict[str, list[dict]]) -> None:
    """Fail loudly if the fixtures violate the model's deployment requirements."""
    customer_ids = [r["CustomerID"] for r in tables["silver_customers"]]
    job_ids = [r["JobID"] for r in tables["silver_jobs"]]

    # DimCustomer[CustomerID] and FactJobs[JobID] must each be unique in the source,
    # or the relationships in dictionary section 6 cannot be created.
    assert len(customer_ids) == len(set(customer_ids)), "CustomerID is not unique"
    assert len(job_ids) == len(set(job_ids)), "JobID is not unique"

    customer_set, job_set = set(customer_ids), set(job_ids)
    assert {r["CustomerID"] for r in tables["silver_jobs"]} <= customer_set, (
        "FactJobs has a CustomerID with no matching customer"
    )
    for entity in ("silver_expenses", "silver_invoices"):
        assert {r["JobID"] for r in tables[entity]} <= job_set, (
            f"{entity} has a JobID with no matching job"
        )

    target_keys = [(r["Region"], r["Month"]) for r in tables["silver_targets"]]
    assert len(target_keys) == len(set(target_keys)), "FactTargets Region x Month repeats"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "generated",
        help="directory to write the Parquet files into",
    )
    args = parser.parse_args()

    rng = random.Random(SEED)
    customers = build_customers(rng)
    jobs = build_jobs(rng, customers)
    tables = {
        "silver_customers": customers,
        "silver_jobs": jobs,
        "silver_expenses": build_expenses(rng, jobs),
        "silver_invoices": build_invoices(rng, jobs),
        "silver_targets": build_targets(rng),
    }
    check_integrity(tables)

    args.out.mkdir(parents=True, exist_ok=True)
    for entity, rows in tables.items():
        schema = SCHEMAS[entity]
        columns = {name: [r[name] for r in rows] for name in schema.names}
        table = pa.Table.from_pydict(columns, schema=schema)
        path = args.out / f"{entity}.parquet"
        pq.write_table(table, path, compression="zstd")
        print(f"wrote {path.name:<26} {table.num_rows:>7,} rows")


if __name__ == "__main__":
    main()
