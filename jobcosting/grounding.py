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
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

MODEL_PATH = Path(__file__).resolve().parents[1] / "model" / "model.yaml"
SCHEMA_CONTEXT_PATH = Path(__file__).resolve().parents[1] / "model" / "schema_context.md"

# 1,234 | 1234.5 | $4,497,901.88 | 45% | -12
#
# The lookbehind keeps identifiers out. Job numbers look like J-202551, and without
# it that reads as the number -202551, which then fails to match anything in the
# data and is reported as an invented figure. Anything glued to a letter, a dot or
# a hyphen is part of a token, not a quantity. The first trailing lookahead does
# the same for dotted forms such as 2.1.3; the second drops ordinals, because the
# "1" in "1st quarter" is not a figure about the data either. (?!\d) stops the
# engine backtracking to a shorter prefix to dodge that check -- without it "23rd"
# fails as "23" and then succeeds as "2".
NUMBER = re.compile(
    r"(?<![A-Za-z0-9._-])-?\$?\d[\d,]*(?:\.\d+)?%?(?!\.\d)(?!\d)(?!(?:st|nd|rd|th)\b)"
)

# "8. J-202551 - $232,248 over" -- the 8 numbers the point, it is not a quantity.
# An answer that lists its findings was having its own list markers reported as
# invented figures. Only a marker at the start of a line, followed by "." or ")"
# and a space, counts -- so a sentence that legitimately opens with a number, such
# as "30 cost rows are orphaned", keeps it.
LIST_MARKER = re.compile(r"^[ \t]*\d+[.)][ \t]+", re.MULTILINE)

# Scales the prose may restate a value at: "4.5 million" for 4497901.88.
SCALES = (Decimal(1), Decimal(1_000), Decimal(1_000_000), Decimal(1_000_000_000))


@dataclass
class Grounding:
    """What the prose claimed, and which of it the data does not support.

    Flagged numbers carry a severity, because two different things get caught:

      FAIL  -- the number appears nowhere. Nothing in the data or the prompt says
               it. "15-39 days late" when no job is 39 days late.
      WARN  -- the number is one the prompt instructs the model to cite -- a known
               issue, a refusal script, a mandatory caveat, a column description --
               but it is not in this result. "across all 52 jobs" is true and the
               model read it there. Quoting and computing look identical to a
               checker, so the conservative call is to surface it and let a person
               decide.
    """

    numbers: list[Decimal] = field(default_factory=list)
    ungrounded: list[Decimal] = field(default_factory=list)
    warnings: list[Decimal] = field(default_factory=list)

    @property
    def failures(self) -> list[Decimal]:
        warned = set(self.warnings)
        return [n for n in self.ungrounded if n not in warned]

    @property
    def ok(self) -> bool:
        """True when nothing is unaccounted for. Warnings do not fail a case."""
        return not self.failures

    @property
    def grounded(self) -> list[Decimal]:
        missing = set(self.ungrounded)
        return [n for n in self.numbers if n not in missing]

    def summary(self) -> str:
        if not self.numbers:
            return "no numbers stated"
        if not self.ungrounded:
            return f"all {len(self.numbers)} numbers grounded"
        parts = []
        if self.failures:
            parts.append(f"{len(self.failures)} of {len(self.numbers)} in neither the "
                         f"result nor the prompt: {', '.join(_plain(n) for n in self.failures)}")
        if self.warnings:
            parts.append(f"{len(self.warnings)} from the prompt, not this result: "
                         f"{', '.join(_plain(n) for n in self.warnings)}")
        return "; ".join(parts)


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
def citable_figures(model_path: Path = MODEL_PATH) -> frozenset[Decimal]:
    """Figures the model is instructed to cite as context.

    Used ONLY to grade severity, never to ground.

    Scoped to the parts of the declaration the model is told to quote from: the
    known issues, the refusal scripts, the mandatory caveats on a calculation, and
    the column descriptions. Quoting one of those is reading an instruction.

    Deliberately NOT every number in the rendered prompt. Relationship evidence is
    excluded, and that exclusion is the point: the prompt says "keeps 44 of 52
    jobs" about a completely unrelated join, and counting that would downgrade
    "33 of 44 jobs finished late" -- the canonical failure -- to a warning. A
    coincidence in a join warning must not excuse invented arithmetic.
    """
    model = yaml.safe_load(Path(model_path).read_text())
    cited: list[Any] = [
        model.get("data_quality", []),
        model.get("cannot_answer", []),
        [pattern.get("required_caveats", []) for pattern in model.get("analysis_patterns", [])],
        [
            [column.get("desc", "") for column in table.get("columns", {}).values()]
            for table in model.get("tables", [])
        ],
    ]
    found: set[Decimal] = set()
    for scalar in _walk(cited):
        for token in DECLARED_NUMBER.findall(str(scalar)):
            value = _to_decimal(token)
            if value is not None:
                found.add(value)
    return frozenset(found)


@functools.lru_cache(maxsize=4)
def declared_figures(model_path: Path = MODEL_PATH) -> frozenset[Decimal]:
    """Figures the model declares as known issues.

    These reach the agent through the system prompt, so quoting one is reading the
    declaration rather than inventing a number.

    Two sections count. The known issues (`data_quality`), and `cannot_answer` --
    the second is not a judgement call, because it holds figures the prompt
    explicitly instructs the model to state when declining, such as "4,637 of
    10,365 cost rows". Flagging a model for following its own instructions is
    nonsense.

    Table row counts and column descriptions are NOT included. That boundary is
    what keeps "52 total minus the 8 above = 44 assessable" a failure.
    """
    model = yaml.safe_load(Path(model_path).read_text())
    found: set[Decimal] = set()
    sections = [model.get("data_quality", []), model.get("cannot_answer", [])]
    for scalar in _walk(sections):
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
    """Every number stated in the text, in order, duplicates kept.

    List markers and ordinals are not numbers stated about the data.
    """
    # U+2212 is a minus sign, and prose uses it. Without this, "-549,091.45"
    # extracts as positive and fails to match the negative value in the result.
    # En and em dashes are left alone: they punctuate and separate ranges.
    body = LIST_MARKER.sub("", (text or "").replace("\u2212", "-"))
    found = []
    for token in NUMBER.findall(body):
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


def _subset_sums(column: list[Decimal]) -> set[Decimal]:
    """Sums of parts of a column.

    "Direct costs are 94% of spend" adds three of four percentages; "combined they
    are $815,892" adds two of twelve clients. Both follow directly from the rows,
    which is what the rule permits -- the whole-column sum alone does not cover it.
    Bounded hard: short columns only. Anything longer is left alone, because the
    coincidences cost more than the check gains.
    """
    if len(column) > 12:
        # Pairwise sums over a long column manufacture coincidences: across 33
        # late-job durations they grounded both 39 and 52, which were invented.
        # A column that long is not one an answer adds up by hand anyway.
        return set()
    totals = {Decimal(0)}
    for value in column:
        totals |= {total + value for total in totals}
    return totals


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
            else:
                # Dates carry numbers an answer legitimately quotes: a result row
                # holding 2026-09-24 supports "through September 24, 2026".
                text = value.isoformat() if hasattr(value, "isoformat") else value
                if isinstance(text, str):
                    # A cell is data, not prose, so the permissive pattern is used:
                    # "2026-09-24" must yield 24, which the prose rule's
                    # hyphen lookbehind would drop as part of an identifier.
                    for token in DECLARED_NUMBER.findall(text):
                        inner = _to_decimal(token)
                        if inner is not None:
                            cells.add(inner)
        if column:
            aggregates.update({sum(column), min(column), max(column), Decimal(len(column))})
            aggregates.update(_subset_sums(column))

    return cells, aggregates


def _matches(stated: Decimal, supported: Iterable[Decimal]) -> bool:
    """True when `stated` equals a supported value, at any plausible scale.

    Comparison happens at the precision the PROSE uses. "$520,803" is how an answer
    writes the cell 520802.87, and "$4.5 million" is how it writes 4497901.88 --
    rounding for readability is not inventing a figure.
    """
    precision = Decimal(1).scaleb(stated.as_tuple().exponent)
    for value in supported:
        # A negative is routinely restated as a magnitude: a gross margin of
        # -27,857.16 is written "$27,857 over contract value".
        for signed in ({value, -value} if value else {value}):
            for scale in SCALES:
                scaled = signed / scale
                if stated == scaled:
                    return True
                try:
                    if scaled.quantize(precision, rounding=ROUND_HALF_UP) == stated:
                        return True
                except InvalidOperation:
                    continue
    return False


def check_answer(
    answer: str,
    tool_calls: Sequence[Any],
    question: str = "",
    declared: Iterable[Decimal] | None = None,
    seen_in_prompt: Iterable[Decimal] | None = None,
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
    printed = set(citable_figures() if seen_in_prompt is None else seen_in_prompt)
    warnings = [number for number in ungrounded if number in printed]
    return Grounding(numbers=stated, ungrounded=ungrounded, warnings=warnings)
