"""Check that every number in an answer came from the data.

The guardrails protect the SQL. Nothing protects the prose, and the failure mode
seen repeatedly is narrative arithmetic: a query returns the 33 late jobs, and the
answer says "33 of 44 finished late". The 33 is real. The 44 was never retrieved.

This finds numbers stated in prose that the returned rows cannot support.

It is deliberately CONSERVATIVE about what counts as supported -- cell values,
row counts, column aggregates, numbers from the question, the declared known
issues, and percentages of any two of those. Anything else is reported for a
human to judge, with the offending number named. It can therefore flag a number
that was in fact derivable in a way not modelled here; it reports rather than
concludes.

Declared known issues count because they are in the system prompt precisely so the
agent can caveat its answers with them. When the model says that 16,882.98 of cost
cannot be attributed to any job, it is quoting the declaration, not inventing a
figure, and rule 6 of the system prompt requires it to.

Table row counts are NOT in that set. "All 52 known jobs" is declared too, but it
was used as a denominator for a claim about a result, which is the failure this
check exists to find. The line is what the figure is used FOR: a known issue is
quoted as a caveat, a row count gets computed into an assertion.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

MODEL_PATH = Path(__file__).resolve().parents[1] / "model" / "model.yaml"

# 1,234 | 1234.5 | $4,497,901.88 | 45% | -12
#
# The lookbehind keeps identifiers out. Job numbers look like J-202551, and without
# it that reads as the number -202551, which then fails to match anything in the
# data and is reported as an invented figure. Anything glued to a letter, a dot or
# a hyphen is part of a token, not a quantity.
NUMBER = re.compile(r"(?<![A-Za-z0-9._-])-?\$?\d[\d,]*(?:\.\d+)?%?(?!\.\d)")

# Scales the prose may restate a value at: "4.5 million" for 4497901.88.
SCALES = (Decimal(1), Decimal(1_000), Decimal(1_000_000), Decimal(1_000_000_000))


@dataclass
class Grounding:
    numbers: list[Decimal] = field(default_factory=list)
    ungrounded: list[Decimal] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.ungrounded

    @property
    def grounded(self) -> list[Decimal]:
        missing = set(self.ungrounded)
        return [n for n in self.numbers if n not in missing]

    def summary(self) -> str:
        if not self.numbers:
            return "no numbers stated"
        if self.ok:
            return f"all {len(self.numbers)} numbers grounded"
        listed = ", ".join(_plain(n) for n in self.ungrounded)
        return f"{len(self.ungrounded)} of {len(self.numbers)} not in the result: {listed}"


# Permissive on purpose: this reads declarations, not prose. "(53-71)" must yield
# both endpoints, and a stray match only ever removes a false positive.
DECLARED_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _walk(node: Any):
    """Every scalar in a nested YAML structure."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk(value)
    else:
        yield node


@functools.lru_cache(maxsize=4)
def declared_figures(model_path: Path = MODEL_PATH) -> frozenset[Decimal]:
    """Figures the model declares as known issues.

    These reach the agent through the system prompt, so quoting one is reading the
    declaration rather than inventing a number. Scoped to the known-issues section
    (`data_quality` in model.yaml) and nothing else.
    """
    model = yaml.safe_load(Path(model_path).read_text())
    found: set[Decimal] = set()
    for scalar in _walk(model.get("data_quality", [])):
        for token in DECLARED_NUMBER.findall(str(scalar)):
            value = _to_decimal(token)
            if value is not None:
                found.add(value)
    return frozenset(found)


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _to_decimal(token: str) -> Decimal | None:
    cleaned = token.replace(",", "").replace("$", "").rstrip("%")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def extract_numbers(text: str) -> list[Decimal]:
    """Every number stated in the text, in order, duplicates kept."""
    found = []
    for token in NUMBER.findall(text or ""):
        value = _to_decimal(token)
        if value is not None:
            found.append(value)
    return found


def _numeric(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return None


def _values_from_rows(columns: Sequence[str], rows: Sequence[Sequence[Any]]):
    """Every number the result contains, plus the aggregates over it.

    Numbers inside string cells count too: a date renders as "2026-07-31", and an
    answer that names the year is quoting the data, not inventing it.
    """
    cells: set[Decimal] = set()
    aggregates: set[Decimal] = set()

    cells.add(Decimal(len(rows)))            # the row count itself
    aggregates.add(Decimal(len(rows)))

    for index in range(len(columns)):
        column = []
        for row in rows:
            if index >= len(row):
                continue
            value = row[index]
            number = _numeric(value)
            if number is not None:
                cells.add(number)
                column.append(number)
            elif isinstance(value, str):
                for token in NUMBER.findall(value):
                    inner = _to_decimal(token)
                    if inner is not None:
                        cells.add(inner)
        if column:
            aggregates.update({sum(column), min(column), max(column), Decimal(len(column))})

    return cells, aggregates


def _matches(stated: Decimal, supported: Iterable[Decimal]) -> bool:
    """True when `stated` equals a supported value, at any plausible scale."""
    for value in supported:
        for scale in SCALES:
            scaled = value / scale
            if stated == scaled:
                return True
            # Allow the prose to round: "$4.5 million", "23.1%".
            for places in (Decimal("0.1"), Decimal("0.01")):
                try:
                    if scaled.quantize(places) == stated.quantize(places):
                        return True
                except InvalidOperation:
                    continue
    return False


def check_answer(
    answer: str,
    tool_calls: Sequence[Any],
    question: str = "",
    declared: Iterable[Decimal] | None = None,
) -> Grounding:
    """Find numbers in `answer` that neither the results nor the model declare."""
    cells: set[Decimal] = set()
    aggregates: set[Decimal] = set()

    for call in tool_calls:
        if not getattr(call, "ok", False):
            continue
        call_cells, call_aggregates = _values_from_rows(call.columns, call.rows)
        cells |= call_cells
        aggregates |= call_aggregates

    asked = set(extract_numbers(question))
    known = set(declared_figures() if declared is None else declared)
    supported = cells | aggregates | asked | known

    # A percentage may be the ratio of two salient figures rather than a stored
    # value. Only aggregates and question numbers are paired, which keeps this to
    # a handful of candidates instead of every cell against every other cell.
    salient = sorted(aggregates | asked)
    ratios: set[Decimal] = set()
    for numerator in salient:
        for denominator in salient:
            if denominator and denominator != 0:
                try:
                    ratios.add((numerator / denominator * 100).quantize(Decimal("0.1")))
                except (InvalidOperation, ZeroDivisionError):
                    continue

    stated = extract_numbers(answer)
    ungrounded = [
        number
        for number in stated
        if not _matches(number, supported) and not _matches(number, ratios)
    ]
    return Grounding(numbers=stated, ungrounded=ungrounded)
