"""CLI: sanitize one source's ``/raw-staging/<source>/events.jsonl`` →
``/clean-staging/<source>/`` and write a manifest.

Reads self-identity + keep-list + mapping path from config/local.json.
Persists any newly-assigned pseudonyms back to the mapping file so
later runs stay consistent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from sanitizer.manifest import write_manifest
from sanitizer.mapping import Mapping
from sanitizer.pipeline import KeepList, SelfIdentity, sanitize
from sanitizer.state import STATE_FILENAME, write_state
from scripts._config import LocalConfig, load_config
from scripts._filter import is_in_window, validate_since, validate_until


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", required=True, help="Source name (currently only 'ical' supported)."
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/local.json"), help="Local config path."
    )
    parser.add_argument(
        "--since",
        type=validate_since,
        default=None,
        help=(
            "Only sanitize records whose timestamp is on or after this YYYY-MM-DD "
            "(inclusive). When set, the existing clean file is preserved and new "
            "records are appended (deduped by source_id) so prior runs aren't "
            "overwritten."
        ),
    )
    parser.add_argument(
        "--until",
        type=validate_until,
        default=None,
        help=(
            "Only sanitize records whose timestamp is strictly before this "
            "YYYY-MM-DD (exclusive upper bound). Pair with --since for "
            "non-overlapping windows: --since A --until B then --since B --until C."
        ),
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)

    raw_file = cfg.raw_staging_dir / args.source / "events.jsonl"
    if not raw_file.exists():
        print(
            f"error: no raw input at {raw_file}. Run the extract step first.",
            file=sys.stderr,
        )
        return 2

    identity = SelfIdentity(emails=cfg.self_emails, phones=cfg.self_phones)
    keep = KeepList(names=cfg.keep_names, gpes=cfg.keep_gpes)
    mapping = Mapping.load(cfg.mapping_path)

    clean_dir = cfg.clean_staging_dir / args.source
    clean_dir.mkdir(parents=True, exist_ok=True)
    clean_file = clean_dir / "events.jsonl"

    # In windowed mode (any --since or --until), preserve already-sanitized
    # records and skip any raw record whose source_id is already in the
    # clean file. Without either bound, the clean file is rewritten from
    # scratch as before.
    incremental = args.since is not None or args.until is not None
    existing_ids: set[str] = set()
    if incremental and clean_file.exists():
        with clean_file.open() as fexisting:
            for line in fexisting:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = rec.get("source_id")
                if isinstance(sid, str):
                    existing_ids.add(sid)

    # Count raw records up front so the progress line can show N/total.
    # One line per record, so wc-l is correct. Extra cost is trivial
    # compared to the NER run that follows.
    total = sum(1 for line in raw_file.open() if line.strip())
    if args.since or args.until:
        window_parts = []
        if args.since:
            window_parts.append(f"--since {args.since}")
        if args.until:
            window_parts.append(f"--until {args.until}")
        window_label = "window: " + " ".join(window_parts)
    else:
        window_label = "window: full"
    mode_label = (
        f"mode: append+dedupe ({len(existing_ids)} existing records preserved)"
        if incremental
        else "mode: overwrite"
    )
    print(
        f"Sanitizing {total} {args.source} record(s) from {raw_file}\n"
        f"   → {clean_file}\n"
        f"   mapping: {cfg.mapping_path}\n"
        f"   {window_label}\n"
        f"   {mode_label}",
        file=sys.stderr,
        flush=True,
    )

    # entity_counts / new_assignments track ONLY new pseudonyms assigned
    # to the mapping this run — i.e. mapping growth. Cache-hit
    # pseudonyms (entities already in the warm mapping) and hard-redact
    # rule matches (REDACTED_GOV_ID, REDACTED_2FA, REDACTED_CARD, etc.)
    # are NOT counted here. On a re-run with a warm mapping the count
    # is naturally zero — that's correct, not a bug.
    #
    # Surfacing total redactions per record would require either
    # plumbing through the pipeline or a post-hoc token-count scan;
    # tracked as future work.
    entity_counts: Counter[str] = Counter()
    records_in = 0
    records_out = 0
    skipped_window = 0
    skipped_dedupe = 0
    new_assignments = 0
    # Save the mapping every this-many records so a crashed run doesn't
    # lose any new pseudonyms already assigned.
    _mapping_save_interval = 500
    cfg.mapping_path.parent.mkdir(parents=True, exist_ok=True)

    open_mode = "a" if incremental else "w"

    with raw_file.open() as fin, clean_file.open(open_mode) as fout:
        for line in fin:
            if not line.strip():
                continue
            records_in += 1
            raw_record = json.loads(line)
            if not is_in_window(raw_record, args.since, args.until):
                skipped_window += 1
                continue
            sid = raw_record.get("source_id")
            if incremental and isinstance(sid, str) and sid in existing_ids:
                skipped_dedupe += 1
                continue
            before = _mapping_sizes(mapping)
            sanitized = sanitize(
                raw_record,
                mapping=mapping,
                self_identity=identity,
                keep=keep,
            )
            after = _mapping_sizes(mapping)
            for kind, size in after.items():
                delta = size - before.get(kind, 0)
                if delta > 0:
                    entity_counts[kind] += delta
                    new_assignments += delta
            fout.write(json.dumps(sanitized) + "\n")
            fout.flush()  # so `wc -l` on the output file shows progress
            records_out += 1
            if records_out % 50 == 0:
                pct = (records_out / total * 100) if total else 0.0
                print(
                    f"  {records_out}/{total} ({pct:.1f}%) — "
                    f"{sum(entity_counts.values())} new pseudonyms assigned "
                    f"(mapping growth; warm-mapping reruns can stay at 0)",
                    file=sys.stderr,
                    flush=True,
                )
            if records_out % _mapping_save_interval == 0:
                mapping.save(cfg.mapping_path)

    mapping.save(cfg.mapping_path)
    manifest_path = write_manifest(
        clean_dir,
        records_in=records_in,
        records_out=records_out,
        entities_redacted=dict(entity_counts),
        new_entities_auto_classified=new_assignments,
        mapping_file_path=cfg.mapping_path,
    )

    state = write_state(clean_dir, run_window=args.since)
    state_path = clean_dir / STATE_FILENAME

    summary_parts = [f"Sanitized {records_out}/{records_in} record(s) → {clean_file}"]
    if skipped_window or skipped_dedupe:
        summary_parts.append(
            f"  skipped: {skipped_window} outside --since window, "
            f"{skipped_dedupe} already in clean file (dedup)"
        )
    summary_parts.append(f"Manifest: {manifest_path}")
    summary_parts.append(f"State: {state_path}")
    suggested = state.get("suggested_next_since")
    if suggested:
        summary_parts.append(
            f"Next incremental run: --since {suggested} (latest record date in clean file)"
        )
    print("\n".join(summary_parts))
    return 0


def _mapping_sizes(mapping: Mapping) -> dict[str, int]:
    """Return ``{kind: count}`` for the mapping's current pseudonym slots."""
    # Uses the module-private dict directly because the Mapping API is
    # intentionally small. If that grows, swap this for a public method.
    return {kind: len(slot) for kind, slot in mapping._by_kind.items()}  # noqa: SLF001


if __name__ == "__main__":
    sys.exit(main())
