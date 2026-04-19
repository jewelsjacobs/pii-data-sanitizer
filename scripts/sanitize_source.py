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
from scripts._config import LocalConfig, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", required=True, help="Source name (currently only 'ical' supported)."
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/local.json"), help="Local config path."
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

    entity_counts: Counter[str] = Counter()
    records_in = 0
    records_out = 0
    new_assignments = 0

    with raw_file.open() as fin, clean_file.open("w") as fout:
        for line in fin:
            if not line.strip():
                continue
            records_in += 1
            before = _mapping_sizes(mapping)
            sanitized = sanitize(
                json.loads(line),
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
            records_out += 1

    cfg.mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping.save(cfg.mapping_path)
    manifest_path = write_manifest(
        clean_dir,
        records_in=records_in,
        records_out=records_out,
        entities_redacted=dict(entity_counts),
        new_entities_auto_classified=new_assignments,
        mapping_file_path=cfg.mapping_path,
    )

    print(
        f"Sanitized {records_out}/{records_in} record(s) → {clean_file}\nManifest: {manifest_path}"
    )
    return 0


def _mapping_sizes(mapping: Mapping) -> dict[str, int]:
    """Return ``{kind: count}`` for the mapping's current pseudonym slots."""
    # Uses the module-private dict directly because the Mapping API is
    # intentionally small. If that grows, swap this for a public method.
    return {kind: len(slot) for kind, slot in mapping._by_kind.items()}  # noqa: SLF001


if __name__ == "__main__":
    sys.exit(main())
