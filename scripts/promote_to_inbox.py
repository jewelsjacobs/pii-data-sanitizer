"""CLI: promote sanitized JSONL records into Obsidian-friendly markdown.

Reads ``clean-staging/<source>/events.jsonl`` and writes one or more
markdown notes per record into ``promote_staging_dir/<source>/``. The
promote staging directory is OUTSIDE the vault by design — the user
reviews the staging folder and manually moves approved notes into
``00-Inbox/``. This preserves the trust boundary documented in
``CLAUDE.md`` and ``files/pipeline-architecture.md``: the sanitizer
and promote step run as the user, the Crew agents run separately, and
no automated process writes into the vault.

Already-existing files in the staging folder are skipped — re-runs are
safe and the user can clean up the staging folder at any time.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sanitizer.promote import (
    promote_apple_mail,
    promote_apple_notes,
    promote_ical,
    promote_imessage,
    promote_local_files,
)
from scripts._config import LocalConfig, load_config
from scripts._filter import is_in_window, validate_since, validate_until

# Sources that promote one note per record.
_PER_RECORD_PROMOTERS = {
    "ical": promote_ical,
    "apple-mail": promote_apple_mail,
    "local-files": promote_local_files,
    "apple-notes": promote_apple_notes,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        choices=["ical", "apple-mail", "imessage", "local-files", "apple-notes"],
        help="Source name to promote.",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/local.json"), help="Local config path."
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing notes in the staging folder. Default is to skip.",
    )
    parser.add_argument(
        "--since",
        type=validate_since,
        default=None,
        help=(
            "Only promote records whose timestamp is on or after this YYYY-MM-DD "
            "(inclusive). Skip-if-exists in the staging folder still applies, so "
            "older notes from prior runs are left alone."
        ),
    )
    parser.add_argument(
        "--until",
        type=validate_until,
        default=None,
        help=(
            "Only promote records whose timestamp is strictly before this "
            "YYYY-MM-DD (exclusive upper bound). Pair with --since for clean "
            "non-overlapping windows: --since A --until B then --since B --until C."
        ),
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)

    clean_file = cfg.clean_staging_dir / args.source / "events.jsonl"
    if not clean_file.exists():
        print(
            f"error: no sanitized input at {clean_file}. Run `make sanitize-{args.source}` first.",
            file=sys.stderr,
        )
        return 2

    out_dir = cfg.promote_staging_dir / args.source
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.since or args.until:
        window_parts = []
        if args.since:
            window_parts.append(f"--since {args.since}")
        if args.until:
            window_parts.append(f"--until {args.until}")
        window_label = " ".join(window_parts)
    else:
        window_label = "full"
    print(
        f"Promoting {args.source} → {out_dir}\n"
        f"   from: {clean_file}\n"
        f"   window: {window_label}\n"
        f"   note: this is OUTSIDE the vault — review and move manually.",
        file=sys.stderr,
        flush=True,
    )

    records = (
        rec for rec in _iter_records(clean_file) if is_in_window(rec, args.since, args.until)
    )

    if args.source == "imessage":
        notes = promote_imessage(records)
    else:
        promoter = _PER_RECORD_PROMOTERS[args.source]
        notes = (promoter(rec) for rec in records)

    written = 0
    skipped = 0
    duplicates = 0
    seen: dict[str, int] = {}

    for filename, markdown in notes:
        # On rare collisions (same date + same title), suffix with a
        # disambiguator so we never silently overwrite a sibling note.
        unique_name = filename
        if filename in seen:
            seen[filename] += 1
            duplicates += 1
            unique_name = _disambiguate(filename, seen[filename])
        else:
            seen[filename] = 1

        out_path = out_dir / unique_name
        if out_path.exists() and not args.overwrite:
            skipped += 1
            continue
        out_path.write_text(markdown)
        written += 1
        if written % 100 == 0:
            print(f"  wrote {written} note(s)…", file=sys.stderr, flush=True)

    print(
        f"Done. wrote={written} skipped={skipped} disambiguated={duplicates}\n"
        f"Review and move approved notes into the vault's 00-Inbox/.",
        file=sys.stderr,
        flush=True,
    )
    return 0


def _iter_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open() as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _disambiguate(filename: str, n: int) -> str:
    if filename.endswith(".md"):
        return f"{filename[:-3]} ({n}).md"
    return f"{filename} ({n})"


if __name__ == "__main__":
    sys.exit(main())
