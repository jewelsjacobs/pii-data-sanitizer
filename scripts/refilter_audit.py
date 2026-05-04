"""CLI: re-apply the current ``_verify_finding`` logic to an existing audit log.

Use this when you've improved the verifier (e.g. tightened the
mostly-bracketed-token check) and want to see how many already-saved
findings would be dropped by the new logic — without paying the
2-4 hour cost of re-running the LLM auditor.

Reads the original log line-by-line, runs each finding through the
verifier, and writes the surviving findings to a new file. Prints
before/after counts and the drop breakdown.

Note: this can only re-run the local deterministic checks (bracketed
token, hallucination via substring match, keep-list). It cannot
recover findings the LLM didn't produce in the first place.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sanitizer.auditor import AuditConfig, DropStats, Finding, _verify_finding
from scripts._config import LocalConfig, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, required=True, help="Path to existing audit log (JSONL)."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write the refiltered log. Defaults to <input>.refiltered.jsonl.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            "Path to the source events.jsonl that produced this audit. Used as the "
            "haystack for the substring (hallucination) check. If omitted, the "
            "substring check is SKIPPED — useful for a fast bracketed-token-only "
            "refilter when you don't need the full verifier."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/local.json"),
        help="Local config (for keep-list).",
    )
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"error: input log not found: {args.input}", file=sys.stderr)
        return 2

    out_path = args.output or args.input.with_suffix(".refiltered.jsonl")

    cfg: LocalConfig = load_config(args.config)
    audit_config = AuditConfig(keep_names=cfg.keep_names, keep_gpes=cfg.keep_gpes)

    # If a source file was provided, build a single haystack containing
    # every record's content + metadata so the substring check works.
    haystack = ""
    if args.source is not None:
        if not args.source.exists():
            print(f"error: source file not found: {args.source}", file=sys.stderr)
            return 2
        chunks: list[str] = []
        with args.source.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = rec.get("content")
                if isinstance(content, str):
                    chunks.append(content)
                meta = rec.get("metadata")
                if isinstance(meta, dict):
                    for v in meta.values():
                        if isinstance(v, str):
                            chunks.append(v)
        haystack = "\n".join(chunks)

    stats = DropStats()
    total_in = 0
    kept = 0

    with args.input.open() as fin, out_path.open("w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            total_in += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            finding = Finding(
                source=obj.get("source", ""),
                source_id=obj.get("source_id", ""),
                category=obj.get("category", ""),
                severity=obj.get("severity", ""),
                excerpt=obj.get("excerpt", ""),
                reasoning=obj.get("reasoning", ""),
            )

            # When haystack is empty (--source not provided), substring check
            # always succeeds: skip it by passing the excerpt itself as the
            # haystack. Other verifier checks (bracket, keep-list) still apply.
            check_haystack = haystack if haystack else finding.excerpt

            result = _verify_finding(
                finding,
                haystack=check_haystack,
                config=audit_config,
                stats=stats,
            )
            if result is not None:
                fout.write(json.dumps(obj) + "\n")
                kept += 1

    print(f"Input:  {total_in} findings → {args.input}")
    print(f"Kept:   {kept} findings → {out_path}")
    print(f"Dropped: {total_in - kept} ({(total_in - kept) * 100 // total_in}%)")
    print(
        f"  bracketed_token: {stats.bracketed_token}, "
        f"hallucinated: {stats.hallucinated}, "
        f"keep_list: {stats.keep_list}, "
        f"empty_excerpt: {stats.empty_excerpt}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
