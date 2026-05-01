"""Shared helpers for the ``--since YYYY-MM-DD`` and ``--until YYYY-MM-DD`` flags.

Sanitize / audit / promote each accept these flags to bound the date
window of records they operate on. ISO 8601 timestamps sort
lexicographically, so the first 10 characters (``YYYY-MM-DD``) compare
correctly against a date threshold without parsing the full datetime.

* ``--since YYYY-MM-DD`` — keep records whose timestamp is on or after
  this date (inclusive lower bound).
* ``--until YYYY-MM-DD`` — keep records whose timestamp is **strictly
  before** this date (exclusive upper bound). Pairing it with the next
  window's ``--since`` means non-overlapping ranges:
  ``--since 2025-11-01 --until 2026-01-01`` then
  ``--since 2026-01-01 --until 2026-03-02`` covers everything cleanly.

Records with a missing or malformed timestamp are excluded from any
windowed run — without a timestamp we can't decide whether they belong
in the window, and silently passing them through would contradict the
flag's intent.
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
    return _validate_date_arg(value, flag="--since")


def validate_until(value: str | None) -> str | None:
    """Validate a ``YYYY-MM-DD`` argument and return it unchanged.

    Same shape rules as ``--since``. Semantics differ: ``--until`` is
    an **exclusive** upper bound (records strictly before this date pass).
    """
    return _validate_date_arg(value, flag="--until")


def _validate_date_arg(value: str | None, *, flag: str) -> str | None:
    if value is None:
        return None
    if not _DATE_SHAPE.match(value):
        raise argparse.ArgumentTypeError(
            f"{flag} must be in YYYY-MM-DD form (zero-padded), got {value!r}"
        )
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{flag} is not a valid date: {value!r}") from exc
    return value


def is_in_window(
    record: dict[str, Any],
    since: str | None,
    until: str | None = None,
) -> bool:
    """Return whether ``record`` should be kept under the date window.

    * ``since=None, until=None`` — every record passes.
    * ``since`` set — record's ``timestamp[:10]`` must be ``>= since``
      (inclusive lower bound).
    * ``until`` set — record's ``timestamp[:10]`` must be ``< until``
      (**exclusive** upper bound, so back-to-back windows don't
      double-count the boundary day).
    * Both set — both conditions must hold.

    Records with a missing or non-string timestamp are excluded
    whenever any bound is active.
    """
    if since is None and until is None:
        return True
    ts = record.get("timestamp")
    if not isinstance(ts, str) or len(ts) < 10:
        return False
    day = ts[:10]
    if since is not None and day < since:
        return False
    if until is not None and day >= until:
        return False
    return True
