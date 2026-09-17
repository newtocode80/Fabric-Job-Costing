#!/usr/bin/env python3
"""Run the eval set and print a pass/fail table.

    python evals/run.py                  # every case
    python evals/run.py --case refusal   # cases whose id or category matches
    python evals/run.py --dry-run        # validate the file, no model calls

Numeric groundedness is checked on every case, not declared per case: every number
in the prose must be present in, or derivable from, the rows the query returned.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobcosting.agent import Agent, Answer  # noqa: E402
from jobcosting.engine import DuckDBEngine  # noqa: E402
from jobcosting.grounding import check_answer, extract_numbers  # noqa: E402

QUESTIONS = Path(__file__).resolve().parent / "questions.yaml"

# Phrases that mark a genuine refusal or a genuine clarifying question, rather than
# an answer that merely hedges.
REFUSAL_MARKERS = ("cannot", "can't", "no ", "not available", "would need", "missing")
MONEY = re.compile(r"\$\s?\d")


@dataclass
class CaseResult:
    id: str
    category: str
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str | None = None
    tool_calls: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed and not self.error

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        (self.passed if condition else self.failed).append(
            name if condition or not detail else f"{name} ({detail})"
        )


def scalar(engine: DuckDBEngine, sql: str) -> Decimal | None:
    rows = engine.run(sql).rows
    if not rows or rows[0][0] is None:
        return None
    value = rows[0][0]
    return value if isinstance(value, Decimal) else Decimal(str(value))


def states(answer: str, value: Decimal) -> bool:
    """Is `value` one of the numbers the answer states?"""
    return any(
        n == value or n.quantize(Decimal("0.01")) == value.quantize(Decimal("0.01"))
        for n in extract_numbers(answer)
    )


def evaluate(case: dict, answer: Answer, engine: DuckDBEngine) -> CaseResult:
    result = CaseResult(id=case["id"], category=case["category"])
    result.tool_calls = len(answer.tool_calls)
    prose = (answer.answer or "").lower()
    all_sql = " ".join((c.executed_sql or c.sql) for c in answer.tool_calls).lower()

    if answer.error:
        result.error = answer.error
        return result

    # --- universal: every number in the prose must come from the data ---------
    grounding = check_answer(answer.answer or "", answer.tool_calls, case["question"])
    result.check("numbers grounded", grounding.ok, grounding.summary())

    # --- budget -------------------------------------------------------------
    limit = case.get("max_tool_calls")
    if limit is not None:
        result.check("tool calls", result.tool_calls <= limit,
                     f"{result.tool_calls} > {limit}")

    # --- behaviour ----------------------------------------------------------
    behaviour = case["behaviour"]
    if behaviour == "refuse":
        result.check("refuses", any(m in prose for m in REFUSAL_MARKERS),
                     "no refusal language")
        result.check("no invented figure", not (answer.rows and states(answer.answer, Decimal(0))),
                     "")
    elif behaviour == "clarify":
        result.check("asks a question", "?" in (answer.answer or ""), "no question asked")
        result.check("does not guess", not grounding.numbers or grounding.ok,
                     "stated figures while asking")
    else:
        result.check("answers", bool(prose.strip()), "empty answer")
        result.check("ran a query", result.tool_calls >= 1, "no SQL run")

    if case.get("states_no_money"):
        result.check("states no money figure", not MONEY.search(answer.answer or ""),
                     "quoted a figure it should not have")

    # --- SQL shape ----------------------------------------------------------
    for table in case.get("sql_references", []):
        result.check(f"sql uses {table}", table.lower() in all_sql, "not referenced")
    for table in case.get("sql_excludes", []):
        result.check(f"sql avoids {table}", table.lower() not in all_sql, "referenced")

    bounds = case.get("expect_rows")
    if bounds and answer.rows is not None and behaviour == "answer":
        count = len(answer.rows)
        result.check("row count", bounds["min"] <= count <= bounds["max"],
                     f"{count} not in {bounds['min']}..{bounds['max']}")

    # --- content ------------------------------------------------------------
    for phrase in case.get("mentions", []):
        result.check(f"mentions {phrase!r}", phrase.lower() in prose, "absent")
    for phrase in case.get("avoids", []):
        result.check(f"avoids {phrase!r}", phrase.lower() not in prose, "present")

    # --- ground truth -------------------------------------------------------
    if case.get("expect_value_sql"):
        expected = scalar(engine, case["expect_value_sql"])
        result.check(f"states {expected}", expected is not None and states(answer.answer, expected),
                     "correct figure not stated")
    if case.get("avoids_value_sql"):
        wrong = scalar(engine, case["avoids_value_sql"])
        result.check(f"avoids wrong figure {wrong}",
                     wrong is None or not states(answer.answer, wrong),
                     "stated the figure the wrong query produces")
    return result


def print_table(results: list[CaseResult]) -> None:
    width = max(len(r.id) for r in results)
    print()
    print(f"{'CASE':<{width}}  {'CATEGORY':<17} {'CALLS':>5}  {'RESULT':<6}  CHECKS")
    print("-" * (width + 60))
    for r in results:
        verdict = "ERROR" if r.error else ("pass" if r.ok else "FAIL")
        detail = r.error if r.error else f"{len(r.passed)}/{len(r.passed) + len(r.failed)}"
        print(f"{r.id:<{width}}  {r.category:<17} {r.tool_calls:>5}  {verdict:<6}  {detail}")
        for failure in r.failed:
            print(f"{'':<{width}}  {'':<17} {'':>5}          x {failure}")
    print("-" * (width + 60))
    passed = sum(1 for r in results if r.ok)
    print(f"{passed}/{len(results)} passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="only ids or categories containing this text")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate the file and the ground-truth SQL; call no model")
    args = parser.parse_args()

    cases = yaml.safe_load(QUESTIONS.read_text())["cases"]
    if args.case:
        needle = args.case.lower()
        cases = [c for c in cases if needle in c["id"].lower() or needle in c["category"].lower()]
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 1

    engine = DuckDBEngine()

    if args.dry_run:
        print(f"{'CASE':<28} {'CATEGORY':<17} {'BEHAVIOUR':<9} GROUND TRUTH")
        print("-" * 96)
        for case in cases:
            truth = ""
            for key, prefix in (("expect_value_sql", "expects"), ("avoids_value_sql", "avoids")):
                if case.get(key):
                    truth += f"{prefix} {scalar(engine, case[key])}  "
            print(f"{case['id']:<28} {case['category']:<17} {case['behaviour']:<9} {truth.strip()}")
        print("-" * 96)
        print(f"{len(cases)} cases validated; every ground-truth query ran.")
        return 0

    agent = Agent(engine=engine)
    results = []
    for case in cases:
        print(f"  asking: {case['question']}", file=sys.stderr)
        results.append(evaluate(case, agent.ask(case["question"]), engine))

    print_table(results)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
