"""CLI: extract Apple Notes to ``/raw-staging/apple-notes/events.jsonl``.

Snapshots ``NoteStore.sqlite`` (plus WAL / SHM sidecars when present) to
``raw-staging/apple-notes/snapshot.sqlite`` and reads from the snapshot
in read-only mode, so the live Notes app can keep running without
interference.

Requirements:
  * Full Disk Access granted to Terminal (System Settings → Privacy &
    Security → Full Disk Access).
  * ``apple_notes_db_path`` in ``config/local.json`` points at the real
    NoteStore.sqlite (default:
    ``~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite``).
  * The Notes app must have been opened at least once on this machine so
    the database exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts._config import LocalConfig, load_config
from sources.apple_notes.adapter import extract_notes, snapshot_notes_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/local.json"),
        help="Path to local config (default: config/local.json).",
    )
    parser.add_argument(
        "--notes-db",
        type=Path,
        default=None,
        help="Source NoteStore.sqlite path. Defaults to apple_notes_db_path in config.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Where to write events.jsonl. Defaults to <raw_staging_dir from config>/apple-notes/.",
    )
    parser.add_argument(
        "--skip-snapshot",
        action="store_true",
        help="Skip the NoteStore.sqlite copy step and read the existing snapshot. "
        "Useful when iterating on the parser.",
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)
    source_db = args.notes_db if args.notes_db else cfg.apple_notes_db_path
    if source_db is None:
        print(
            "error: no NoteStore.sqlite path configured. Add apple_notes_db_path to "
            "config/local.json or pass --notes-db.",
            file=sys.stderr,
        )
        return 2

    out_dir = args.out_dir if args.out_dir else cfg.raw_staging_dir / "apple-notes"
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = out_dir / "snapshot.sqlite"
    out_file = out_dir / "events.jsonl"

    if not args.skip_snapshot:
        print(f"Snapshotting {source_db} → {snapshot_path}")
        snapshot_notes_db(source_db, snapshot_path)
    elif not snapshot_path.exists():
        print(f"error: --skip-snapshot set but {snapshot_path} does not exist.", file=sys.stderr)
        return 2
    else:
        print(f"Using existing snapshot at {snapshot_path}")

    print(f"Extracting notes → {out_file}")
    count = 0
    with out_file.open("w") as fout:
        for record in extract_notes(snapshot_path):
            fout.write(json.dumps(record) + "\n")
            fout.flush()
            count += 1
            if count % 100 == 0:
                print(f"  ...{count} notes", file=sys.stderr, flush=True)
    print(f"Extracted {count} apple-notes record(s) → {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
