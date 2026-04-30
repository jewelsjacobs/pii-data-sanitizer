"""Shared helpers for the ``--since YYYY-MM-DD`` flag.

Sanitize / audit / promote each accept ``--since`` to filter normalized
records by their ``timestamp`` field. ISO 8601 timestamps sort
lexicographically, so the first 10 characters (``YYYY-MM-DD``) compare
correctly against a date threshold without parsing the full datetime.

Records with a missing or malformed timestamp are excluded from any
``--since`` run — without a timestamp we can't decide whether they
belong in the window, and silently passing them through would
contradict the flag's intent.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from typing import Any

# Strict zero-padded ``YYYY-MM-DD``. ``datetime.strptime`` alone is too
# lenient — it accepts ``2026-3-30`` (no zero-pad), which would compare
# wrong against the lexicographic ISO 8601 timestamps in records.
_DATE_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_since(value: str | None) -> str | None:
    """Validate a ``YYYY-MM-DD`` argument and return it unchanged.

    Returns ``None`` when the argument is not set. Raises
    ``argparse.ArgumentTypeError`` (so argparse surfaces a clean error)
    when the shape is wrong — fail fast rather than silently match
    nothing.
    """
    if value is None:
        return None
    if not _DATE_SHAPE.match(value):
        raise argparse.ArgumentTypeError(
            f"--since must be in YYYY-MM-DD form (zero-padded), got {value!r}"
        )
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--since is not a valid date: {value!r}") from exc
    return value


def is_in_window(record: dict[str, Any], since: str | None) -> bool:
    """Return whether ``record`` should be kept under the ``--since`` filter.

    With ``since=None`` every record passes. Otherwise the record's
    ``timestamp`` (ISO 8601, possibly date-only for all-day events)
    must be lexicographically >= ``since`` on its first 10 characters.
    Missing or non-string timestamps are excluded.
    """
    if since is None:
        return True
    ts = record.get("timestamp")
    if not isinstance(ts, str) or len(ts) < 10:
        return False
    return ts[:10] >= since
