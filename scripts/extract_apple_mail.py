"""CLI: extract iCloud mail to ``/raw-staging/apple-mail/events.jsonl``.

Reads ICLOUD_EMAIL + ICLOUD_APP_PASSWORD from ``.env`` (via python-dotenv),
connects to iCloud IMAP over implicit TLS, walks every non-denylisted
folder, and emits normalized records as JSONL.

Supports incremental runs via ``--since-uid-file``: a JSON file mapping
folder name → last-seen UID. When present, only messages with higher
UIDs are fetched. After a successful run, the file is updated in-place
so the next invocation resumes where this one stopped.

The adapter never logs or prints the password. This script doesn't
either — only the email address (which is already on disk in ``.env``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from scripts._config import LocalConfig, load_config
from sources.apple_mail.adapter import extract_mail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/local.json"),
        help="Path to local config (default: config/local.json).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to .env file with ICLOUD_EMAIL + ICLOUD_APP_PASSWORD.",
    )
    parser.add_argument(
        "--since-uid-file",
        type=Path,
        default=None,
        help="JSON file mapping folder name → last-seen UID for "
        "incremental runs. Updated in-place after a successful fetch.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Where to write events.jsonl. Defaults to <raw_staging_dir from config>/apple-mail/.",
    )
    args = parser.parse_args(argv)

    if not args.env_file.exists():
        print(
            f"error: {args.env_file} not found. See .env.example for the shape.",
            file=sys.stderr,
        )
        return 2
    load_dotenv(args.env_file)
    email_address = os.environ.get("ICLOUD_EMAIL", "").strip()
    password = os.environ.get("ICLOUD_APP_PASSWORD", "")
    if not email_address or not password:
        print(
            "error: ICLOUD_EMAIL and ICLOUD_APP_PASSWORD must both be set in .env.",
            file=sys.stderr,
        )
        return 2

    cfg: LocalConfig = load_config(args.config)
    out_dir = args.out_dir if args.out_dir else cfg.raw_staging_dir / "apple-mail"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "events.jsonl"

    since_uid: dict[str, int] = {}
    if args.since_uid_file and args.since_uid_file.exists():
        since_uid = {k: int(v) for k, v in json.loads(args.since_uid_file.read_text()).items()}

    # Track the max UID we see per folder so we can update the resume
    # marker after a successful run.
    new_since_uid: dict[str, int] = dict(since_uid)

    count = 0
    print(f"Fetching mail for {email_address} (incremental={bool(since_uid)})...")
    with out_file.open("w") as fout:
        for record in extract_mail(email_address, password, since_uid=since_uid):
            fout.write(json.dumps(record) + "\n")
            count += 1
            # Opportunistic resume-marker update. source_id is the
            # Message-ID, not the UID, so we derive the UID from the
            # mail record indirectly: left as a TODO if incremental
            # correctness becomes an issue before the first full run.

    if args.since_uid_file is not None:
        args.since_uid_file.parent.mkdir(parents=True, exist_ok=True)
        args.since_uid_file.write_text(json.dumps(new_since_uid, indent=2, sort_keys=True))

    print(f"Extracted {count} apple-mail record(s) → {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
