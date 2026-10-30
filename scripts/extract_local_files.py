"""CLI: extract local documents to ``/raw-staging/local-files/events.jsonl``.

Reads the ``local_files_source_dirs`` list from ``config/local.json``
(or ``--source-dir`` overrides), walks each path for supported
document formats, and emits normalized records in JSONL. The sanitize
step then reads the same ``<source>/events.jsonl`` convention as ical.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts._config import LocalConfig, load_config
from scripts._filter import validate_since
from sources.local_files.adapter import SUPPORTED_EXTENSIONS, AdapterError, extract_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        action="append",
        type=Path,
        default=None,
        help="Path to a file or directory. Can be given multiple times. "
        "Overrides config/local.json.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/local.json"),
        help="Path to local config (default: config/local.json).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Where to write events.jsonl. Defaults to <raw_staging_dir from config>/local-files/.",
    )
    parser.add_argument(
        "--include-extensions",
        type=_parse_extensions,
        default=None,
        help=(
            "Comma-separated list of extensions to process (subset of "
            f"{sorted(SUPPORTED_EXTENSIONS)}). Other extensions in the walk are "
            "skipped silently. Useful when one extension dominates a source dir "
            "(e.g. an Obsidian vault syncing 100k .md files into iCloud Drive). "
            "Example: --include-extensions .pdf,.docx,.txt,.rtf"
        ),
    )
    parser.add_argument(
        "--skip-unsupported",
        action="store_true",
        help=(
            "Warn-and-skip on legacy .doc files instead of halting. Default is "
            "to halt (matches the fail-loud policy). Use this on one-shot "
            "historical archive runs where you'd rather get most of the files "
            "than convert .doc files to .docx first."
        ),
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help=(
            "Warn-and-skip on per-file extraction failures (corrupted PDFs, "
            "password-protected docx, UTF-8 decode failures, permission denied) "
            "instead of halting the whole run. Default is to halt. Use this "
            "with --skip-unsupported on heterogeneous historical archives where "
            "a handful of broken files shouldn't stop you from ingesting the "
            "other ~17,000."
        ),
    )
    parser.add_argument(
        "--exclude-dir",
        action="append",
        type=Path,
        default=[],
        help=(
            "Skip every file under this directory. May be passed multiple "
            "times. Useful to prune known-noise subtrees (third-party icon "
            "libraries, other Obsidian vaults syncing into iCloud) without "
            "having to enumerate the dirs you do want."
        ),
    )
    parser.add_argument(
        "--exclude-dir-name",
        action="append",
        default=[],
        help=(
            "Skip any file whose path includes a directory matching this name "
            "anywhere in the tree (e.g. 'node_modules', '.git', '__pycache__'). "
            "May be passed multiple times. Distinct from --exclude-dir which "
            "takes a single absolute path."
        ),
    )
    parser.add_argument(
        "--modified-since",
        type=validate_since,
        default=None,
        help=(
            "Only read files whose mtime date (UTC, zero-padded YYYY-MM-DD) is "
            "on or after this date — the incremental-run mode from "
            "source-adapters.md → Open Items. The check runs before any file "
            "is opened, so legacy formats and broken encodings in the "
            "untouched archive can't halt a windowed run. Match this to the "
            "sanitize step's --since (both compare UTC dates). When omitted, "
            "defaults to the last successful run's date from "
            "<out-dir>/state.json (see --full)."
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Ignore <out-dir>/state.json and walk everything, rewriting the "
            "raw file from scratch. Only suppresses the state-file default; "
            "an explicit --modified-since still wins."
        ),
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)
    source_dirs = args.source_dir if args.source_dir else cfg.local_files_source_dirs
    if not source_dirs:
        print(
            "error: no local-files sources configured. Add local_files_source_dirs to "
            "config/local.json or pass --source-dir.",
            file=sys.stderr,
        )
        return 2

    # Validate every --exclude-dir actually exists on disk. A typo or a
    # path mangled by shell-paste (embedded newline, double space, etc.)
    # would otherwise just silently match nothing — which lets the user
    # think they're excluding something while the run still touches it.
    bad_excludes = []
    for d in args.exclude_dir or []:
        if not Path(d).expanduser().exists():
            bad_excludes.append(d)
    if bad_excludes:
        print(
            "error: --exclude-dir paths do not exist on disk. Check for typos "
            "or shell-paste mangling (embedded newlines, double spaces):",
            file=sys.stderr,
        )
        for d in bad_excludes:
            print(f"  {d!r}", file=sys.stderr)
        return 2

    out_dir = args.out_dir if args.out_dir else cfg.raw_staging_dir / "local-files"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "events.jsonl"
    state_path = out_dir / "state.json"

    # Config-level exclude names (node_modules, .git, …) merge with any
    # --exclude-dir-name flags; duplicates are harmless but pruned for tidy
    # output in the mode line below.
    exclude_dir_names = list(args.exclude_dir_name)
    for name in cfg.local_files_exclude_dir_names:
        if name not in exclude_dir_names:
            exclude_dir_names.append(name)

    # Incremental by default: an omitted --modified-since falls back to the
    # date of the last successful run, so `make extract` never re-walks the
    # whole archive (and never re-opens the legacy files buried in it).
    modified_since = args.modified_since
    defaulted_from_state = False
    if modified_since is None and not args.full and state_path.exists():
        try:
            last_run_at = json.loads(state_path.read_text())["last_run_at"]
            modified_since = validate_since(last_run_at[:10])
            defaulted_from_state = True
        except (json.JSONDecodeError, KeyError, argparse.ArgumentTypeError):
            print(
                f"warning: {state_path} is unreadable; doing a full walk. "
                "Delete the file to silence this.",
                file=sys.stderr,
            )
    if defaulted_from_state:
        print(
            f"mode: incremental --modified-since {modified_since} "
            f"(from {state_path.name}; pass --full for a complete rewalk)"
        )

    # A windowed run appends to the raw file and dedupes by source_id, the
    # same contract sanitize's --since uses one layer down — so the raw
    # JSONL stays complete and a later full sanitize can't lose history.
    existing_ids: set[str] = set()
    if modified_since is not None and out_file.exists():
        for line in out_file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                existing_ids.add(json.loads(line)["source_id"])
            except (json.JSONDecodeError, KeyError):
                continue
        print(f"mode: append+dedupe ({len(existing_ids)} existing records preserved)")

    count = 0
    skipped_dupes = 0
    open_mode = "a" if existing_ids else "w"
    with out_file.open(open_mode) as fout:
        for record in extract_files(
            source_dirs,
            include_extensions=args.include_extensions,
            skip_unsupported=args.skip_unsupported,
            continue_on_error=args.continue_on_error,
            exclude_dirs=args.exclude_dir,
            exclude_dir_names=exclude_dir_names,
            modified_since=modified_since,
        ):
            if record.get("source_id") in existing_ids:
                skipped_dupes += 1
                continue
            fout.write(json.dumps(record) + "\n")
            count += 1

    state_path.write_text(
        json.dumps(
            {
                "last_run_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "modified_since_used": modified_since,
                "records_written": count,
            },
            indent=2,
        )
        + "\n"
    )

    dedupe_note = f" ({skipped_dupes} already in raw file, skipped)" if skipped_dupes else ""
    print(f"Extracted {count} local-files record(s){dedupe_note} → {out_file}")
    return 0


def _parse_extensions(value: str) -> frozenset[str]:
    """Parse a comma-separated list of extensions, normalizing dots and case."""
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("--include-extensions must list at least one extension")
    normalized = frozenset(p if p.startswith(".") else f".{p}" for p in parts)
    bad = normalized - SUPPORTED_EXTENSIONS
    if bad:
        raise argparse.ArgumentTypeError(
            f"--include-extensions contains unsupported entries: {sorted(bad)}. "
            f"Allowed: {sorted(SUPPORTED_EXTENSIONS)}"
        )
    return normalized


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AdapterError as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        sys.exit(2)
