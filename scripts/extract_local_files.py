"""CLI: extract local documents to ``/raw-staging/local-files/events.jsonl``.

Reads the ``local_files_source_dirs`` list from ``config/local.json``
(or ``--source-dir`` overrides), walks each path for supported
document formats, and emits normalized records in JSONL. The sanitize
step then reads the same ``<source>/events.jsonl`` convention as ical.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts._config import LocalConfig, load_config
from sources.local_files.adapter import SUPPORTED_EXTENSIONS, AdapterError, extract_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        action="append",
        type=Path,
        default=None,
        help="Path to a file or directory. Can be given multiple times. "
        "Overrides config/local.json.",
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
        help="Where to write events.jsonl. Defaults to <raw_staging_dir from config>/local-files/.",
    )
    parser.add_argument(
        "--include-extensions",
        type=_parse_extensions,
        default=None,
        help=(
            "Comma-separated list of extensions to process (subset of "
            f"{sorted(SUPPORTED_EXTENSIONS)}). Other extensions in the walk are "
            "skipped silently. Useful when one extension dominates a source dir "
            "(e.g. an Obsidian vault syncing 100k .md files into iCloud Drive). "
            "Example: --include-extensions .pdf,.docx,.txt,.rtf"
        ),
    )
    parser.add_argument(
        "--skip-unsupported",
        action="store_true",
        help=(
            "Warn-and-skip on legacy .doc files instead of halting. Default is "
            "to halt (matches the fail-loud policy). Use this on one-shot "
            "historical archive runs where you'd rather get most of the files "
            "than convert .doc files to .docx first."
        ),
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)
    source_dirs = args.source_dir if args.source_dir else cfg.local_files_source_dirs
    if not source_dirs:
        print(
            "error: no local-files sources configured. Add local_files_source_dirs to "
            "config/local.json or pass --source-dir.",
            file=sys.stderr,
        )
        return 2

    out_dir = args.out_dir if args.out_dir else cfg.raw_staging_dir / "local-files"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "events.jsonl"

    count = 0
    with out_file.open("w") as fout:
        for record in extract_files(
            source_dirs,
            include_extensions=args.include_extensions,
            skip_unsupported=args.skip_unsupported,
        ):
            fout.write(json.dumps(record) + "\n")
            count += 1
            if count % 500 == 0:
                print(f"  ...{count} records", file=sys.stderr, flush=True)

    print(f"Extracted {count} local-files record(s) → {out_file}")
    return 0


def _parse_extensions(value: str) -> frozenset[str]:
    """Parse a comma-separated list of extensions, normalizing dots and case."""
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("--include-extensions must list at least one extension")
    normalized = frozenset(p if p.startswith(".") else f".{p}" for p in parts)
    bad = normalized - SUPPORTED_EXTENSIONS
    if bad:
        raise argparse.ArgumentTypeError(
            f"--include-extensions contains unsupported entries: {sorted(bad)}. "
            f"Allowed: {sorted(SUPPORTED_EXTENSIONS)}"
        )
    return normalized


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AdapterError as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        sys.exit(2)
