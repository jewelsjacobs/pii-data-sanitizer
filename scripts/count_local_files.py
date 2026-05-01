"""CLI: count files by extension under each ``local_files_source_dirs`` entry.

Run before ``make extract-local-files`` to see what volume you'd be
processing. Reports per-directory totals and a per-extension
breakdown so you can decide whether to scope the import down (drop
heavy ``.pdf`` dirs, exclude an archive folder, etc.) before kicking
off the slow OCR-bearing extract.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

from scripts._config import load_config

_SUPPORTED = {".txt", ".md", ".rtf", ".docx", ".pdf", ".doc"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/local.json"), help="Local config path."
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if not cfg.local_files_source_dirs:
        print("No local_files_source_dirs configured.", file=sys.stderr)
        return 2

    grand_total = 0
    grand_counts: collections.Counter[str] = collections.Counter()

    for d in cfg.local_files_source_dirs:
        p = Path(d).expanduser()
        if not p.exists():
            print(f"{d}: (does not exist)")
            continue
        if not p.is_dir():
            print(f"{d}: (not a directory)")
            continue
        c: collections.Counter[str] = collections.Counter()
        for f in p.rglob("*"):
            if f.is_file() and f.suffix.lower() in _SUPPORTED:
                c[f.suffix.lower()] += 1
        total = sum(c.values())
        grand_total += total
        grand_counts.update(c)
        per_ext = dict(sorted(c.items()))
        print(f"{d}: {total} files {per_ext}")

    print()
    print(f"TOTAL: {grand_total} files {dict(sorted(grand_counts.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
