"""CLI: extract iCalendar events to ``/raw-staging/ical/events.jsonl``.

Reads the ``ical_source_dirs`` list from ``config/local.json`` (or
``--source-dir`` overrides), walks each path for ``.ics`` files, and
emits normalized records in JSONL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts._config import LocalConfig, load_config
from sources.ical.adapter import extract_events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        action="append",
        type=Path,
        default=None,
        help="Path to a directory of .ics files or a single .ics file. "
        "Can be given multiple times. Overrides config/local.json.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/local.json"),
        help="Path to local config (default: config/local.json).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Where to write events.jsonl. Defaults to <raw_staging_dir from config>/ical/.",
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)
    source_dirs = args.source_dir if args.source_dir else cfg.ical_source_dirs
    if not source_dirs:
        print(
            "error: no ical sources configured. Add ical_source_dirs to config/local.json "
            "or pass --source-dir.",
            file=sys.stderr,
        )
        return 2

    out_dir = args.out_dir if args.out_dir else cfg.raw_staging_dir / "ical"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "events.jsonl"

    count = 0
    with out_file.open("w") as fout:
        for record in extract_events(source_dirs):
            fout.write(json.dumps(record) + "\n")
            count += 1

    print(f"Extracted {count} ical event(s) → {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
