"""Round-tripping every type DuckDB hands back.

An eval run crashed on the final write -- `date` was not handled -- and discarded
fifteen completed model calls. These pin every type the engine can return, not
only the ones a particular query happened to produce.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest

from jobcosting.engine import DuckDBEngine
from jobcosting.serialisation import decode, decode_rows, encode, encode_rows, to_display

# Everything a DuckDB query has been observed to return.
VALUES = [
    Decimal("7684852.81"),
    dt.date(2026, 4, 24),
    dt.datetime(2026, 4, 24, 13, 45, 6, 123456),
    dt.time(12, 34, 56),
    b"\x00binary\xff",
    dt.timedelta(days=3, seconds=45, microseconds=7),
    uuid.UUID("209187de-f349-4c48-b51b-b04ad570d5dd"),
    42,
    1.5,
    True,
    False,
    None,
    "a plain string",
    "",
]


@pytest.mark.parametrize("value", VALUES, ids=[type(v).__name__ + repr(v)[:14] for v in VALUES])
def test_every_type_survives_a_json_round_trip(value):
    """encode -> json -> decode must give back an equal value of the same type."""
    restored = decode(json.loads(json.dumps(encode(value))))
    assert restored == value
    assert type(restored) is type(value)


@pytest.mark.parametrize("value", VALUES, ids=[type(v).__name__ for v in VALUES])
def test_every_type_is_json_serialisable_once_encoded(value):
    json.dumps(encode(value))          # must not raise


def test_rows_round_trip_as_tuples():
    rows = [(Decimal("1.50"), dt.date(2026, 1, 2), None), (Decimal("2.50"), None, b"x")]
    restored = decode_rows(json.loads(json.dumps(encode_rows(rows))))
    assert restored == rows
    assert all(isinstance(r, tuple) for r in restored)


def test_an_unknown_type_degrades_instead_of_raising():
    """The defect was a crash on the final write. Nothing may ever raise here."""
    class Exotic:
        def __str__(self):
            return "exotic!"

    encoded = encode(Exotic())
    json.dumps(encoded)                 # must not raise
    assert decode(encoded) == "exotic!"


def test_a_real_query_with_dates_round_trips():
    """The exact query shape that crashed the run."""
    engine = DuckDBEngine()
    result = engine.run(
        "SELECT JobNumber, ActualEndDate, CAST(ActualEndDate AS DATE), "
        "CAST(ContractValue AS DECIMAL(18,2)) FROM dim_job "
        "WHERE ActualEndDate > ScheduledEndDate LIMIT 5"
    )
    assert any(isinstance(v, dt.date) for row in result.rows for v in row)
    assert decode_rows(json.loads(json.dumps(encode_rows(result.rows)))) == result.rows


# ------------------------------------------------------- the display encoding

@pytest.mark.parametrize("value,expected", [
    (Decimal("7684852.81"), "7684852.81"),          # exact, never a float
    (dt.date(2026, 4, 24), "2026-04-24"),
    (dt.datetime(2026, 4, 24, 13, 45), "2026-04-24T13:45:00"),
    (dt.time(12, 34, 56), "12:34:56"),
    (42, 42),
    (True, True),
    (None, None),
    ("text", "text"),
])
def test_display_encoding_is_plain_json(value, expected):
    assert to_display(value) == expected
    json.dumps(to_display(value))


def test_display_encoding_never_raises_either():
    class Exotic:
        def __str__(self):
            return "exotic!"

    json.dumps(to_display(Exotic()))
