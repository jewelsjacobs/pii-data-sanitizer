"""CLI: find a literal string in a file and print masked context windows.

Useful for diagnosing residual leaks after sanitize: pass a known
sensitive value with ``--needle``, point at ``clean-staging/.../events.jsonl``,
and get N randomly-sampled context windows around each occurrence with
all alphanumerics replaced by ``X`` — completely safe to paste into
chat or share for diagnosis without exposing the actual content.

Example:
    uv run python -m scripts.grep_masked_context \\
        --needle 1234 \\
        --file clean-staging/local-files/events.jsonl \\
        --window 40 --samples 15

Output: 15 lines like ``XX X. XXXX XXXXX XX  XXXX-XX-XXXX``, plus a
``total matches: N`` line at the end. Patterns of interest:
* ``XXXX X. XXXX XXXXX`` followed by ``Street/Ave/Hwy/etc.`` — real
  address leaks, scrub_strings needs more variants.
* ``XX/XX/XXXX`` or ``XXXX-XX-XX`` — date coincidences, ignore.
* ``vX.X.X`` — version number coincidences, ignore.
* ``XXX-XXX-XXXX`` — phone-shaped, separately-flagged elsewhere.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--needle",
        required=True,
        help="The literal string (or regex with --regex) to search for.",
    )
    parser.add_argument(
        "--regex",
        action="store_true",
        help="Treat --needle as a regular expression instead of a literal string.",
    )
    parser.add_argument(
        "--file",
        type=Path,
        required=True,
        help="Path to the file to search (typically a clean-staging events.jsonl).",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=40,
        help="Characters of context to show on each side of the match (default 40).",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=15,
        help="Maximum number of randomly-selected matches to print (default 15).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed for sample selection (default 0 → deterministic).",
    )
    args = parser.parse_args(argv)

    if not args.file.exists():
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return 2
    if not args.needle:
        print("error: --needle must be a non-empty string", file=sys.stderr)
        return 2

    matches: list[str] = []
    if args.regex:
        try:
            pattern = re.compile(args.needle)
        except re.error as exc:
            print(f"error: invalid regex {args.needle!r}: {exc}", file=sys.stderr)
            return 2
        with args.file.open() as f:
            for line in f:
                for m in pattern.finditer(line):
                    start = max(0, m.start() - args.window)
                    end = min(len(line), m.end() + args.window)
                    matches.append(line[start:end])
    else:
        needle = args.needle
        with args.file.open() as f:
            for line in f:
                idx = 0
                while True:
                    idx = line.find(needle, idx)
                    if idx < 0:
                        break
                    start = max(0, idx - args.window)
                    end = min(len(line), idx + len(needle) + args.window)
                    matches.append(line[start:end])
                    idx += len(needle)

    rng = random.Random(args.seed)
    rng.shuffle(matches)

    for ctx in matches[: args.samples]:
        masked = "".join("X" if c.isalnum() else c for c in ctx).rstrip("\n")
        print(masked)

    print(f"(total matches: {len(matches)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
