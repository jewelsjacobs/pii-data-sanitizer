"""CLI: re-apply the known-surface sweep to an existing clean file.

When a surface is added to the mapping *after* a source was sanitized
(``add_mapping_entity``, or an audit-driven fix), the already-written
clean file still contains the raw surface. A full ``sanitize_source``
re-run repairs it but re-pays the NER cost on every record — hours on a
large mailbox. This script applies only the cheap deterministic layers
(the known-surface sweep plus the person-initials collapse) to every
record's ``content`` and string metadata fields, in place.

Skips ``_STRUCTURAL_KEEP_KEYS`` and opaque ``chat_identifier`` values,
mirroring ``_sanitize_metadata``. Default is dry-run (prints replacement
counts only — no PII in output); pass ``--apply`` to rewrite the file.
A ``.before-resweep.bak`` sibling backup is written before mutation.

Example:
    uv run python -m scripts.resweep_known_entities --source apple-mail --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from sanitizer.mapping import Mapping
from sanitizer.pipeline import (
    _STRUCTURAL_KEEP_KEYS,
    KeepList,
    _collapse_person_initials,
    _sub_known_entities,
)
from scripts._config import load_config


def _resweep_text(text: str, mapping: Mapping, keep: KeepList) -> str:
    return _collapse_person_initials(_sub_known_entities(text, mapping, keep))


def _resweep_value(value: Any, mapping: Mapping, keep: KeepList) -> Any:
    if isinstance(value, str):
        return _resweep_text(value, mapping, keep)
    if isinstance(value, list):
        return [_resweep_value(v, mapping, keep) for v in value]
    if isinstance(value, dict):
        return {k: _resweep_value(v, mapping, keep) for k, v in value.items()}
    return value


def _resweep_record(rec: dict[str, Any], mapping: Mapping, keep: KeepList) -> dict[str, Any]:
    out = dict(rec)
    if isinstance(out.get("content"), str):
        out["content"] = _resweep_text(out["content"], mapping, keep)
    meta = out.get("metadata")
    if isinstance(meta, dict):
        new_meta: dict[str, Any] = {}
        for key, value in meta.items():
            if key in _STRUCTURAL_KEEP_KEYS or key == "chat_identifier":
                new_meta[key] = value
            else:
                new_meta[key] = _resweep_value(value, mapping, keep)
        out["metadata"] = new_meta
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Source name, e.g. apple-mail.")
    parser.add_argument("--config", default="config/local.json", help="Local config path.")
    parser.add_argument("--apply", action="store_true", help="Rewrite the clean file.")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    clean_path = cfg.clean_staging_dir / args.source / "events.jsonl"
    if not clean_path.exists():
        raise SystemExit(f"no clean file at {clean_path}")

    mapping = Mapping.from_dict(json.loads(cfg.mapping_path.read_text()))
    keep = KeepList(names=cfg.keep_names, gpes=cfg.keep_gpes)

    changed = 0
    total = 0
    out_lines: list[str] = []
    # Iterate with the file object, not read_text().splitlines():
    # splitlines() also splits on U+2028/U+2029 etc., which appear raw
    # inside email bodies and would shear JSON lines mid-string.
    with clean_path.open(encoding="utf-8") as fh:
        lines = [ln.rstrip("\n") for ln in fh]
    for line in lines:
        if not line.strip():
            continue
        total += 1
        rec = json.loads(line)
        swept = _resweep_record(rec, mapping, keep)
        new_line = json.dumps(swept, ensure_ascii=False)
        if swept != rec:
            changed += 1
        out_lines.append(new_line)

    print(f"{args.source}: {changed}/{total} record(s) changed by resweep")
    if not args.apply:
        print("dry-run — pass --apply to rewrite the clean file")
        return

    backup = clean_path.with_suffix(clean_path.suffix + ".before-resweep.bak")
    shutil.copy2(clean_path, backup)
    clean_path.write_text("\n".join(out_lines) + "\n")
    print(f"rewrote {clean_path} (backup at {backup.name})")


if __name__ == "__main__":
    main()
