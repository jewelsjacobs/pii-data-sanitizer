"""Per-source processing state: high-water mark of sanitized records.

After every sanitize run we write ``clean-staging/<source>/state.json``
recording the latest ``record["timestamp"]`` we've processed for that
source. This is the value the user wants when picking a ``--since`` for
the next incremental run — passing it back means any record dated on
or after the high-water mark is picked up, while dedupe skips ones
already in the clean file.

Computed by scanning the clean ``events.jsonl`` once at end-of-run, so
the state file always reflects what's actually on disk and never drifts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_FILENAME = "state.json"


def write_state(
    clean_dir: Path,
    *,
    run_window: str | None,
) -> dict[str, Any]:
    """Scan ``clean_dir/events.jsonl`` and write ``state.json`` next to it.

    ``run_window`` is the ``--since`` value the most recent sanitize run
    used (``None`` for a full pass). It's recorded so the user can see
    what the last run actually filtered on.

    Returns the state dict that was written.
    """
    events_path = clean_dir / "events.jsonl"
    latest_record_date = ""
    record_count = 0

    if events_path.exists():
        with events_path.open() as fin:
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                record_count += 1
                ts = rec.get("timestamp")
                if isinstance(ts, str) and len(ts) >= 10:
                    candidate = ts[:10]
                    if candidate > latest_record_date:
                        latest_record_date = candidate

    state: dict[str, Any] = {
        "last_run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "last_run_window": run_window,
        "record_count": record_count,
        "latest_record_date": latest_record_date or None,
        # The value the user should pass to --since on the next run.
        # We surface it explicitly so they don't have to interpret the
        # other fields. Equal to latest_record_date (--since is
        # inclusive; dedupe handles records already in the clean file).
        "suggested_next_since": latest_record_date or None,
    }

    clean_dir.mkdir(parents=True, exist_ok=True)
    state_path = clean_dir / STATE_FILENAME
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return state


def read_state(clean_dir: Path) -> dict[str, Any] | None:
    """Return the state dict for ``clean_dir`` or ``None`` if not yet written."""
    state_path = clean_dir / STATE_FILENAME
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())
    except json.JSONDecodeError:
        return None
