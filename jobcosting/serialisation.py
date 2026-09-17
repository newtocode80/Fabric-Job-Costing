"""Turning DuckDB values into JSON and back.

Two encodings, for two different jobs:

  encode / decode  -- lossless, tagged, for saving an eval run to disk. A saved
                      answer must score identically when replayed, so a Decimal
                      has to come back a Decimal and a date a date.
  to_display       -- lossy and plain, for the /ask response the page renders.
                      No tags: the browser wants "2026-04-24", not a wrapper.

Neither ever raises. An unknown type degrades to its string form, because the
defect these replaced was a crash on the final write of a run that had already
made fifteen model calls.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Iterable, Sequence

TAG = "__t"
VALUE = "v"


def encode(value: Any) -> Any:
    """Lossless JSON-safe form. Round-trips through `decode`."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return {TAG: "decimal", VALUE: str(value)}
    # datetime before date: datetime is a subclass of date.
    if isinstance(value, dt.datetime):
        return {TAG: "datetime", VALUE: value.isoformat()}
    if isinstance(value, dt.date):
        return {TAG: "date", VALUE: value.isoformat()}
    if isinstance(value, dt.time):
        return {TAG: "time", VALUE: value.isoformat()}
    if isinstance(value, dt.timedelta):
        return {TAG: "timedelta", VALUE: value.total_seconds()}
    if isinstance(value, (bytes, bytearray)):
        return {TAG: "bytes", VALUE: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, uuid.UUID):
        return {TAG: "uuid", VALUE: str(value)}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    # Anything unforeseen: keep the run, lose the type.
    return {TAG: "repr", VALUE: str(value)}


_DECODERS = {
    "decimal": Decimal,
    "datetime": dt.datetime.fromisoformat,
    "date": dt.date.fromisoformat,
    "time": dt.time.fromisoformat,
    "timedelta": lambda seconds: dt.timedelta(seconds=seconds),
    "bytes": lambda text: base64.b64decode(text.encode("ascii")),
    "uuid": uuid.UUID,
    "repr": lambda text: text,
}


def decode(value: Any) -> Any:
    """Inverse of `encode`. Unknown tags and untagged values pass through."""
    if isinstance(value, dict) and TAG in value:
        decoder = _DECODERS.get(value[TAG])
        if decoder is None:
            return value.get(VALUE)
        try:
            return decoder(value[VALUE])
        except Exception:
            return value.get(VALUE)
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        return {k: decode(v) for k, v in value.items()}
    return value


def encode_rows(rows: Iterable[Sequence[Any]]) -> list[list[Any]]:
    return [[encode(v) for v in row] for row in rows]


def decode_rows(rows: Iterable[Sequence[Any]]) -> list[tuple[Any, ...]]:
    """Rows come back as tuples, matching what the engine returns."""
    return [tuple(decode(v) for v in row) for row in rows]


def to_display(value: Any) -> Any:
    """Plain JSON for the API and the page.

    Decimal becomes a string, not a float: money is cast to DECIMAL(18,2) so it
    prints exactly, and a float round-trip would put the rounding error back.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return [to_display(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_display(v) for k, v in value.items()}
    return str(value)
