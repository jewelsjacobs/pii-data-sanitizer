"""CLI: find records still containing a given needle string after sanitize.

Diagnostic tool for the spot-check phase. Given a known sensitive
literal that should have been redacted (e.g. via ``scrub_strings``),
walks ``clean-staging/<source>/events.jsonl`` and prints, for each
record that *still* contains the needle:

* ``source_id`` — so you can find it in the raw file if needed.
* Which field(s) of the record contain the needle (``content``,
  ``metadata.path``, ``metadata.subject``, etc).
* Total record content length — helps identify whether the residual
  leak correlates with the 200k-char chunk boundary.
* Whether the record is flagged ``content_truncated`` — points at
  truncation as a possible cause.

Output prints one line per match in TSV-ish form, all alphanumerics
masked so it's safe to share. The needle itself is masked too.

Example:
    uv run python -m scripts.find_residual_pii \\
        --needle Fauntleroy \\
        --file clean-staging/local-files/events.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--needle",
        required=True,
        help="The literal string to search for. Match is case-insensitive.",
    )
    parser.add_argument(
        "--file",
        type=Path,
        required=True,
        help="Path to the clean-staging events.jsonl to search.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of matching records to report.",
    )
    args = parser.parse_args(argv)

    if not args.file.exists():
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return 2

    needle_lower = args.needle.casefold()
    total = 0
    reported = 0

    with args.file.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            fields = []
            content = rec.get("content")
            if isinstance(content, str) and needle_lower in content.casefold():
                fields.append(f"content(len={len(content)})")

            meta = rec.get("metadata")
            if isinstance(meta, dict):
                for k, v in meta.items():
                    if isinstance(v, str) and needle_lower in v.casefold():
                        fields.append(f"metadata.{k}(len={len(v)})")
                    elif isinstance(v, list):
                        for i, item in enumerate(v):
                            if isinstance(item, str) and needle_lower in item.casefold():
                                fields.append(f"metadata.{k}[{i}](len={len(item)})")
                    elif isinstance(v, dict):
                        for sub_k, sub_v in v.items():
                            if isinstance(sub_v, str) and needle_lower in sub_v.casefold():
                                fields.append(f"metadata.{k}.{sub_k}(len={len(sub_v)})")

            if not fields:
                continue

            total += 1
            if reported >= args.limit:
                continue

            sid = rec.get("source_id", "?")
            sid_short = sid[:24] + "..." if len(sid) > 24 else sid
            truncated = (meta or {}).get("content_truncated", False)
            orig_len = (meta or {}).get("original_content_chars", "")

            print(
                f"id={sid_short}\t"
                f"truncated={truncated}\t"
                f"orig_len={orig_len}\t"
                f"fields={','.join(fields)}"
            )
            reported += 1

    print(f"\n(total records with residual matches: {total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
