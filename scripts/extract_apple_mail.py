"""CLI: extract iCloud mail to ``/raw-staging/apple-mail/events.jsonl``.

Two-phase flow:

1. **fetch** — connect to iCloud IMAP, download every not-yet-cached
   message's raw RFC822 bytes to ``raw-staging/apple-mail/cache/``.
   Interruptible and resumable: already-cached UIDs are skipped
   without a network round-trip.
2. **parse** — read the cache from disk and emit normalized records
   as JSONL. Pure local compute; never hits the network.

Use ``--skip-fetch`` to re-run just the parse phase against an existing
cache (fast iteration on parser / sanitizer changes).

Reads ``ICLOUD_EMAIL`` + ``ICLOUD_APP_PASSWORD`` from ``.env`` via
python-dotenv. The adapter never logs the password; neither does this
script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from scripts._config import LocalConfig, load_config
from sources.apple_mail.adapter import fetch_and_cache, parse_cache


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
        "--cache-dir",
        type=Path,
        default=None,
        help="Where raw .eml files are stored. Defaults to "
        "<raw_staging_dir from config>/apple-mail/cache/.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Where to write events.jsonl. Defaults to <raw_staging_dir from config>/apple-mail/.",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip phase 1 (IMAP) and only re-parse the existing cache. "
        "Useful when iterating on the parser or the sanitize stage.",
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)
    out_dir = args.out_dir if args.out_dir else cfg.raw_staging_dir / "apple-mail"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = args.cache_dir if args.cache_dir else out_dir / "cache"
    out_file = out_dir / "events.jsonl"

    # --- Phase 1: fetch to cache -----------------------------------------
    if not args.skip_fetch:
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
        print(f"Phase 1: fetching mail for {email_address} → {cache_dir}")
        new_count, already_count = fetch_and_cache(email_address, password, cache_dir)
        print(
            f"Phase 1 done: {new_count} new, {already_count} already cached "
            f"(total {new_count + already_count} on disk)."
        )
    else:
        print(f"Phase 1: skipped (--skip-fetch). Using existing cache at {cache_dir}.")

    # --- Phase 2: parse cache → events.jsonl -----------------------------
    print(f"Phase 2: parsing cache → {out_file}")
    count = 0
    with out_file.open("w") as fout:
        for record in parse_cache(cache_dir):
            fout.write(json.dumps(record) + "\n")
            fout.flush()
            count += 1
            if count % 500 == 0:
                print(f"  ...{count} records", file=sys.stderr, flush=True)
    print(f"Phase 2 done: {count} records → {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
