"""CLI: extract iMessage to ``/raw-staging/imessage/events.jsonl``.

Snapshots ``chat.db`` (plus WAL / SHM sidecars when present) to
``raw-staging/imessage/snapshot.db`` and reads from the snapshot in
read-only mode, so the live Messages app can keep running without
interference.

Requirements:
  * Full Disk Access granted to Terminal (System Settings → Privacy &
    Security → Full Disk Access).
  * ``imessage_chat_db_path`` in ``config/local.json`` points at the
    real ``chat.db`` (default: ``~/Library/Messages/chat.db``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts._config import LocalConfig, load_config
from sources.imessage.adapter import extract_messages, snapshot_chat_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/local.json"),
        help="Path to local config (default: config/local.json).",
    )
    parser.add_argument(
        "--chat-db",
        type=Path,
        default=None,
        help="Source chat.db path. Defaults to imessage_chat_db_path in config.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Where to write events.jsonl. Defaults to <raw_staging_dir from config>/imessage/.",
    )
    parser.add_argument(
        "--skip-snapshot",
        action="store_true",
        help="Skip the chat.db copy step and read the existing snapshot. "
        "Useful when iterating on the parser.",
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)
    source_db = args.chat_db if args.chat_db else cfg.imessage_chat_db_path
    if source_db is None:
        print(
            "error: no chat.db path configured. Add imessage_chat_db_path to "
            "config/local.json or pass --chat-db.",
            file=sys.stderr,
        )
        return 2

    out_dir = args.out_dir if args.out_dir else cfg.raw_staging_dir / "imessage"
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = out_dir / "snapshot.db"
    out_file = out_dir / "events.jsonl"

    # --- Phase 1: snapshot chat.db ---------------------------------------
    if not args.skip_snapshot:
        print(f"Snapshotting {source_db} → {snapshot_path}")
        snapshot_chat_db(source_db, snapshot_path)
    elif not snapshot_path.exists():
        print(f"error: --skip-snapshot set but {snapshot_path} does not exist.", file=sys.stderr)
        return 2
    else:
        print(f"Using existing snapshot at {snapshot_path}")

    # --- Phase 2: extract → events.jsonl ---------------------------------
    print(f"Extracting messages → {out_file}")
    count = 0
    with out_file.open("w") as fout:
        for record in extract_messages(snapshot_path):
            fout.write(json.dumps(record) + "\n")
            fout.flush()
            count += 1
            if count % 1000 == 0:
                print(f"  ...{count} records", file=sys.stderr, flush=True)
    print(f"Extracted {count} imessage record(s) → {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
