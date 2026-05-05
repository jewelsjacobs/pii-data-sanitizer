"""CLI: print a privacy-safe summary of the loaded config.

Useful for confirming that fields like ``scrub_strings``, ``self_emails``,
``keep_names`` actually loaded the values you intended (typos in JSON
key names or quote stripping otherwise produce silently-empty fields).

Output masks each value to its length + first/last character so you
can verify the right number of entries with sane sizes without
exposing the values themselves.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts._config import load_config


def _mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[0]}{'*' * (len(value) - 2)}{value[-1]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/local.json"),
        help="Local config path.",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)

    print(f"Config: {args.config}")
    print()

    fields = [
        ("ical_source_dirs", cfg.ical_source_dirs),
        ("local_files_source_dirs", cfg.local_files_source_dirs),
        ("imessage_chat_db_path", [cfg.imessage_chat_db_path] if cfg.imessage_chat_db_path else []),
        ("self_emails", sorted(cfg.self_emails)),
        ("self_phones", sorted(cfg.self_phones)),
        ("keep_names", sorted(cfg.keep_names)),
        ("keep_gpes", sorted(cfg.keep_gpes)),
        ("scrub_strings", sorted(cfg.scrub_strings, key=len, reverse=True)),
    ]

    for name, items in fields:
        if not items:
            print(f"{name}: <empty>")
            continue
        print(f"{name}: {len(items)} entries")
        for item in items[:20]:
            s = str(item)
            print(f"  {len(s):>4} chars  {_mask(s)}")
        if len(items) > 20:
            print(f"  ...and {len(items) - 20} more")

    print()
    print("Paths:")
    print(f"  mapping_path:        {cfg.mapping_path}")
    print(f"  raw_staging_dir:     {cfg.raw_staging_dir}")
    print(f"  clean_staging_dir:   {cfg.clean_staging_dir}")
    print(f"  promote_staging_dir: {cfg.promote_staging_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
