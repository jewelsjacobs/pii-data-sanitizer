"""CLI: report WHICH FIELD each audit finding's excerpt lives in — no PII output.

Joins an audit findings JSONL against the sanitized clean-staging records
and, for each finding, searches the excerpt across ``content`` and every
``metadata`` field (including lists and nested dicts). Prints only the
finding index, severity/category, and the field path(s) where the excerpt
was found — never the excerpt itself. The output is safe to share with an
agent that must not see the underlying PII.

Example:
    uv run python -m scripts.locate_audit_findings \\
        --findings logs/audit/2026-07-03T18-28-33/ical.jsonl \\
        --clean clean-staging/ical/events.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _find_paths(value: Any, needle: str, prefix: str) -> list[str]:
    """Return field paths under ``prefix`` whose string value contains ``needle``."""
    hits: list[str] = []
    if isinstance(value, str):
        if needle in value:
            hits.append(prefix)
    elif isinstance(value, dict):
        for k, v in value.items():
            hits.extend(_find_paths(v, needle, f"{prefix}.{k}"))
    elif isinstance(value, list):
        for item in value:
            # Collapse list indices — "attendees[3]" adds noise, not signal.
            for hit in _find_paths(item, needle, f"{prefix}[]"):
                if hit not in hits:
                    hits.append(hit)
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--findings", type=Path, required=True)
    parser.add_argument("--clean", type=Path, required=True)
    args = parser.parse_args(argv)

    if not args.findings.exists():
        print(f"error: findings file not found: {args.findings}", file=sys.stderr)
        return 2
    if not args.clean.exists():
        print(f"error: clean file not found: {args.clean}", file=sys.stderr)
        return 2

    records: dict[str, dict[str, Any]] = {}
    with args.clean.open() as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            sid = rec.get("source_id")
            if isinstance(sid, str):
                records[sid] = rec

    print(f"{'#':<4} {'sev/category':<18} field path(s) containing excerpt")
    print("-" * 70)
    with args.findings.open() as f:
        for idx, line in enumerate(f, start=1):
            if not line.strip():
                continue
            finding = json.loads(line)
            sev = finding.get("severity", "?")
            cat = finding.get("category", "?")
            excerpt = finding.get("excerpt", "")
            rec = records.get(finding.get("source_id", ""))
            if rec is None:
                where = "(record not found in clean file)"
            elif not excerpt:
                where = "(empty excerpt)"
            else:
                paths: list[str] = []
                paths.extend(_find_paths(rec.get("content"), excerpt, "content"))
                paths.extend(_find_paths(rec.get("metadata"), excerpt, "metadata"))
                where = ", ".join(paths) if paths else "(excerpt not found in record)"
            print(f"{idx:<4} {sev + '/' + cat:<18} {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
