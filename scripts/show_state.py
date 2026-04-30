"""CLI: show per-source processing state.

Reads ``clean-staging/<source>/state.json`` (written by sanitize) for
each known source and prints a short report. Use this to decide what
``--since`` value to pass on the next incremental run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sanitizer.state import read_state
from scripts._config import LocalConfig, load_config

KNOWN_SOURCES = ("ical", "apple-mail", "imessage", "local-files")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/local.json"), help="Local config path."
    )
    parser.add_argument(
        "--source",
        choices=KNOWN_SOURCES,
        default=None,
        help="Show only one source. Default: show all.",
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)
    sources = [args.source] if args.source else list(KNOWN_SOURCES)

    print(f"{'source':<14} {'last run (UTC)':<26} {'records':>8}  next --since")
    print("-" * 70)
    any_state = False
    for source in sources:
        clean_dir = cfg.clean_staging_dir / source
        state = read_state(clean_dir)
        if state is None:
            print(f"{source:<14} {'(no state — never sanitized)':<26}")
            continue
        any_state = True
        last_run = (state.get("last_run_at") or "")[:25]
        record_count = state.get("record_count", 0)
        suggested = state.get("suggested_next_since") or "(no records)"
        print(f"{source:<14} {last_run:<26} {record_count:>8}  {suggested}")

    if not any_state:
        print(
            "\nNo state files yet. Run `make sanitize-<source>` first.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
